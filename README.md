# rknn-surveillance

Offline surveillance for a Rockchip NPU board: YOLOv10 detection through the
RKNN runtime, PTZ control, a PIR contact on GPIO, and a wall panel that works
on an Android tablet old enough to be stuck on an ES5 browser.

Developed on a Radxa Rock 5B (RK3588) against a tennis club's vandalism
problem, but nothing here is specific to either. The model is a config value,
the board only has to run the RKNN runtime, and the site's name, trigger
classes and schedule are all settings.

It runs offline. The club has no internet, so there is no cloud, no app and no
account — the panel is served from the board over the club LAN, and an optional
point-to-point LoRa link can carry an alert to somebody's house.

> **This is a hobby project that runs at one tennis club.** It aids
> surveillance; it does not guarantee it. See [NOTICE](NOTICE) before relying
> on it for anything.

## Why it might be useful to you

Most of this is not tennis-specific. If you are putting a camera on an RK3588
board, these parts are worth stealing:

- **`yolov10.py` + `preprocess.py`** — running the model zoo's YOLOv10 export
  without the demo scaffolding, and letterboxing 16:9 into 640×640 rather than
  squashing it. Squashing makes a standing person short and wide; on one test
  image it found two people where letterboxing found three.
- **`capture.py`** — a two-state recording model that keeps footage
  proportional to *events* rather than to *time*. In `ready`, each new minute
  drops the previous one. In `triggered`, everything is kept and the minutes
  are concatenated into one clip when the event ends.
- **`concat_mgr.py`** — joining ffmpeg segments with `-c copy` (no re-encode),
  validating the result before deleting the sources, and putting the `moov`
  atom at the front so a tablet can start playing without fetching the whole
  file.
- **`ptz.py`** — driving a camera whose move commands run until stopped.
  Four independent layers of defence against leaving the motors running,
  because the failure mode is a motor that burns out overnight.
- **`health.py`** — a systemd watchdog ping that is *conditional on frames
  still arriving*. A watchdog that always pings never fires.

## Hardware

| | |
|---|---|
| Board | Radxa Rock 5B (RK3588), 8 GB |
| Model | YOLOv10s, converted to `.rknn` for the NPU |
| Camera | Foscam SD2X (PTZ dome) — developed against an INSTAR IN-8415 |
| Trigger | PIR floodlight sensor, dry contact to GPIO |
| Panel | Samsung Galaxy Tab S, wall-mounted |
| Storage | separate ext4 volume; recordings never share the root filesystem |

## Getting it running

```bash
git clone <this repo> rknn-surveillance
cd rknn-surveillance

cp config.local.example.yaml config.local.yaml   # your camera's address
cp secrets.example.yaml secrets.yaml             # camera + panel passwords
chmod 600 secrets.yaml

# The model is not in the repo: it is Rockchip's, and 16 MB. Convert
# yolov10s.onnx with the rknn toolkit, or copy model/yolov10.rknn from
# https://github.com/airockchip/rknn_model_zoo (examples/yolov10).
mkdir -p model && cp /path/to/yolov10.rknn model/

./install.sh        # packages, systemd unit, journal cap
./doctor.py         # checks the config, the camera, the disk and the clock
```

The panel is then on `http://<board>:8081/`.

`./deploy.sh user@board --watch --restart` re-syncs on every save while you
work on it, and restarts the service.

## What it does

**Recording.** Two states. `ready` keeps one minute as pre-roll and throws the
rest away. A trigger — the detector or the PIR — switches to `triggered`, where
every minute is kept and an annotated still is written each second. A minute
with no trigger returns it to `ready`, at which point the minutes become one
clip and the sources are deleted. Clips are capped at ten minutes.

**Triggering.** Configurable COCO classes, tickable from the panel without a
restart. Everything else is still detected and drawn dimmed on the live view,
so you can tell *not detecting* apart from *detecting things we don't care
about*.

**Reviewing.** A media browser with a film strip grouped by clip, collapsible
like a tree, and a resizable divider. Stills and video share one selected
moment, so switching tabs keeps your place.

**Alerting.** An alert policy with a persistence gate and a schedule, a shadow
log that records what *would* have been sent, and an optional LoRa uplink with
ChaCha20-Poly1305 and a replay guard. The shadow log exists because the
previous system at this club was abandoned after it flooded everyone with
false alarms — see [the write-up](#the-write-up).

## Layout

| | |
|---|---|
| `surveillance_main.py` | wiring: everything is constructed here |
| `surveillance_core.py` | the detection loop |
| `controller.py` | the state machine; the only thing that moves the camera |
| `capture.py` `concat_mgr.py` `annotated.py` | what is kept, and how clips are made |
| `webapp.py` `templates/` | the panel and the media browser |
| `ptz.py` `tracker.py` | camera control |
| `link.py` `uplink.py` `transports.py` `receiver.py` | the radio link |
| `settings.py` `settings_cli.py` | panel-editable settings, and the shell rescue |
| `doctor.py` | one command that says whether this install is healthy |

## Tests

```bash
python3 -m unittest discover -s tests
```

470 of them, no network and no hardware required. They are written against
*behaviour* rather than implementation, and several exist because the thing
they describe actually happened on the board — a sweep that deleted footage a
queued cut still needed, an incident that only lived in memory, a browser that
ignores `scrollIntoView` options. Those are the ones worth reading first.

## The write-up

A longer piece on the design decisions, the false-alarm problem, and eighteen
defects that only appeared on real hardware is published separately.

## Licence

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

`yolov10.py` is derived from Rockchip's `rknn_model_zoo` and carries its own
attribution; it remains under the same licence.

Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands.
