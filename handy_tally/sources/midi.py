"""MIDI-Quelle für Radio-/DAW-Controller (Mackie Control, X-Touch, Fader-Boxen, ...).

Global:
    midi:
      enabled: true
      ports: ["X-Touch", "loopMIDI"]   # Teilstrings der MIDI-Eingangsnamen

Pro Tally eine Liste von Bedingungen – ALLE müssen erfüllt sein, dann PROGRAM:
    midi:
      - {type: pitchbend, channel: 1, threshold: 5}           # Mackie-Fader Kanal 1 > 5 %
      - {type: note, channel: 1, number: 16, mode: gate, invert: true}  # Mute-LED aus

Felder:
    type:      cc | note | pitchbend
    channel:   1..16
    number:    CC- bzw. Notennummer (nicht bei pitchbend)
    mode:      level  (Wert > threshold; Standard für cc/pitchbend)
               gate   (Note-On = an, Note-Off/Velocity 0 = aus; Standard für note)
               toggle (jedes Note-On schaltet um)
    threshold: Prozent 0..100 (Standard 2)
    invert:    true = Bedingung umkehren (z.B. Mute)
    port:      optional, nur Nachrichten von diesem Port (Teilstring)

Tipp: Mackie-Control-Feedback (Fader-Position, Mute-LEDs) kommt von der DAW
zum Controller. Am zuverlässigsten ist es, wenn die DAW das zusätzlich auf
einen virtuellen Port (loopMIDI / IAC-Bus) schickt, den dieses Tool liest.
Mit `python tally.py --midi-monitor` siehst du alle eingehenden Nachrichten.
"""
from __future__ import annotations

import asyncio
import collections
import time

from engine import TallyEngine


def _key(msg_type: str, channel: int, number: int | None, port: str) -> tuple:
    return (msg_type, channel, number, port)


class MidiSource:
    name = "MIDI"

    def __init__(self, cfg: dict, engine: TallyEngine, monitor: bool = False):
        import mido  # noqa: F401  (früh scheitern, wenn nicht installiert)
        self.mido = mido
        self.port_filters = cfg.get("ports") or []
        self.engine = engine
        self.monitor = monitor
        self.values: dict[tuple, float] = {}   # (type, ch, num, port) -> 0..1
        self.toggles: dict[tuple, bool] = {}
        self.open_ports: dict[str, object] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        # letzte Nachrichten für den MIDI-Monitor im Admin-Client
        self.log: collections.deque = collections.deque(maxlen=60)
        self.log_seq = 0

    def _wanted(self, name: str) -> bool:
        return not self.port_filters or any(f.lower() in name.lower() for f in self.port_filters)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        try:
            await self._run()
        finally:
            for port in self.open_ports.values():
                try:
                    port.close()
                except Exception:
                    pass
            self.open_ports.clear()

    async def _run(self) -> None:
        while True:
            try:
                available = self.mido.get_input_names()
            except Exception as exc:
                self.engine.set_status(self.name, False, f"MIDI-Backend: {exc}")
                await asyncio.sleep(5)
                continue
            # verschwundene Ports schließen
            for name in list(self.open_ports):
                if name not in available:
                    try:
                        self.open_ports.pop(name).close()
                    except Exception:
                        pass
                    print(f"[MIDI] Port weg: {name}")
            # neue Ports öffnen
            for name in available:
                if name not in self.open_ports and self._wanted(name):
                    try:
                        self.open_ports[name] = self.mido.open_input(
                            name, callback=lambda m, n=name: self.loop.call_soon_threadsafe(self._on_msg, m, n))
                        print(f"[MIDI] geöffnet: {name}")
                    except Exception as exc:
                        print(f"[MIDI] kann {name} nicht öffnen: {exc}")
            if self.open_ports:
                self.engine.set_status(self.name, True, ", ".join(self.open_ports))
            else:
                self.engine.set_status(self.name, False,
                                       f"kein passender Port (verfügbar: {', '.join(available) or '–'})")
            await asyncio.sleep(5)

    def _on_msg(self, msg, port: str) -> None:
        if self.monitor:
            print(f"[MIDI] {port}: {msg}")
        t = msg.type
        if t == "note_off":
            t, vel = "note", 0
        elif t == "note_on":
            t, vel = "note", msg.velocity
        ch = getattr(msg, "channel", 0) + 1
        rule = None
        if t == "note":
            rule = {"type": "note", "channel": ch, "number": msg.note, "mode": "gate"}
        elif t == "control_change":
            rule = {"type": "cc", "channel": ch, "number": msg.control, "threshold": 2}
        elif t == "pitchwheel":
            rule = {"type": "pitchbend", "channel": ch, "threshold": 2}
        if rule:
            self.log_seq += 1
            self.log.append({"seq": self.log_seq, "time": time.time(), "port": port,
                             "text": str(msg).replace(" time=0", ""), "rule": rule})
        if t == "note":
            k = _key("note", ch, msg.note, port)
            if vel > 0:
                self.toggles[k] = not self.toggles.get(k, False)
            self.values[k] = vel / 127
        elif t == "control_change":
            self.values[_key("cc", ch, msg.control, port)] = msg.value / 127
        elif t == "pitchwheel":
            self.values[_key("pitchbend", ch, None, port)] = (msg.pitch + 8192) / 16383
        else:
            return
        self._compute()

    def _lookup(self, store: dict, rule: dict, typ: str, ch: int, num):
        pf = rule.get("port")
        for (kt, kc, kn, kp), v in store.items():
            if kt == typ and kc == ch and kn == num and (not pf or pf.lower() in kp.lower()):
                return v
        return None

    def _rule_ok(self, rule: dict) -> bool:
        typ = rule.get("type", "cc")
        ch = int(rule.get("channel", 1))
        num = None if typ == "pitchbend" else int(rule.get("number", 0))
        mode = rule.get("mode", "gate" if typ == "note" else "level")
        thr = float(rule.get("threshold", 2)) / 100
        if mode == "toggle":
            ok = bool(self._lookup(self.toggles, rule, typ, ch, num))
        else:
            v = self._lookup(self.values, rule, typ, ch, num)
            v = v or 0.0
            ok = v > 0 if mode == "gate" else v > thr
        return (not ok) if rule.get("invert") else ok

    def _compute(self) -> None:
        result = {}
        for tid, t in self.engine.tallies.items():
            rules = t.get("midi")
            if not rules:
                continue
            if isinstance(rules, dict):
                rules = [rules]
            result[tid] = (all(self._rule_ok(r) for r in rules), False)
        self.engine.update(self.name, result)
