# The wall panel (Galaxy Tab S)

The panel is served by the `rknn-surveillance` service itself, on the port in
`web.port` -- **8081** on the club board, because 8080 is forwarded to the
camera's own interface.

It runs inside the surveillance process on purpose: it then drives the same
PTZ object as the automatic controller, so every button obeys the same
deadline watchdog and the same motor budget. There is no way to move the
camera from the panel that the safety logic does not see.

## How the tablet reaches it

There is one radio and it does one job at a time, so there are two answers
depending on what `wifi_mode.sh` was last told:

* **`ap`** -- the board *is* the network. It serves `tvw-camera` on
  192.168.92.1, hands out addresses, and resolves the name itself. This is the
  normal state at the club, and the reason the panel has to work with no
  internet at all.
* **`client`** -- the radio is on someone else's network (a phone hotspot,
  typically, to feed the tunnel for remote work). The tablet then has to be on
  a network that can route to the board, and the AP is gone while this lasts.

**Use the name, not an address, in both.** The old code had an IP baked into a
template and that is exactly the failure not to repeat: a bookmark that breaks
after a router reboot is a panel nobody uses. In `ap` mode `wifi_mode.sh`
writes the name into NetworkManager's shared dnsmasq; on the wired camera
segment `setup_network.sh` does the same. Either way the kiosk URL is:

    http://panel:8081/

Verified against the AP's own resolver: `panel` and `panel.local` both answer
192.168.92.1.

### When the tablet is on the 4G router's wifi instead

The board's AP runs on an RTL8188CUS -- 1x1, 2.4 GHz, and the `rtl8192cu`
driver buffers for a sleeping client, which shows up as latency that alternates
between a couple of milliseconds and a full second:

    ping every 0.2s (continuous)   avg  8.0 ms   max   14 ms
    ping every 1.0s                alternating 2 ms / 1000 ms
    ping every 3.0s                295 -> 447 ms, climbing

Under continuous traffic it is fine; every gap is an invitation to doze, and
`idle_fps: 1` sits exactly at the wake interval. If that makes the panel crawl,
put the tablet on the 4G router's wifi instead. Three things follow:

* **Forget the board's AP on the tablet.** Selecting the other network is not
  enough -- the AP shares the board's uplink, so to Android it is a perfectly
  good network with internet, at full signal, with autoconnect priority 100.
  It will go back to it.
* **The URL does not change to the tablet's address.** It stays the *board's*
  address on that LAN (`http://192.168.8.172:8081`). The tablet's own address
  is what the trusted-network check sees, and they are different machines.
* **Reserve the tablet's address** on the router, and set the tablet's wifi
  privacy to *Use device MAC* first -- Android's per-SSID random MAC changes
  when you forget and rejoin, and the reservation would then point at nothing.

The tablet's own video now crosses the uplink NIC on its way back, which is
why usage is counted with iptables counters that skip directly-attached
subnets rather than by reading the interface totals. Without that it would
book 10-19 GB a day of mobile data that never left the building.

### Which mode comes back after a power cut

Whichever one was chosen last. `wifi_mode.sh` writes the decision into
NetworkManager's autoconnect -- the chosen connection gets priority 100 and
the others are turned off -- because autoconnect is the only thing consulted
at boot. Before this the access point was `autoconnect no`, so a power cut at
the club would have handed the radio to a saved hotspot and left the wall
tablet with no panel and no way back short of ssh.

Current state on the board: `rknn-ap` is `yes:100`, the saved client
network is `no:0`, and the wired connection is untouched.

**This has not been through an actual reboot yet** -- there is no reboot in
the privileged helper's action list, by design. Worth confirming once:

    sudo reboot
    # when it comes back
    bash ~/rknn-surveillance/wifi_mode.sh status     # expect: ACCESS POINT

## Access mode

Settings -> Credentials offers three modes, and for a wall tablet the middle
one is right:

* *Always* -- the kiosk browser must store the password.
* *Not on our own network* -- no password from the listed networks, one
  required from anywhere else. The tablet never prompts; a phone somewhere
  else still cannot get in.
