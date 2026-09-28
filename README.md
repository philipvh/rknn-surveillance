# rknn-surveillance

Offline surveillance for a Rockchip NPU board: YOLOv10 detection through the
RKNN runtime, PTZ control, a PIR contact on GPIO, and a wall panel that works
on an Android tablet old enough to be stuck on an ES5 browser.

Developed on a Radxa Rock 5B (RK3588) against a tennis club's vandalism
problem, but nothing here is specific to either. The model is a config value,
the board only has to run the RKNN runtime, and the site's name, trigger
classes and schedule are all settings.

Internet access is possible but not required. The panel is served from the
board over the local network and everything works with no uplink at all; where
there is no internet — as at this club — an optional point-to-point LoRa link
can still carry an alert to somebody's house, and that far end is the only part
that needs to be online.

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
git clone https://github.com/philipvh/rknn-surveillance
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

Two optional services, each installed once and configured from the panel
afterwards:

```bash
sudo bash forwarder.sh install   # publish device pages (Settings -> Forwarding)
sudo bash wan_meter.sh install   # count only the traffic that leaves the site
```

Neither is needed to record. Skip the meter if the uplink is not metered, and
skip the forwarder if nothing sits on a segment you cannot already reach.

To get notifications on a phone, subscribe the [ntfy](https://ntfy.sh) app to
a topic of your own and put it in `secrets.yaml`:

```bash
openssl rand -hex 16          # the topic is a password, not a name
```

```yaml
# secrets.yaml
notify:
  topic: "<that random string>"
```

Then set `notify.enabled: true` and `alerts.shadow_only: false`. Run
`./review.py` against a fortnight of shadow log first: it will tell you how
many notifications a night you are signing up for, before your phone does.

### Reaching the panel from outside

A phone on the site's own wifi needs nothing. From elsewhere there are three
shapes, and the board does not care which you pick:

* **A VPN you already run.** The panel is just a web page on the board.
* **Tailscale** — `sudo bash tailscale.sh install` then `up`. No ports, no
  DNS, no certificates, and a tailnet address is neither trusted nor
  directly attached, so a phone gets the password prompt and the metered
  picture without any configuration.
* **A public URL** — `tailscale.sh funnel on`, or a tunnel/reverse proxy of
  your own.

The last one needs `web.trusted_proxies` set first, and this is not optional.
Everything in front of the panel proxies from `127.0.0.1`, and three separate
decisions are taken from the client's address: whether a password is needed,
whether the picture costs money, and whose session is whose. Without it every
visitor is read as the proxy — one shared session, so the limit guards the
tunnel rather than the people, and full-quality video with no cap. `tailscale.sh`
refuses to turn Funnel on until it is set; a proxy of your own will not.

And resist the obvious shortcut when the password prompt gets annoying:
**do not add the proxy's address to the trusted networks.** That is the one
edit that turns a public URL into an open one.

## What it does

**Recording.** Two states. `ready` keeps one minute as pre-roll and throws the
rest away. A trigger — the detector or the PIR — switches to `triggered`, where
every minute is kept and an annotated still is written each second. A minute
with no trigger returns it to `ready`, at which point the minutes become one
clip and the sources are deleted. A clip is capped at ten minutes, and a
window longer than that is **split into consecutive clips** rather than
truncated -- keeping only the first ten minutes and letting the keeper reclaim
the rest cost this club four days of footage once, and there is a test named
after it.

**Triggering.** Configurable COCO classes, tickable from the panel without a
restart. Everything else is still detected and drawn dimmed on the live view,
so you can tell *not detecting* apart from *detecting things we don't care
about*.

**Reviewing.** A media browser with a film strip grouped by clip, collapsible
like a tree, and a resizable divider. Stills and video share one selected
moment, so switching tabs keeps your place.

**Alerting.** Five gates in order, cheapest and most decisive first: the armed
schedule, PIR corroboration, persistence (duration, sightings, confidence),
the class gate, and a rate limit. Every decision is written to a shadow log
with the reason it passed or failed — including the rejects, which is what
makes "it is quiet" distinguishable from "it is broken and quiet".

The class gate exists because cats and birds cross a tennis court at three in
the morning. Recording still triggers on the wider set — that footage is worth
keeping — while only `person` and vehicles are worth a phone buzzing. It
passes when *any* label qualifies, so a cat in frame cannot suppress the
person beside it.

**Notifying.** A push notification to a phone via [ntfy](https://ntfy.sh),
with the incident's still attached, downscaled first because this runs on a
mobile bundle — a 277 kB still goes out as 26 kB. The policy above decides
*whether*; `notify.py` only carries the verdict, so a second way out of the
building does not mean a second set of rules about when to use it. Nothing is
built at all while `alerts.shadow_only` is true. There is an optional LoRa
uplink too, with ChaCha20-Poly1305 and a replay guard, for a site with no
mobile signal.

On a public ntfy server the topic *is* the password — anyone who knows it can
read every alert and see the stills — so it lives in `secrets.yaml`, wants to
be long and random, and is never echoed in status output.

**Predicting the volume before switching it on.** The shadow log stores the
raw facts each decision was made from, not just the verdict, so `review.py`
can replay a fortnight against thresholds nobody has tried yet. That turned
"will this flood my phone?" into a number: 12 alerts over 37 days, 0.32 a
night, worst night 5. It also caught two things worth catching — that
`require_pir` was rejecting 636 of 710 incidents at a site with no PIR wired,
so nothing could ever have been sent; and that arming at 21:00 at weekends was
alerting on the club's own members, who play until 23:00.

The shadow log exists because the previous system at this club was abandoned
after it flooded everyone with false alarms — see
[the write-up](#the-write-up).

**Access.** Watching and changing are separate questions. Trusted networks
decide who can *watch* without a password; two independent switches decide
whether the **settings pages** and **manual control** still ask for one even
there. With either on, the panel shows a single *Sign in as manager* button in
place of Settings -- a short-lived signed cookie rather than a browser prompt,
because on an old tablet a Basic-auth box can surface out of an XHR mid-stream
and cannot be signed out of. It lapses after `web.manager_minutes` (30), so a
panel somebody unlocked and walked away from re-locks itself. Being on the VPN
does not bypass it; a lock the tunnel skipped would only stop people standing
in the room.

**Forwarding.** A camera or router on a segment you cannot route to, published
on a board port and configured from the panel (*Settings -> Forwarding*). It is
a Host-rewriting reverse proxy, not an iptables DNAT: a DNAT leaves the HTTP
payload alone, so it works only on devices that ignore `Host`. Measured here,
the camera returns 200 for any `Host` while the 4G router redirects everything
but its own address to an address the client cannot reach. Every entry is
checked against the networks the board is *directly attached to*, so a forward
cannot be pointed at the internet, at the far side of the tunnel, or at the
board itself.

**Metering.** On a mobile bundle, video is the only thing big enough to matter:
the overlay view runs about 0.8 GB an hour. Remote viewers get a smaller,
slower picture and a session that stops itself; viewers on the board's own
wiring get the full thing. Which networks count as "own wiring" is read from
the kernel's routing table, not configured -- tunnels excluded, because a
viewer down the tunnel is exactly who costs money. Usage is counted with
iptables counters that skip every directly-attached subnet, so the wall panel's
own video is not billed as mobile data.

## Layout

| | |
|---|---|
| `surveillance_main.py` | wiring: everything is constructed here |
| `surveillance_core.py` | the detection loop |
| `controller.py` | the state machine; the only thing that moves the camera |
| `capture.py` `concat_mgr.py` `annotated.py` | what is kept, and how clips are made |
| `webapp.py` `templates/` | the panel and the media browser |
| `ptz.py` `tracker.py` | camera control: deadlines, motor budget, watchdog |
| `camera/` | one file per camera make -- the only place a vendor protocol lives |
| `alerts.py` `review.py` | the gates, the shadow log, and replaying it |
| `notify.py` | the push notification, and the still that goes with it |
| `link.py` `uplink.py` `transports.py` `receiver.py` | the radio link |
| `settings.py` `settings_cli.py` | panel-editable settings, and the shell rescue |
| `netinfo.py` | which networks this board is on, read from the routing table |
| `forwarder.py` `forwarder.sh` | device web pages published on board ports |
| `datausage.py` `wan_meter.sh` | the mobile bundle, counting only what leaves the site |
| `doctor.py` | one command that says whether this install is healthy |
| `setup_network.sh` `wan_ports.sh` `tailscale.sh` | the camera segment, and reaching the board from outside (`camera_ui.sh` is superseded by the forwarder) |

## Cameras

The camera's protocol is isolated in `camera/`. Everything else -- the state
machine, the panel, recording -- is written against one small interface, so a
different camera is a subclass rather than a fork.

```yaml
camera:
  type: foscam        # a backend in camera/, or yourpkg.module:YourBackend
  host: 192.168.1.50
  user: admin
```

Each backend supplies the defaults for its make (ports, stream paths), so a
config usually carries only what differs.

### Adding one

```python
from camera import Cap, CameraBackend, register

class AxisBackend(CameraBackend):
    name       = "axis"
    HTTP_PORT  = 80
    RTSP_PORT  = 554
    MAIN_PATH  = "axis-media/media.amp"
    SUB_PATH   = "axis-media/media.amp?resolution=640x480"
    CAPABILITIES = {Cap.PRESETS, Cap.ZOOM, Cap.SNAPSHOT}

    def start_move(self, direction): ...     # begin moving, return at once
    def stop(self, kind=None, timeout=None): ...   # stop, or raise
    # presets, zoom, snapshot, clock: implement what the camera has

register(AxisBackend)
```

Three methods is a working camera. Everything else is optional and raises
`NotSupported`, which callers use to hide a control rather than to show an
error -- so a fixed camera with no presets degrades honestly instead of
failing at runtime.

### The rule worth knowing before you start

**A backend starts and stops motion. It never decides when.**

Deadlines, the motor budget, the retry-until-confirmed stop, the rescue stop
at startup and the watchdog all live in `ptz.py`, above the interface, and
apply to every backend equally. A backend that stopped itself -- "I'll move
for two seconds" -- would be outside the watchdog, and a camera outside the
watchdog is one that can be left grinding against its end stop when a process
dies. `tests/test_camera.py::TestSomeoneElsesCamera` is the guard: a backend
that has never heard of `ptz.py` still gets the watchdog and the budget, and
that test failing means the abstraction has become a way to bypass the safety
layer instead of a way to inherit it.

`stop()` carries the one hard obligation: stop the camera or raise. Returning
quietly when the camera did not hear you is the worst thing a backend can do,
because the layer above treats a clean return as proof and stops retrying.

### Capabilities, and being honest about them

Cameras differ in ways callers care about. The shipped Foscam backend declares
that it *cannot* report absolute position -- `getPTZAbsolutePos` and friends
answer `result=-3` -- so anything wanting to draw a compass knows not to try.
Declare what yours can do and let the rest raise.


## Tests

```bash
python3 -m unittest discover -s tests
```

786 of them, no network and no hardware required. They are written against
*behaviour* rather than implementation, and several exist because the thing
they describe actually happened on the board — a sweep that deleted footage a
queued cut still needed, an incident that only lived in memory, a clip cap
that discarded four days rather than splitting them, a recorder that died at
midnight because a tidy-up removed the directory it was about to write to, a
browser that ignores `scrollIntoView` options. Those are the ones worth
reading first.

Each one is checked by breaking the code it covers and confirming it fails.
That habit has paid for itself repeatedly: an "expired cookie is refused" test
passed with the expiry check deleted, because it had also broken the signature
and was being rejected for the wrong reason; a "the wall tablet is never cut
off" test opened its first stream from the remote address and checked the
local one, so it passed with the exemption removed. Both were rewritten.

## The write-up

A longer piece on the design decisions, the false-alarm problem, and eighteen
defects that only appeared on real hardware is published separately.

## Licence

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

`yolov10.py` is derived from Rockchip's `rknn_model_zoo` and carries its own
attribution; it remains under the same licence.

Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands.
