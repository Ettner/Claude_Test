"""Yamaha DM3 über das Yamaha Remote Control Protocol (RCP, TCP-Port 49280).

Dasselbe Textprotokoll nutzen auch Bitfocus Companion & Co. für DM3/DM7/CL/QL/TF.
Am Pult muss nichts aktiviert werden – nur Netzwerk (IP) einrichten.

Global:
    dm3:
      enabled: true
      host: 192.168.0.128
      port: 49280

Pro Tally:
    dm3:
      channel: 1        # Kanalnummer wie am Pult (1-basiert)
      kind: InCh        # InCh (Mono-Eingänge) oder StInCh (Stereo-Eingänge)
      min_db: -60       # Fader muss über diesem Wert stehen (Standard -60 dB)
      to_stereo: false  # true = zusätzlich muss "To Stereo" an sein

Ein Kanal gilt als LIVE, wenn er eingeschaltet (ON) ist und der Fader über min_db steht.
"""
from __future__ import annotations

import asyncio

from engine import TallyEngine

NEG_INF = -32768  # RCP-Wert für -∞ dB (Einheit: 0.01 dB)


class DM3Source:
    name = "DM3"

    def __init__(self, cfg: dict, engine: TallyEngine):
        self.host = cfg.get("host", "192.168.0.128")
        self.port = int(cfg.get("port", 49280))
        self.poll = float(cfg.get("poll_interval", 1.0))
        self.engine = engine
        self.values: dict[tuple[str, int], int] = {}

    def _params(self, t: dict) -> list[tuple[str, int]]:
        d = t.get("dm3") or {}
        if "channel" not in d:
            return []
        kind = d.get("kind", "InCh")
        idx = int(d["channel"]) - 1
        p = [(f"MIXER:Current/{kind}/Fader/On", idx), (f"MIXER:Current/{kind}/Fader/Level", idx)]
        if d.get("to_stereo"):
            p.append((f"MIXER:Current/{kind}/ToSt/On", idx))
        return p

    def _all_params(self) -> set[tuple[str, int]]:
        out = set()
        for t in self.engine.tallies.values():
            out.update(self._params(t))
        return out

    async def run(self) -> None:
        if not self._all_params():
            print("[DM3] keine Tallys mit 'dm3' konfiguriert")
        while True:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 5)
                print(f"[DM3] verbunden mit {self.host}:{self.port}")
                self.engine.set_status(self.name, True, f"{self.host}:{self.port}")
                poller = asyncio.create_task(self._poller(writer))
                try:
                    await self._reader(reader)
                finally:
                    poller.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.engine.set_status(self.name, False, f"{self.host}:{self.port}: {exc}")
                print(f"[DM3] keine Verbindung ({exc}) – neuer Versuch in 3 s")
            finally:
                self.values.clear()
                if writer:
                    writer.close()
            await asyncio.sleep(3)

    async def _poller(self, writer: asyncio.StreamWriter) -> None:
        # Das Pult schickt bei Änderungen NOTIFY-Zeilen; Pollen ist Fallback + Keepalive.
        while True:
            for addr, idx in sorted(self._all_params()):
                writer.write(f"get {addr} {idx} 0\n".encode())
            await writer.drain()
            await asyncio.sleep(self.poll)

    async def _reader(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await asyncio.wait_for(reader.readline(), self.poll * 5 + 5)
            if not line:
                raise ConnectionError("Pult hat die Verbindung geschlossen")
            self.handle_line(line.decode(errors="replace").strip())

    def handle_line(self, line: str) -> None:
        # Beispiele:
        #   OK get MIXER:Current/InCh/Fader/On 0 0 1
        #   NOTIFY set MIXER:Current/InCh/Fader/Level 3 0 -1000 "-10.00"
        #   ERROR get MIXER:Current/InCh/Fader/On 40 0 InvalidArgument
        parts = line.split()
        if len(parts) < 6 or parts[0] not in ("OK", "NOTIFY", "OKm") or parts[1] not in ("get", "set"):
            if parts and parts[0] == "ERROR":
                print(f"[DM3] {line}")
            return
        try:
            addr, idx, val = parts[2], int(parts[3]), int(parts[5])
        except ValueError:
            return
        if self.values.get((addr, idx)) != val:
            self.values[(addr, idx)] = val
            self._compute()

    def _compute(self) -> None:
        result = {}
        for tid, t in self.engine.tallies.items():
            params = self._params(t)
            if not params:
                continue
            d = t["dm3"]
            on = self.values.get(params[0]) == 1
            level = self.values.get(params[1], NEG_INF)
            live = on and level > float(d.get("min_db", -60)) * 100
            if d.get("to_stereo"):
                live = live and self.values.get(params[2]) == 1
            result[tid] = (live, False)
        self.engine.update(self.name, result)
