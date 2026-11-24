"""OBS Studio über obs-websocket v5 (in OBS 28+ eingebaut).

Pro Tally in der config:
    obs:
      sources: ["Kamera 1"]   # PROGRAM, wenn die Quelle in der Programm-Szene sichtbar ist
      scenes:  ["Interview"]  # PROGRAM, wenn diese Szene selbst auf Programm ist
      audio:   ["Mikro 1"]    # PROGRAM, wenn dieser Audio-Eingang NICHT stummgeschaltet ist
PREVIEW gilt analog für die Vorschau-Szene (nur im Studio-Modus).
Verschachtelte Szenen und Gruppen werden rekursiv aufgelöst.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json

import websockets

from engine import TallyEngine

# EventSubscription-Bits: Inputs(8) | Scenes(4) | Outputs(64) | SceneItems(128) | Ui(1024)
EVENT_SUBS = 8 | 4 | 64 | 128 | 1024

REFRESH_EVENTS = {
    "CurrentProgramSceneChanged", "CurrentPreviewSceneChanged", "StudioModeStateChanged",
    "SceneItemEnableStateChanged", "SceneItemCreated", "SceneItemRemoved",
    "SceneNameChanged", "InputNameChanged", "SceneListChanged",
    "InputMuteStateChanged", "StreamStateChanged", "RecordStateChanged",
}


def _auth(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class OBSSource:
    name = "OBS"

    def __init__(self, cfg: dict, engine: TallyEngine):
        self.host = cfg.get("host", "localhost")
        self.port = int(cfg.get("port", 4455))
        self.password = cfg.get("password") or ""
        self.engine = engine
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._refresh = asyncio.Event()
        self._ws = None

    async def run(self) -> None:
        url = f"ws://{self.host}:{self.port}"
        while True:
            try:
                async with websockets.connect(url, max_size=None, open_timeout=5) as ws:
                    self._ws = ws
                    await self._identify(ws)
                    self.engine.set_status(self.name, True, url)
                    print(f"[OBS] verbunden mit {url}")
                    reader = asyncio.create_task(self._reader(ws))
                    refresher = asyncio.create_task(self._refresher())
                    self._refresh.set()
                    try:
                        done, _ = await asyncio.wait({reader, refresher}, return_when=asyncio.FIRST_COMPLETED)
                        for t in done:
                            t.result()
                    finally:
                        for t in (reader, refresher):
                            t.cancel()
                        await asyncio.gather(reader, refresher, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.engine.set_status(self.name, False, f"{url}: {exc}")
                self.engine.set_global("streaming", False)
                self.engine.set_global("recording", False)
                print(f"[OBS] keine Verbindung ({exc}) – neuer Versuch in 3 s")
            finally:
                self._ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(ConnectionError("OBS getrennt"))
                self._pending.clear()
            await asyncio.sleep(3)

    async def _identify(self, ws) -> None:
        hello = json.loads(await ws.recv())
        d = hello.get("d", {})
        ident = {"rpcVersion": 1, "eventSubscriptions": EVENT_SUBS}
        if "authentication" in d:
            a = d["authentication"]
            ident["authentication"] = _auth(self.password, a["salt"], a["challenge"])
        await ws.send(json.dumps({"op": 1, "d": ident}))
        msg = json.loads(await ws.recv())
        if msg.get("op") != 2:
            raise ConnectionError(f"Identify fehlgeschlagen (Passwort?): {msg}")

    async def _reader(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            op, d = msg.get("op"), msg.get("d", {})
            if op == 5 and d.get("eventType") in REFRESH_EVENTS:
                self._refresh.set()
            elif op == 7:
                fut = self._pending.pop(d.get("requestId"), None)
                if fut and not fut.done():
                    fut.set_result(d)
        raise ConnectionError("OBS hat die Verbindung geschlossen")

    async def discover(self) -> dict:
        """Szenen- und Quellennamen für den Admin-Client."""
        if not self._ws:
            return {}
        scenes = [s["sceneName"] for s in (await self._request("GetSceneList")).get("scenes", [])][::-1]
        inputs = (await self._request("GetInputList")).get("inputs", [])
        return {"scenes": scenes, "inputs": sorted(i["inputName"] for i in inputs)}

    async def _request(self, rtype: str, **data) -> dict:
        rid = str(next(self._ids))
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._ws.send(json.dumps({"op": 6, "d": {"requestType": rtype, "requestId": rid, "requestData": data}}))
        d = await asyncio.wait_for(fut, 5)
        status = d.get("requestStatus", {})
        if not status.get("result"):
            raise RuntimeError(f"{rtype}: {status.get('comment') or status.get('code')}")
        return d.get("responseData") or {}

    async def _refresher(self) -> None:
        while True:
            await self._refresh.wait()
            await asyncio.sleep(0.05)  # mehrere Events zusammenfassen
            self._refresh.clear()
            await self._compute()

    async def _visible(self, scene: str, group: bool = False, seen: set | None = None) -> set[str]:
        seen = seen if seen is not None else set()
        if scene in seen:
            return set()
        seen.add(scene)
        req = "GetGroupSceneItemList" if group else "GetSceneItemList"
        items = (await self._request(req, sceneName=scene)).get("sceneItems", [])
        out: set[str] = set()
        for it in items:
            if not it.get("sceneItemEnabled"):
                continue
            name = it["sourceName"]
            out.add(name)
            if it.get("isGroup"):
                out |= await self._visible(name, True, seen)
            elif it.get("sourceType") == "OBS_SOURCE_TYPE_SCENE":
                out |= await self._visible(name, False, seen)
        return out

    async def _compute(self) -> None:
        program = (await self._request("GetCurrentProgramScene")).get("currentProgramSceneName")
        studio = (await self._request("GetStudioModeEnabled")).get("studioModeEnabled")
        preview = None
        if studio:
            preview = (await self._request("GetCurrentPreviewScene")).get("currentPreviewSceneName")

        prog_vis = await self._visible(program) if program else set()
        prev_vis = await self._visible(preview) if preview else set()

        # Audio-Mute-Zustände nur für konfigurierte Eingänge abfragen
        audio_live: dict[str, bool] = {}
        for t in self.engine.tallies.values():
            for inp in (t.get("obs") or {}).get("audio", []):
                if inp not in audio_live:
                    try:
                        muted = (await self._request("GetInputMute", inputName=inp)).get("inputMuted")
                        audio_live[inp] = not muted
                    except RuntimeError as exc:
                        print(f"[OBS] Audio-Eingang '{inp}': {exc}")
                        audio_live[inp] = False

        result: dict[str, tuple[bool, bool]] = {}
        for tid, t in self.engine.tallies.items():
            oc = t.get("obs") or {}
            srcs, scenes, audio = set(oc.get("sources", [])), set(oc.get("scenes", [])), oc.get("audio", [])
            if not (srcs or scenes or audio):
                continue
            is_prog = (program in scenes) or bool(srcs & prog_vis) or any(audio_live.get(a) for a in audio)
            is_prev = bool(preview) and ((preview in scenes) or bool(srcs & prev_vis))
            result[tid] = (is_prog, is_prev)
        self.engine.update(self.name, result)

        for key, req in (("streaming", "GetStreamStatus"), ("recording", "GetRecordStatus")):
            try:
                self.engine.set_global(key, bool((await self._request(req)).get("outputActive")))
            except RuntimeError:
                pass
        self.engine.set_status(self.name, True, f"Programm: {program}" + (f" | Vorschau: {preview}" if preview else ""))