* *Nobody* -- no password at all, from anywhere, including the tunnel.

**The board is set to the middle one**, trusting the AP subnet, the camera
segment, the lab LAN and the tunnel subnet:

    192.168.92.0/24  192.168.91.0/24  192.168.90.0/24  10.8.2.0/24

So the kiosk stores no credentials, and the login still stands for anything
arriving from outside those. Narrow the list from the panel if the lab LAN or
the tunnel should not be on it. If a change ever locks you out, ssh in and use
`./settings_cli.py trusted <net>` or `./settings_cli.py password`.

### Watching is not the same as changing

Trusted networks answer *who can watch without a password*. They are the wrong
answer for *who can change things*: the tablet hangs in a room anyone at the
club can walk into, and from the panel you can re-aim the camera, redefine
where the sweep stops, publish a device on a board port, or set a new password.

Two switches on the same screen separate the two, and they are independent
because the risks are -- a club might want a committee member able to swing the
camera without also handing them the page that sets passwords:

* **Always ask for the password on these Settings pages**
* **Always ask for the password for manual control** -- the Manual tab: moving
  the camera, and setting where the sweep stops

Neither touches watching. Both apply **everywhere, the tunnel included**: a
lock the VPN skipped would only stop people standing in the room, which is not
the threat.

With either on, the View tab replaces the Settings button with **Sign in as
manager**, and the Manual tab is hidden rather than left there doing nothing.
Signing in unlocks both, shows the minutes remaining, and offers Sign out.

    Stills & clips
    Sign in as manager      <- unlocks the Manual tab and Settings

It is a short-lived signed cookie, not a browser prompt. A Basic-auth box on
this tablet can surface out of an XHR in the middle of a video stream, and
there is no way out of it short of closing the browser. The cookie carries an
expiry and an HMAC of that expiry; the key never leaves the board. It lapses
after `web.manager_minutes` (30) so a panel somebody unlocked and walked away
from re-locks itself, and it is **per origin** -- signing in on your laptop
does not sign in the wall tablet.

From the shell:

    ./settings_cli.py protect on both      # settings and manual
    ./settings_cli.py protect off manual   # just the Manual tab again
    ./settings_cli.py show                 # each flag, beside its undo

It refuses to switch on when no password is set, because the first screen it
would lock is the one where you would set one.

**Do not put the panel password into the kiosk browser** if you want these to
mean anything. Stored credentials satisfy both prompts automatically, and the
tablet is back to being a device anyone can change the system from. Trust the
tablet by address instead (below) and keep the password as the thing a human
types.

## Browser

The Tab S stopped receiving updates, so check what it actually has:
Settings -> About tablet -> Android version. The panel is written to an ES5
floor -- no arrow functions, no `fetch`, no template literals, `XMLHttpRequest`
throughout, flexbox rather than grid, and MJPEG rather than WebRTC -- so it
should work from Android 5 upwards. **Test on the tablet itself early**, not
on a laptop: which video path survives is the whole question.

If the live view is choppy, try `web.stream_mode: ffmpeg` in `config.yaml`.
That transcodes the sub-stream instead of polling the camera's JPEG endpoint:
smoother, at the cost of one ffmpeg process per viewer.

## Kiosk setup

**Install Fully Kiosk Browser while the tablet still has internet.** Once it is
on the access point there may be none, and an app store that cannot reach
anything is a bad moment to find that out. Do the install, the sign-out and the
clean-up in that order, then join `tvw-camera`.

Chrome was considered and does not fit: stock Android Chrome has no kiosk
mode, cannot be started at boot, and will not install the panel as a
standalone app because that needs HTTPS and the panel is plain HTTP on a LAN.
It would leave an address bar on the wall.

Settings names drift between Fully Kiosk versions; these are the ones that
matter, by what they do:

1. **Start URL** -> `http://panel:8081/`
   If it does not load, the name is the thing to suspect first -- fall back to
   `http://192.168.92.1:8081/` and see the resolver note above.
