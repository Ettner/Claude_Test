#!/usr/bin/env python3
"""Handy-Tally: Tally-Lichter auf dem Smartphone für OBS, MIDI-Controller und Yamaha DM3.

Start:   python tally.py              (liest config.yaml, legt sie bei Bedarf an)
         python tally.py --demo       (Testmodus ohne Hardware)
         python tally.py --midi-monitor
Handys:  http://<IP>:8080/
Admin:   http://<IP>:8080/admin       (Konfiguration & Steuerung)
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hmac
import itertools
import json
import shutil
import socket
import sys
import time
from pathlib import Path

import yaml
from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from engine import TallyEngine  # noqa: E402

DEFAULT_CONFIG = {
    "server": {"host": "0.0.0.0", "port": 8080, "admin_password": ""},
    "obs": {"enabled": False, "host": "localhost", "port": 4455, "password": ""},
    "midi": {"enabled": False, "ports": []},
    "dm3": {"enabled": False, "host": "192.168.0.128", "port": 49280, "poll_interval": 1.0},
    "tallies": [{"id": "cam1", "name": "Kamera 1"}, {"id": "cam2", "name": "Kamera 2"}],
}


# ---------------------------------------------------------------- Hilfs-Quellen
class DemoSource:
    """Schaltet die Tallys reihum – zum Testen der Handys ohne OBS/Pult."""
    name = "Demo"

    def __init__(self, engine: TallyEngine):
        self.engine = engine

    async def run(self) -> None:
        self.engine.set_status(self.name, True, "Testmodus")
        for i in itertools.count():
            ids = list(self.engine.tallies)
            if ids:
                prog, prev = ids[i % len(ids)], ids[(i + 1) % len(ids)]
                self.engine.update(self.name, {t: (t == prog, t == prev and t != prog) for t in ids})
            await asyncio.sleep(3)


class ManualSource:
    """Manuelles Schalten aus dem Admin-Client (bleibt über Config-Reloads erhalten)."""
    name = "Manuell"

    def __init__(self, engine: TallyEngine):
        self.engine = engine
        self.overrides: dict[str, str] = {}  # tally_id -> "program" | "preview"

    def set(self, tid: str, state: str | None) -> None:
        if state in ("program", "preview"):
            self.overrides[tid] = state
        else:
            self.overrides.pop(tid, None)
        self.push()

    def push(self) -> None:
        self.overrides = {t: s for t, s in self.overrides.items() if t in self.engine.tallies}
        self.engine.update(self.name, {t: (s == "program", s == "preview") for t, s in self.overrides.items()})


# ---------------------------------------------------------------- Controller
class Controller:
    """Hält Config, Engine und laufende Quellen; kann zur Laufzeit neu laden."""

    def __init__(self, cfg_path: Path, args):
        self.cfg_path = cfg_path
        self.args = args
        self.cfg: dict = {}
        self.engine = TallyEngine([])
        self.manual = ManualSource(self.engine)
        self.sources: dict[str, object] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.demo = bool(args.demo)
        self.clients: dict[int, dict] = {}   # Handy-Verbindungen
        self._client_ids = itertools.count(1)
        self.engine.subscribe(self._broadcast)

    # ----- Config
    def load(self) -> dict:
        if not self.cfg_path.exists():
            example = HERE / "config.example.yaml"
            if example.exists():
                shutil.copy(example, self.cfg_path)
            else:
                self.cfg_path.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False, allow_unicode=True),
                                         encoding="utf-8")
            print(f"[config] {self.cfg_path.name} neu angelegt – bitte im Admin-Client anpassen.")
        return yaml.safe_load(self.cfg_path.read_text(encoding="utf-8")) or {}

    def save(self, cfg: dict) -> None:
        if self.cfg_path.exists():
            shutil.copy(self.cfg_path, self.cfg_path.with_suffix(".yaml.bak"))
        self.cfg_path.write_text(
            "# Handy-Tally Konfiguration (vom Admin-Client gespeichert)\n"
            + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")

    @staticmethod
    def validate(cfg: dict) -> dict:
        if not isinstance(cfg, dict):
            raise ValueError("Config muss ein Objekt sein")
        cfg = copy.deepcopy(cfg)
        tallies = cfg.get("tallies") or []
        ids = set()
        for t in tallies:
            tid = str(t.get("id", "")).strip()
            if not tid or any(c in tid for c in " /?#&"):
                raise ValueError(f"Ungültige Tally-ID: '{tid}' (keine Leerzeichen oder Sonderzeichen)")
            if tid in ids:
                raise ValueError(f"Tally-ID doppelt: {tid}")
            ids.add(tid)
            t["id"] = tid
            t["name"] = str(t.get("name") or tid)
            # leere Unterabschnitte entfernen
            obs = {k: [x for x in v if x] for k, v in (t.get("obs") or {}).items() if v}
            obs = {k: v for k, v in obs.items() if v}
            t.pop("obs", None)
            if obs:
                t["obs"] = obs
            if not (t.get("dm3") or {}).get("channel"):
                t.pop("dm3", None)
            if not t.get("midi"):
                t.pop("midi", None)
        cfg["tallies"] = tallies
        return cfg

    async def apply(self, cfg: dict) -> None:
        """Quellen stoppen, Tallys setzen, Quellen neu starten."""
        await self.stop_sources()
        self.cfg = cfg
        self.engine.set_tallies(copy.deepcopy(cfg.get("tallies") or []))
        self.manual.push()
        self.start_sources()

    async def stop_sources(self) -> None:
        for name, task in self.tasks.items():
            task.cancel()
        for name, task in self.tasks.items():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self.engine.remove_source(name)
        self.tasks.clear()
        self.sources.clear()
        self.engine.set_global("streaming", False)
        self.engine.set_global("recording", False)

    def start_sources(self) -> None:
        cfg, srcs = self.cfg, []
        if self.demo:
            srcs.append(DemoSource(self.engine))
        if (cfg.get("obs") or {}).get("enabled"):
            from sources.obs import OBSSource
            srcs.append(OBSSource(cfg["obs"], self.engine))
        if (cfg.get("midi") or {}).get("enabled") or self.args.midi_monitor:
            try:
                from sources.midi import MidiSource
                srcs.append(MidiSource(cfg.get("midi") or {}, self.engine, monitor=self.args.midi_monitor))
            except ImportError as exc:
                self.engine.set_status("MIDI", False, f"nicht installiert: pip install mido python-rtmidi ({exc})")
        if (cfg.get("dm3") or {}).get("enabled"):
            from sources.yamaha_dm3 import DM3Source
            srcs.append(DM3Source(cfg["dm3"], self.engine))
        for s in srcs:
            self.sources[s.name] = s
            self.tasks[s.name] = asyncio.create_task(self._guard(s), name=s.name)
        if not srcs:
            print("Hinweis: keine Quelle aktiv – im Admin-Client aktivieren.")

    async def _guard(self, src) -> None:
        try:
            await src.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[{src.name}] abgestürzt: {exc!r}")
            self.engine.set_status(src.name, False, f"Fehler: {exc}")

    # ----- Handys
    async def _broadcast(self, snap: dict) -> None:
        data = json.dumps(snap)
        for c in list(self.clients.values()):
            try:
                await c["ws"].send_str(data)
            except Exception:
                pass

    async def send_to(self, cid: int | None, msg: dict) -> int:
        targets = [c for k, c in self.clients.items() if cid is None or k == cid]
        for c in targets:
            await c["ws"].send_str(json.dumps(msg))
        return len(targets)

    def client_list(self) -> list[dict]:
        return [{"id": k, "ip": c["ip"], "tally": c.get("tally"), "agent": c.get("agent", ""),
                 "since": c["since"]} for k, c in self.clients.items()]


# ---------------------------------------------------------------- Webserver
def make_app(ctl: Controller) -> web.Application:
    engine = ctl.engine

    def check_admin(req: web.Request) -> None:
        pw = str((ctl.cfg.get("server") or {}).get("admin_password") or "")
        if pw and not hmac.compare_digest(req.headers.get("X-Admin-Key", ""), pw):
            raise web.HTTPUnauthorized(text=json.dumps({"error": "Admin-Passwort falsch"}),
                                       content_type="application/json")

    async def index(_req):
        return web.FileResponse(HERE / "static" / "index.html")

    async def admin(_req):
        return web.FileResponse(HERE / "static" / "admin.html")

    async def api_state(_req):
        return web.json_response(engine.snapshot())

    async def ws_handler(req):
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(req)
        cid = next(ctl._client_ids)
        ctl.clients[cid] = {"ws": ws, "ip": req.remote, "since": time.time()}
        await ws.send_str(json.dumps({"type": "welcome", "client": cid}))
        await ws.send_str(json.dumps(engine.snapshot()))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                if msg.data == "ping":
                    await ws.send_str('{"type":"pong"}')
                    continue
                try:
                    m = json.loads(msg.data)
                except ValueError:
                    continue
                if m.get("type") == "hello":
                    ctl.clients[cid]["tally"] = m.get("tally")
                    ctl.clients[cid]["agent"] = str(m.get("agent", ""))[:120]
        finally:
            ctl.clients.pop(cid, None)
        return ws

    # ----- Admin-API
    async def get_config(req):
        check_admin(req)
        return web.json_response(ctl.cfg)

    async def put_config(req):
        check_admin(req)
        try:
            cfg = ctl.validate(await req.json())
        except (ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        ctl.save(cfg)
        await ctl.apply(cfg)
        print("[config] gespeichert und neu geladen")
        return web.json_response({"ok": True, "config": cfg})

    async def get_status(req):
        check_admin(req)
        midi = ctl.sources.get("MIDI")
        since = int(req.query.get("midi_since", 0))
        return web.json_response({
            "state": engine.snapshot(),
            "clients": ctl.client_list(),
            "overrides": ctl.manual.overrides,
            "demo": ctl.demo,
            "midi_log": [e for e in midi.log if e["seq"] > since] if midi else [],
        })

    async def discover(req):
        check_admin(req)
        out = {"obs": {}, "midi_ports": [], "midi_error": ""}
        obs = ctl.sources.get("OBS")
        if obs:
            try:
                out["obs"] = await obs.discover()
            except Exception as exc:
                out["obs_error"] = str(exc)
        try:
            import mido
            out["midi_ports"] = mido.get_input_names()
        except Exception as exc:
            out["midi_error"] = str(exc)
        return web.json_response(out)

    async def override(req):
        check_admin(req)
        d = await req.json()
        if d.get("all_off"):
            ctl.manual.overrides.clear()
            ctl.manual.push()
        else:
            ctl.manual.set(d["id"], d.get("state"))
        return web.json_response({"ok": True, "overrides": ctl.manual.overrides})

    async def demo(req):
        check_admin(req)
        ctl.demo = bool((await req.json()).get("on"))
        await ctl.apply(ctl.cfg)
        return web.json_response({"ok": True, "demo": ctl.demo})

    async def client_cmd(req):
        check_admin(req)
        d = await req.json()
        cid = d.get("client")
        cid = int(cid) if cid not in (None, "", "all") else None
        if d.get("action") == "identify":
            n = await ctl.send_to(cid, {"type": "identify"})
        elif d.get("action") == "assign":
            n = await ctl.send_to(cid, {"type": "assign", "tally": d.get("tally")})
        elif d.get("action") == "reload":
            n = await ctl.send_to(cid, {"type": "reload"})
        else:
            return web.json_response({"ok": False, "error": "unbekannte Aktion"}, status=400)
        return web.json_response({"ok": True, "sent": n})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/admin", admin)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", put_config)
    app.router.add_get("/api/status", get_status)
    app.router.add_get("/api/discover", discover)
    app.router.add_post("/api/override", override)
    app.router.add_post("/api/demo", demo)
    app.router.add_post("/api/client", client_cmd)
    app.router.add_static("/static", HERE / "static")
    return app


def local_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return sorted(ips) or ["127.0.0.1"]


def print_urls(port: int) -> None:
    urls = [f"http://{ip}:{port}/" for ip in local_ips()]
    print("\n  Handy-Tally läuft.")
    print("  Handys (selbes WLAN):  " + "  ".join(urls))
    print(f"  Admin / Steuerung:     http://localhost:{port}/admin   (oder {urls[0]}admin)")
    try:
        import qrcode  # optional: pip install qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(urls[0])
        qr.print_ascii(invert=True)
    except ImportError:
        pass
    print()


# ---------------------------------------------------------------- main
async def main(args) -> None:
    ctl = Controller(Path(args.config), args)
    try:
        cfg = ctl.validate(ctl.load())
    except ValueError as exc:
        sys.exit(f"Config-Fehler: {exc}")

    engine_task = asyncio.create_task(ctl.engine.run())
    await ctl.apply(cfg)

    srv = cfg.get("server") or {}
    host, port = srv.get("host", "0.0.0.0"), int(args.port or srv.get("port", 8080))
    runner = web.AppRunner(make_app(ctl))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print_urls(port)
    try:
        await engine_task
    finally:
        await ctl.stop_sources()
        await runner.cleanup()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Handy-Tally für OBS, MIDI und Yamaha DM3")
    ap.add_argument("-c", "--config", default=str(HERE / "config.yaml"))
    ap.add_argument("-p", "--port", type=int, help="Webserver-Port (überschreibt Config)")
    ap.add_argument("--demo", action="store_true", help="Testmodus: Tallys wechseln reihum")
    ap.add_argument("--midi-monitor", action="store_true", help="alle MIDI-Nachrichten ausgeben")
    try:
        asyncio.run(main(ap.parse_args()))
    except KeyboardInterrupt:
        print("\nBeendet.")
