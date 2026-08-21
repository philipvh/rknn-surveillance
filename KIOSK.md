# The wall panel (Galaxy Tab S)

The panel is served by the `tvw-surveillance` service itself, at
`http://<rock5b>:8080/`. Log in with the user in `config.yaml`
(`web.auth_user`, default `tvw`) and the password from `secrets.yaml`.

It runs inside the surveillance process on purpose: it then drives the same
PTZ object as the automatic controller, so every button obeys the same
deadline watchdog and the same motor budget. There is no way to move the
camera from the panel that the safety logic does not see.

## Before anything else

Give the Rock 5B a **fixed address or an mDNS name**. The old code had an IP
baked into a template and that is exactly the failure not to repeat. A
bookmark that breaks after a router reboot is a panel nobody uses.

## Browser

The Tab S stopped receiving updates, so check what it actually has:
Settings → About tablet → Android version. The panel is written to an ES5
floor — no arrow functions, no `fetch`, no template literals, `XMLHttpRequest`
throughout, flexbox rather than grid, and MJPEG rather than WebRTC — so it
should work from Android 5 upwards. **Test on the tablet itself early**, not
on a laptop: which video path survives is the whole question.

If the live view is choppy, try `web.stream_mode: ffmpeg` in `config.yaml`.
That transcodes the sub-stream instead of polling the camera's JPEG endpoint:
smoother, at the cost of one ffmpeg process per viewer.

## Kiosk setup

**Fully Kiosk Browser** handles all of this on old Android:

* Start URL `http://<rock5b>:8080/`, and store the credentials so it does not
  prompt.
* **Start on boot** and **restore on crash**.
* **Screensaver**: blank the screen after a few minutes, wake on touch. Fully
  Kiosk can also wake on motion from the front camera, which for a panel by
  the door is exactly right.
* **Keep screen on while in use**, brightness low.
* Disable pull-down notifications and the address bar.

## Two things that kill wall-mounted tablets

**Battery swelling.** An older tablet held at 100% permanently will swell, and
a swelling battery behind a bracket is a hazard, not an inconvenience. Put the
charger on a timer plug or a smart plug and cycle it — a couple of hours a day
is ample for a device that never leaves the wall. Look at it occasionally.

**Burn-in.** The Tab S screens are AMOLED and a static interface displayed
continuously will ghost into them. The panel is dark for that reason; low
brightness helps more; the screensaver is the real fix.

## Security

The tablet is unpatched, which is fine only because it never touches the
internet. Keep it that way — if the radio uplink is ever added, do not route
the tablet through it. The panel's password is not there to stop attackers so
much as to stop accidents; the club wifi is not a trust boundary.

## What the panel does

* **Idle**: live view large, current state, armed/disarmed, PIR activity.
* **Held direction button**: moves while held, stops on release. If the
  release never arrives — tablet asleep, wifi dropped, browser suspended —
  the server stops the motors on its own deadline about a third of a second
  later. The browser-side stop is belt; that deadline is braces.
* **Presets**: Home first, then the court views.
* **Scan now**: runs a sweep as though the PIR had fired.
* **Disarm 2h / Arm now / Back to schedule**: alerting overrides that expire
  on their own, so nobody can leave the system silenced permanently.
* **Recordings**: event clips first, newest first.