2. **Launch on boot**, and **relaunch on crash**. This is the whole reason for
   using a kiosk browser rather than Chrome.
3. **Set as Home app.** The strongest single lockdown: the home button returns
   to the panel instead of leaving it, and boot goes straight there.
4. **Kiosk mode on, with an exit PIN.** Write the PIN down somewhere that is
   not the tablet.
5. **Screensaver** after a few minutes, wake on touch. If the version in use
   can wake on motion from the front camera, a panel by the door is exactly
   the case that suits.
6. **Keep screen on while in use**, brightness low.
7. **Reload on network reconnect**, so a board restart does not leave an error
   page on the wall.
8. Address bar, pull-down notifications and gestures off.

Some of these sit behind the paid Plus licence depending on version -- check
before assuming a feature is missing.

Then in Android itself, outside Fully Kiosk:

* **Screen lock: None** (Settings -> Security), or boot stops at a lock screen
  nobody at the club can pass.
* **Stay awake while charging** (Developer options), as a second line under
  Fully Kiosk's own screen handling.

There are no credentials to store: the access mode above trusts the access
point subnet, so the panel does not ask.

### Android and a network with no internet

In `ap` mode the board shares whatever uplink it has, which at the club is
none. Android notices, marks the network "no internet", and on some versions
offers to leave it. The Tab S has no mobile data to fall back to, so it stays
put -- but if a dialog appears, tell it to stay connected, and turn off
"switch to mobile data automatically" if the setting exists. Fully Kiosk
itself does not care whether the network reaches the internet.

## Two things that kill wall-mounted tablets

**A tablet that browses.** Clear it before mounting: sign out of the Google
account, remove mail and photos, and take everything off the home screen. A
wall panel by a clubhouse door is a device other people will pick up, and it
should hold nothing but this one page. Fully Kiosk's own settings then stop
anyone leaving that page.

**Battery swelling.** An older tablet held at 100% permanently will swell, and
a swelling battery behind a bracket is a hazard, not an inconvenience. Put the
charger on a timer plug or a smart plug and cycle it — a couple of hours a day
is ample for a device that never leaves the wall. Look at it occasionally.

**Burn-in.** The Tab S screens are AMOLED and a static interface displayed
continuously will ghost into them. The panel is dark for that reason; low
brightness helps more; the screensaver is the real fix.

## Security

The tablet is unpatched, so what it can reach matters.

It is not sealed off. `ipv4.method shared` means NetworkManager masquerades
the access point subnet out through whatever uplink the board has, so the
tablet reaches exactly what the board reaches. At the club that is nothing,
which is why this is acceptable. On the bench, where the board sits on the lab
LAN with a default route, the tablet is on the internet -- so do the app
installs and the sign-out there, and do not leave an unpatched tablet parked
on that segment longer than the setup takes.

(The firewall rules behind this were not inspected: reading them needs root
and `sudo -n` is not granted on the board. The behaviour above is what
`ipv4.method shared` is defined to do, not something observed here.)

The panel's password is not there to stop attackers so much as to stop
accidents; the club wifi is not a trust boundary. What *is* worth taking
seriously is physical access to the tablet, which is why manual control and
the settings pages can be held behind a sign-in even on a trusted network --
see [Watching is not the same as changing](#watching-is-not-the-same-as-changing).

If the tablet is moved onto the club wifi, resist trusting that whole subnet:
anyone who knows the wifi password would then reach the panel with no login.
Trust the tablet's reserved address alone (`192.168.8.x/32`) and keep a
password for everything else -- and know what that buys, which is "possession
of an address" as the credential. Anyone on that wifi could take the address
while the tablet is off. It raises the bar; it is not a wall.

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
* **Data used**: today's figure against the bundle, on the View tab. Counts
  only what actually crossed the mobile link.
* **Sign in as manager**: shown in place of Settings when the settings pages
  or manual control are protected. Unlocks both for 30 minutes.
