# 📱 Handy-Tally

Tally-Lichter auf dem Smartphone, in Python. Jedes Handy im WLAN öffnet eine Webseite
und wird **rot** (LIVE), **grün** (PREVIEW) oder dunkel. Es muss keine App installiert werden.

Mögliche Quellen, auch mehrere gleichzeitig:

| Quelle | Protokoll | Was wird erkannt |
|---|---|---|
| **OBS Studio** 28+ | obs-websocket v5 | Quelle sichtbar in Programm-/Vorschau-Szene (auch verschachtelte Szenen und Gruppen), Szene aktiv, Audio-Eingang nicht stumm, Stream-/Aufnahme-Status |
| **Radio-/DAW-Controller** | MIDI (CC, Note, Pitchbend / Mackie Control) | Fader offen, Mute aus, Taster an (frei kombinierbar) |
| **Yamaha DM3** | Yamaha RCP über TCP 49280 | Kanal ON, Fader über X dB, optional „To Stereo“ |

Ein Tally ist LIVE, sobald **eine** der Quellen ihn live meldet. Damit kann z. B. das
Moderations-Handy rot werden, wenn das Mikro am DM3 **oder** am DAW-Controller offen ist.

## Installation

```bash
cd handy_tally
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Unter Windows: `copy config.example.yaml config.yaml`. Wer kein MIDI braucht, kann
`mido`/`python-rtmidi` weglassen.

## Start

```bash
python tally.py            # normal
python tally.py --demo     # Testmodus ohne Hardware: die Tallys wechseln reihum
python tally.py --midi-monitor   # zeigt alle eingehenden MIDI-Nachrichten (zum Mappen)
```

Das Programm gibt die Adresse aus, z. B. `http://192.168.1.20:8080/`. Wenn `qrcode`
installiert ist, kommt ein QR-Code dazu. Am Handy öffnen, Tally auswählen, fertig.

- Direktlink für ein bestimmtes Handy: `http://IP:8080/?t=cam1`
- Übersicht aller Tallys (z. B. für Regie-Tablet): `http://IP:8080/?t=*`
- Antippen = Vollbild. Bei LIVE vibriert das Handy (Android).
- Wenn die Verbindung abreißt, erscheint ein schwarzer Bildschirm mit gestricheltem Rand
  und „OFFLINE“. So wird nie ein falsches Rot angezeigt.
- Ganz oben stehen die Quellen (grüner Punkt = verbunden) sowie **ON AIR**/**REC** von OBS.
- Zustand als JSON (für eigene Erweiterungen): `http://IP:8080/api/state`

> Firewall: Beim ersten Start fragt Windows nach dem Netzwerkzugriff für Python. Erlauben
> (privates Netzwerk), sonst erreichen die Handys den Server nicht.
> Display-Timeout am Handy hochsetzen. Die Seite versucht zwar die Wake-Lock-API, die
> funktioniert über HTTP aber nicht auf jedem Gerät.

## Einrichtung der Quellen

### OBS
In OBS unter *Werkzeuge → WebSocket-Server-Einstellungen* den Server aktivieren,
das Passwort in `config.yaml` unter `obs.password` eintragen. Pro Tally:

```yaml
obs:
  sources: ["Kamera 1"]     # Name der Quelle in OBS (exakt)
  scenes:  ["Interview"]    # optional: live, wenn diese Szene auf Programm ist
  audio:   ["Mikro 1"]      # optional: live, wenn der Audio-Eingang nicht stumm ist
```
Grün (PREVIEW) gibt es nur, wenn der **Studio-Modus** in OBS aktiv ist.

### Radio-/DAW-Controller (MIDI)
1. `python tally.py --midi-monitor` starten, Fader und Tasten bewegen und die Nachrichten ablesen.
2. Pro Tally eine Liste von Bedingungen eintragen. **Alle** Bedingungen müssen erfüllt sein:

```yaml
midi:
  - {type: pitchbend, channel: 1, threshold: 3}                     # Mackie-Fader 1 > 3 %
  - {type: note, channel: 1, number: 16, mode: gate, invert: true}  # Mute-LED Kanal 1 aus
```

Mögliche Modi: `level` (Wert > threshold %), `gate` (Note an/aus), `toggle` (jeder Tastendruck schaltet um).

**Mackie Control / HUI mit DAW:** Die echten Zustände (Fader-Position, Mute-LEDs) schickt die
DAW zum Controller. Am sichersten ist es, die Mackie-Ausgabe der DAW zusätzlich auf einen
virtuellen MIDI-Port zu legen: unter Windows z. B. **loopMIDI**, auf dem Mac den **IAC-Bus**.
Diesen Port dann unter `midi.ports` eintragen. Liest das Tool nur den Controller direkt,
bekommt es nur die Tastendrücke mit (dann `mode: toggle` verwenden).

Hinweis: Ein MIDI-Port lässt sich unter Windows oft nur von **einem** Programm gleichzeitig
öffnen. Deshalb ist der Umweg über loopMIDI empfehlenswert.

### Yamaha DM3
Das DM3 und den Rechner ins selbe Netz bringen (IP am Pult unter *Setup → Network*).
Das Tool spricht das Yamaha Remote Control Protocol auf **TCP 49280**. Pro Tally:

```yaml
dm3:
  channel: 1        # Kanalnummer wie am Pult
  kind: InCh        # InCh = Mono-Eingänge, StInCh = Stereo-Eingänge
  min_db: -60       # Fader muss darüber stehen
  to_stereo: false  # true = Kanal muss zusätzlich auf die Stereo-Summe geroutet sein
```

Das Pult meldet Änderungen sofort (NOTIFY). Zusätzlich wird jede Sekunde abgefragt
(`poll_interval`). Getestet wurde das Tool gegen einen Protokoll-Simulator, nicht an echter
Hardware. Falls dein DM3 eine Fehlermeldung (`ERROR …`) liefert, erscheint sie im Terminal.

## Aufbau

```
tally.py              Webserver (aiohttp) + Start der Quellen
engine.py             kombiniert die Quellen zum Tally-Zustand
sources/obs.py        obs-websocket v5 Client
sources/midi.py       MIDI via mido/python-rtmidi
sources/yamaha_dm3.py Yamaha RCP (TCP 49280)
static/index.html     Handy-Oberfläche
```

Eine neue Quelle (z. B. vMix, ATEM) braucht nur eine Klasse mit `async def run()`, die
`engine.update(name, {tally_id: (program, preview)})` und `engine.set_status(...)` aufruft.
