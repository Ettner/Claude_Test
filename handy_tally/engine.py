"""Zentraler Tally-Zustand.

Jede Quelle (OBS, MIDI, DM3, Demo) meldet pro Tally, ob er PROGRAM (live/rot)
oder PREVIEW (grün) ist. Die Engine kombiniert alle Quellen (ODER-Verknüpfung)
und benachrichtigt die angeschlossenen Handys.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Iterable


class TallyEngine:
    def __init__(self, tallies: Iterable[dict]):
        self.tallies: dict[str, dict] = {}
        # flags[source][tally_id] = {"program": bool, "preview": bool}
        self.flags: dict[str, dict[str, dict[str, bool]]] = {}
        # status[source] = {"connected": bool, "info": str, ...}
        self.status: dict[str, dict[str, Any]] = {}
        # globale Infos (z.B. OBS streamt / nimmt auf)
        self.globals: dict[str, Any] = {}
        self._listeners: set[Callable[[dict], Any]] = set()
        self._dirty = asyncio.Event()
        self.set_tallies(tallies)

    def set_tallies(self, tallies: Iterable[dict]) -> None:
        """Tally-Liste (neu) setzen – z.B. nach Änderung im Admin-Client."""
        new: dict[str, dict] = {}
        for t in tallies:
            if not t.get("id"):
                raise ValueError(f"Tally ohne 'id': {t}")
            if t["id"] in new:
                raise ValueError(f"Tally-ID doppelt: {t['id']}")
            t.setdefault("name", t["id"])
            new[t["id"]] = t
        self.tallies = new
        for flags in self.flags.values():
            for tid in list(flags):
                if tid not in new:
                    del flags[tid]
        self._dirty.set()

    def remove_source(self, source: str) -> None:
        self.flags.pop(source, None)
        self.status.pop(source, None)
        self._dirty.set()

    def notify(self) -> None:
        self._dirty.set()

    # ---------- API für Quellen ----------
    def update(self, source: str, per_tally: dict[str, tuple[bool, bool]]) -> None:
        """per_tally: {tally_id: (program, preview)} – ersetzt alle Flags der Quelle."""
        new = {tid: {"program": bool(p), "preview": bool(v)} for tid, (p, v) in per_tally.items()}
        if self.flags.get(source) != new:
            self.flags[source] = new
            self._dirty.set()

    def set_status(self, source: str, connected: bool, info: str = "") -> None:
        st = {"connected": connected, "info": info}
        if self.status.get(source) != st:
            self.status[source] = st
            if not connected:
                # Bei Verbindungsverlust keine veralteten Zustände anzeigen
                self.flags.pop(source, None)
            self._dirty.set()

    def set_global(self, key: str, value: Any) -> None:
        if self.globals.get(key) != value:
            self.globals[key] = value
            self._dirty.set()

    # ---------- Zustand ----------
    def snapshot(self) -> dict:
        out = []
        for tid, t in self.tallies.items():
            program_by, preview_by = [], []
            for src, flags in self.flags.items():
                f = flags.get(tid)
                if not f:
                    continue
                if f["program"]:
                    program_by.append(src)
                if f["preview"]:
                    preview_by.append(src)
            program = bool(program_by)
            out.append({
                "id": tid,
                "name": t["name"],
                "state": "program" if program else ("preview" if preview_by else "idle"),
                "program_by": program_by,
                "preview_by": preview_by,
            })
        return {
            "type": "state",
            "time": time.time(),
            "tallies": out,
            "sources": self.status,
            "globals": self.globals,
        }

    def subscribe(self, cb: Callable[[dict], Any]) -> None:
        self._listeners.add(cb)

    def unsubscribe(self, cb: Callable[[dict], Any]) -> None:
        self._listeners.discard(cb)

    async def run(self) -> None:
        """Sammelt Änderungen (kurz entprellt) und verteilt sie an alle Listener."""
        while True:
            await self._dirty.wait()
            await asyncio.sleep(0.02)
            self._dirty.clear()
            snap = self.snapshot()
            for cb in list(self._listeners):
                try:
                    res = cb(snap)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:  # ein kaputter Client soll nichts blockieren
                    print(f"[engine] Listener-Fehler: {exc}")
