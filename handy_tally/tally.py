#!/usr/bin/env python3
"""Handy-Tally: Tally-Lichter auf dem Smartphone für OBS, MIDI-Controller und Yamaha DM3.

Start:   python tally.py              (liest config.yaml)
         python tally.py --demo       (Testmodus ohne Hardware)
         python tally.py --midi-monitor
Dann am Handy (gleiches WLAN) die angezeigte Adresse öffnen.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import socket
import sys
from pathlib import Path

import yaml
from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from engine import TallyEngine  # noqa: E402


# ---------------------------------------------------------------- Demo-Quelle
class DemoSource:
    """Schaltet die Tallys reihum – zum Testen der Handys ohne OBS/Pult."""
    name = "Demo"

    def __init__(self, engine: TallyEngine):
        self.engine = engine

    async def run(self) -> None:
        ids = list(self.engine.tallies)
        self.engine.set_status(self.name, True, "Testmodus")
        for i in itertools.count():
            prog = ids[i % len(ids)]
            prev = ids[(i + 1) % len(ids)]
            self.engine.update(self.name, {t: (t == prog, t == prev and t != prog) for t in ids})
            await asyncio.sleep(3)


# ---------------------------------------------------------------- Webserver
def make_app(engine: TallyEngine) -> web.Application:
    clients: set[web.WebSocketResponse] = set()

    async def broadcast(snap: dict) -> None:
        data = json.dumps(snap)
        for ws in list(clients):
            try:
                await ws.send_str(data)
            except Exception:
                clients.discard(ws)

    engine.subscribe(broadcast)

    async def index(_req):
        return web.FileResponse(HERE / "static" / "index.html")

    async def api_state(_req):
        return web.json_response(engine.snapshot())

    async def ws_handler(req):
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(req)
        clients.add(ws)
        await ws.send_str(json.dumps(engine.snapshot()))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT and msg.data == "ping":
                    await ws.send_str('{"type":"pong"}')
        finally:
            clients.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/state", api_state)
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
    print("\n  Handy-Tally läuft. Am Handy im selben WLAN öffnen:")
    urls = [f"http://{ip}:{port}/" for ip in local_ips()]
    for u in urls:
        print(f"    {u}")
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
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        sys.exit(f"Config '{cfg_path}' nicht gefunden. Kopiere config.example.yaml nach config.yaml.")
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    engine = TallyEngine(cfg.get("tallies") or [])
    if not engine.tallies:
        sys.exit("Keine Tallys in der Config definiert (Abschnitt 'tallies').")

    tasks = [asyncio.create_task(engine.run())]
    sources = []
    if args.demo:
        sources.append(DemoSource(engine))
    else:
        if (cfg.get("obs") or {}).get("enabled"):
            from sources.obs import OBSSource
            sources.append(OBSSource(cfg["obs"], engine))
        if (cfg.get("midi") or {}).get("enabled") or args.midi_monitor:
            try:
                from sources.midi import MidiSource
                sources.append(MidiSource(cfg.get("midi") or {}, engine, monitor=args.midi_monitor))
            except ImportError as exc:
                print(f"[MIDI] deaktiviert – bitte 'pip install mido python-rtmidi' ({exc})")
        if (cfg.get("dm3") or {}).get("enabled"):
            from sources.yamaha_dm3 import DM3Source
            sources.append(DM3Source(cfg["dm3"], engine))
    if not sources:
        print("Hinweis: keine Quelle aktiviert (obs/midi/dm3 'enabled: true' oder --demo).")
    tasks += [asyncio.create_task(s.run(), name=s.name) for s in sources]

    srv = cfg.get("server") or {}
    host, port = srv.get("host", "0.0.0.0"), int(args.port or srv.get("port", 8080))
    runner = web.AppRunner(make_app(engine))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    print_urls(port)
    engine._dirty.set()
    try:
        await asyncio.gather(*tasks)
    finally:
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
