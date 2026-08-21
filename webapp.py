# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. This
# software aids surveillance; it does not guarantee it, and no liability is
# accepted for any failure to detect, record, retain or report an event.
# See the NOTICE file for the full disclaimer.

"""The wall panel and the media browser.

Served from inside the surveillance process so it holds the same PTZ object as
the controller: the same deadline watchdog, the same motor budget. There is no
route here that can move the camera in a way the safety logic does not see.

Built for a Galaxy Tab S whose browser stopped updating years ago, so the
front end is ES5 with XHR and flexbox, and the live view is MJPEG rather than
WebRTC. An <img> fed a multipart stream works in browsers far older than
anything else under discussion.
"""

import datetime as dt
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template, request, send_from_directory, url_for)

log = logging.getLogger("web")

BASE_DIR = Path(__file__).resolve().parent
# Our own multipart boundary for snapshot mode.
BOUNDARY = "rknnframe"
# ffmpeg's mpjpeg muxer writes its own, and the Content-Type must match it or
# the browser sees one endless malformed part. Verified against ffmpeg 8.
FFMPEG_BOUNDARY = "ffmpeg"


def _unauthorised(realm="RKNN surveillance"):
    # The realm is what the browser's password box is labelled with, so it
    # should say which installation is asking.
    safe = str(realm).replace('"', "'")
    return Response(
        "Authentication required.", 401,
        {"WWW-Authenticate": 'Basic realm="%s"' % safe})


def create_app(cfg, ptz=None, controller=None, schedule=None, health=None,
               announcer=None, live=None, concat=None, settings=None):
    """ptz/controller may be None -- the app then serves recordings only."""
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
                static_folder=str(BASE_DIR / "static"))
    web = cfg.web or {}

    # Only what a person would look for. The recording buffer is an
    # implementation detail -- it holds the current minute and whatever an open
    # incident needs, and the panel offers live video instead.
    roots = {"events": cfg.events_root.resolve(),
             "detections": cfg.detections_root.resolve()}

    @app.context_processor
    def _site():
        # Every template gets it, so a new page cannot forget to pass it.
        return {"site_name": cfg.site_name}

    # ------------------------------------------------------------------ auth
    # Deriving a PBKDF2 hash costs about 100 ms on the board, and Basic Auth
    # resends the credentials on every single request -- the status poll twice
    # a second, the MJPEG stream, and one per thumbnail in the film strip.
    # Verifying each time would eat a core and starve the detector, so the
    # verdict is cached against a peppered digest. The pepper is per-process
    # and never stored, so the cache keys are not the passwords.
    _pepper = os.urandom(32)
    _auth_cache = {}
    _auth_lock = threading.Lock()
    AUTH_TTL_OK = 300.0
    AUTH_TTL_BAD = 5.0          # short, so a fixed password works at once

    def _auth_key(user, pw):
        # The revision is part of the key, so a password changed behind our
        # back -- settings_cli.py over ssh, say -- invalidates every cached
        # verdict the moment the store notices the file changed.
        rev = settings.revision if settings is not None else 0
        msg = ("%d\0%s\0%s" % (rev, user, pw)).encode("utf-8", "surrogatepass")
        return hmac.new(_pepper, msg, hashlib.sha256).hexdigest()

    def _same(a, b):
        """Constant-time compare that tolerates non-ASCII input."""
        return hmac.compare_digest(a.encode("utf-8", "surrogatepass"),
                                   b.encode("utf-8", "surrogatepass"))

    def forget_auth_cache():
        """Drop cached verdicts, so a credential change bites immediately."""
        with _auth_lock:
            _auth_cache.clear()

    def _verify(user, pw):
        """The slow path. Panel-set credentials win over the configured ones."""
        if settings is not None and settings.has_credentials():
            return _same(user, settings.web_user) and settings.check_password(pw)
        want_pw = cfg.web_password
        if not want_pw:
            log.warning("web.auth_required is set but no password is "
                        "configured; refusing every request")
            return False
        return _same(user, web.get("auth_user", "admin")) and _same(pw, want_pw)

    def _auth_mode():
        """password | trusted | open. The panel overrides the config."""
        if settings is not None and settings.auth_mode:
            return settings.auth_mode
        return "password" if cfg.web_auth_required else "open"

    def check_auth():
        mode = _auth_mode()
        if mode == "open":
            return True
        if mode == "trusted" and settings is not None:
            # remote_addr is the real client here: the panel is served
            # directly, with no proxy in front of it. Behind one this would be
            # the proxy's address and the check would let the world in, which
            # is why the settings page says so.
            if settings.is_trusted(request.remote_addr):
                return True
            # Not on a trusted network: fall through to the password, so a
            # phone on the guest wifi still has a way in rather than a wall.

        a = request.authorization
        if not a or a.username is None or a.password is None:
            return False
        key = _auth_key(a.username, a.password)
        now = time.time()
        with _auth_lock:
            hit = _auth_cache.get(key)
            if hit and hit[1] > now:
                return hit[0]
        ok = _verify(a.username, a.password)
        with _auth_lock:
            if len(_auth_cache) > 64:      # a wall panel has one or two users
                _auth_cache.clear()
            _auth_cache[key] = (ok, now + (AUTH_TTL_OK if ok else AUTH_TTL_BAD))
        return ok

    def protected(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if not check_auth():
                return _unauthorised(cfg.site_name)
            return fn(*a, **kw)
        return wrapper

    def need_ptz():
        if ptz is None:
            abort(503, "camera control is not available in this process")

    # ---------------------------------------------------------------- pages
    @app.route("/")
    @protected
    def panel():
        return render_template(
            "panel.html",
            presets=(cfg._get("ptz", "scan_presets", default=[]) or []),
            home=cfg._get("ptz", "home_preset", default="Home"),
            keepalive_ms=int(web.get("keepalive_ms", 250)),
            has_ptz=ptz is not None and getattr(ptz, "enabled", True),
            has_speaker=announcer is not None and announcer.enabled,
            has_detector=live is not None,
            has_media=True,
            roots=sorted(roots),
        )

    # ------------------------------------------------------------- browser
    def _day_dirs():
        """Days that have anything, newest first."""
        days = set()
        for root in (cfg.detections_root, cfg.events_root):
            if root.exists():
                for d in root.iterdir():
                    if d.is_dir() and len(d.name) == 10 and d.name[4] == "-":
                        days.add(d.name)
        return sorted(days, reverse=True)

    def _stamp_of(name):
        """The clock time a file belongs to, as HH:MM:SS, or None."""
        m = re.search(r"(\d{2})-(\d{2})-(\d{2})(?!.*\d{2}-\d{2}-\d{2})", name)
        if not m:
            return None
        return ":".join(m.groups())

    def _seconds_of_iso(iso):
        """Seconds past midnight from an ISO timestamp, or None."""
        if not iso:
            return None
        try:
            t = dt.datetime.fromisoformat(iso)
        except ValueError:
            return None
        return t.hour * 3600 + t.minute * 60 + t.second

    def _seconds_of(name):
        st = _stamp_of(name)
        if not st:
            return None
        h, m, sec = (int(x) for x in st.split(":"))
        return h * 3600 + m * 60 + sec

    def _gather(day):
        """Stills and clips for one day. Shared by the page and the poll."""
        shots, clips = [], []
        if day:
            d = cfg.detections_root / day
            if d.exists():
                for p in sorted(d.glob("*.jpg")):
                    shots.append({"name": p.name, "at": _stamp_of(p.name) or "",
                                  "secs": _seconds_of(p.name) or 0,
                                  "url": url_for("media", root_key="detections",
                                                 subpath=day, filename=p.name)})
            e = cfg.events_root / day
            if e.exists():
                for p in sorted(e.glob("*.mp4")):
                    if p.name.endswith(".annotated.mp4"):
                        continue
                    m = re.search(r"clip_\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})"
                                  r"_\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})",
                                  p.name)
                    if m:
                        sh, sm, ss, eh, em, es = (int(x) for x in m.groups())
                        start, end = sh * 3600 + sm * 60 + ss, eh * 3600 + em * 60 + es
                    else:
                        start = _seconds_of(p.name) or 0
                        end = start
                    # The 2 fps companion, when it exists: a fraction of the
                    # size, so it opens on a tablet in a moment rather than a
                    # minute, and it carries the boxes.
                    ann = p.with_suffix("")
                    ann = ann.with_name(ann.name + ".annotated.mp4")

                    # Each file has its own first frame: the full-resolution
                    # clip starts on a whole-minute segment boundary, the
                    # companion starts when the incident did. Seeking to a
                    # wall-clock moment needs both, so the sidecar carries them.
                    t0, ann_t0 = start, start
                    side = p.with_suffix(".json")
                    if side.exists():
                        try:
                            meta = json.loads(side.read_text())
                            t0 = _seconds_of_iso(meta.get("t0")) or start
                            ann_t0 = _seconds_of_iso(
                                meta.get("annotated_t0")) or t0
                        except (OSError, ValueError):
                            pass

                    # One shape, used verbatim by both the page and the poll.
                    # These were different names on the two paths, so the first
                    # poll replaced the clip list with objects the page did not
                    # recognise and every video silently fell back to full res.
                    clips.append({
                        "name": p.name, "start": start, "end": max(end, start),
                        "t0": t0, "annT0": ann_t0,
                        "at": f"{start // 3600:02d}:{start % 3600 // 60:02d}:"
                              f"{start % 60:02d}",
                        "size": _human(p.stat().st_size),
                        "url": url_for("media", root_key="events",
                                       subpath=day, filename=p.name),
                        "ann": (url_for("media", root_key="events",
                                        subpath=day, filename=ann.name)
                                if ann.exists() else ""),
                        "annSize": _human(ann.stat().st_size) if ann.exists() else ""})

                # Annotated clips whose full-resolution partner never got cut.
                # Without this they are invisible: the loop above skips
                # *.annotated.mp4 and only reaches them through a base clip.
                # Three real events survived only as their companion, and the
                # footage was on the card the whole time.
                have = {c["name"] for c in clips}
                for p in sorted(e.glob("*.annotated.mp4")):
                    base = p.name[:-len(".annotated.mp4")] + ".mp4"
                    if base in have or (e / base).exists():
                        continue
                    m = re.search(r"clip_\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})"
                                  r"_\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})",
                                  base)
                    if m:
                        sh, sm, ss, eh, em, es = (int(x) for x in m.groups())
                        start = sh * 3600 + sm * 60 + ss
                        end = eh * 3600 + em * 60 + es
                    else:
                        start = _seconds_of(base) or 0
                        end = start
                    t0 = ann_t0 = start
                    side = (e / base).with_suffix(".json")
                    if side.exists():
                        try:
                            meta = json.loads(side.read_text())
                            t0 = _seconds_of_iso(meta.get("t0")) or start
                            ann_t0 = _seconds_of_iso(
                                meta.get("annotated_t0")) or t0
                        except (OSError, ValueError):
                            pass
                    clips.append({
                        "name": base, "start": start, "end": max(end, start),
                        "t0": t0, "annT0": ann_t0,
                        "at": f"{start // 3600:02d}:{start % 3600 // 60:02d}:"
                              f"{start % 60:02d}",
                        "size": "", "url": "",       # no full-resolution cut
                        "ann": url_for("media", root_key="events",
                                       subpath=day, filename=p.name),
                        "annSize": _human(p.stat().st_size)})
                clips.sort(key=lambda c: c["start"])
        return shots, clips

    def _cutting():
        """How many clips are queued or being cut right now.

        "No video yet" must not imply the footage is gone when a concat is
        still running -- a cut can take a minute while it waits for the last
        segment to stop growing.
        """
        if concat is None:
            return 0
        try:
            return len(concat.q.queue) + len(concat.in_progress)
        except Exception:
            log.debug("could not read the clip queue", exc_info=True)
            return 0

    def _open_since(day):
        """Seconds past midnight at which the current incident began, or -1.

        A still only exists because something triggered, so a still with no
        clip normally means the incident has not finished yet.
        """
        if controller is None:
            return -1
        try:
            started = controller.open_incident_start()
        except Exception:
            log.debug("could not read the open incident", exc_info=True)
            return -1
        if not started or started.strftime("%Y-%m-%d") != day:
            return -1
        return started.hour * 3600 + started.minute * 60 + started.second

    # Classes worth putting in front of someone at a tennis club. The rest of
    # COCO is still selectable, just folded away -- nobody needs "toaster" at
    # eye level, but a club with a dog problem should not have to edit yaml.
    LIKELY = ("person", "bicycle", "car", "motorbike", "bus", "truck",
              "dog", "cat", "bird", "horse", "aeroplane", "train")

    def _model_classes():
        """The model's own labels, read from source rather than imported.

        yolov10 pulls in cv2 and torch; the panel process may be running
        without a detector at all, and a settings page that 500s because the
        NPU stack is absent would be a poor trade.
        """
        import ast as _ast
        import re as _re
        try:
            src = (Path(__file__).resolve().parent
                   / "yolov10.py").read_text(encoding="utf-8")
            m = _re.search(r"^CLASSES\s*=\s*(\(.*?\))", src, _re.S | _re.M)
            if m:
                return [c.strip() for c in _ast.literal_eval(m.group(1))]
        except (OSError, ValueError, SyntaxError):
            log.warning("could not read the model class list", exc_info=True)
        return list(LIKELY)

    @app.route("/settings", methods=["GET", "POST"])
    @protected
    def settings_page():
        known = _model_classes()
        saved = ""
        if request.method == "POST":
            if settings is None:
                abort(503)
            chosen = request.form.getlist("cls")
            settings.set_trigger_classes(chosen, known=known)
            saved = "on" if chosen else "none"

        if settings is not None:
            active = settings.trigger_classes
            overridden = settings.overridden("trigger_classes")
        else:
            active = cfg.trigger_classes
            overridden = False

        return _settings_page(
            "classes",
            likely=[c for c in LIKELY if c in known],
            rest=[c for c in known if c not in LIKELY],
            active=active, overridden=overridden, saved=saved)

    def _settings_page(tab, **extra):
        ctx = dict(
            tab=tab, editable=settings is not None,
            cur_user=_current_user(), from_panel=_creds_from_panel(),
            cred_err="", cred_msg="", acc_err="", acc_msg="",
            mode=_auth_mode(),
            mode_from_panel=(settings is not None and bool(settings.auth_mode)),
            networks=", ".join(str(n) for n in settings.trusted_networks)
                     if settings is not None else "",
            client_ip=request.remote_addr or "",
            likely=[], rest=[], active=set(), overridden=False,
            defaults=sorted(cfg.trigger_classes), saved="")
        ctx.update(extra)
        return render_template("settings.html", **ctx)

    def _current_user():
        if settings is not None and settings.has_credentials():
            return settings.web_user
        return web.get("auth_user", "admin")

    def _creds_from_panel():
        return settings is not None and settings.has_credentials()

    @app.route("/settings/credentials", methods=["GET", "POST"])
    @protected
    def settings_credentials():
        err = msg = ""
        if request.method == "POST":
            if settings is None:
                abort(503)
            user = (request.form.get("user") or "").strip()
            cur = request.form.get("current") or ""
            new = request.form.get("new") or ""
            again = request.form.get("again") or ""

            # Re-check the current password even though this request is already
            # authenticated: a wall panel is left logged in, and the browser
            # remembers Basic Auth for the session. Without this, anyone who
            # walks up can lock out everyone else.
            if not _verify(_current_user(), cur):
                err = "The current password is not right."
            elif new != again:
                err = "The two new passwords do not match."
            else:
                try:
                    settings.set_web_credentials(user or _current_user(), new)
                except ValueError as e:
                    err = str(e)[0].upper() + str(e)[1:] + "."
                else:
                    forget_auth_cache()
                    msg = "changed"

        return _settings_page("credentials", cred_err=err, cred_msg=msg)

    @app.route("/settings/access", methods=["POST"])
    @protected
    def settings_access():
        if settings is None:
            abort(503)
        mode = (request.form.get("mode") or "").strip()
        nets = request.form.get("networks") or ""
        try:
            settings.set_auth_mode(mode, nets)
        except ValueError as e:
            return _settings_page("credentials",
                                  acc_err=str(e)[0].upper() + str(e)[1:] + ".")
        forget_auth_cache()
        return _settings_page("credentials", acc_msg=mode)

    @app.route("/settings/access/reset", methods=["POST"])
    @protected
    def settings_access_reset():
        if settings is None:
            abort(503)
        settings.clear_access()
        forget_auth_cache()
        return redirect(url_for("settings_credentials"))

    @app.route("/settings/credentials/reset", methods=["POST"])
    @protected
    def settings_credentials_reset():
        """Go back to the user and password from the config and secrets file."""
        if settings is None:
            abort(503)
        settings.clear_web_credentials()
        forget_auth_cache()
        return redirect(url_for("settings_credentials"))

    @app.route("/settings/reset", methods=["POST"])
    @protected
    def settings_reset():
        """Drop the override so config.yaml applies again."""
        if settings is None:
            abort(503)
        settings.clear("trigger_classes")
        log.info("trigger classes reset to the config default: %s",
                 sorted(cfg.trigger_classes))
        return redirect(url_for("settings_page"))

    @app.route("/legal")
    @protected
    def legal():
        """Licence and notices, read from the files rather than copied here.

        A second copy of a disclaimer is a copy that goes stale, and the one
        that matters is the one shipped beside the code. NOTICE first: Apache
        section 4(d) is satisfied by displaying it wherever third-party
        notices normally appear, and for a device whose only interface is this
        panel, that is here.
        """
        here = Path(__file__).resolve().parent
        parts = []
        for name in ("NOTICE", "LICENSE"):
            try:
                parts.append((here / name).read_text(encoding="utf-8"))
            except OSError:
                parts.append(
                    name + " is missing from this install. This software is "
                    "Copyright 2026 Philip van Houtte, magicview.tv, the "
                    "Netherlands, licensed "
                    "under the Apache License 2.0, and is provided \"AS IS\" "
                    "without warranties or conditions of any kind.")
        return render_template("legal.html",
                               text=("\n\n" + "=" * 79 + "\n\n").join(parts))

    @app.route("/media")
    @protected
    def media_browser():
        """Two tabs over the same day: the JPEGs, and the clips.

        The thumbnail strip is shared, so switching tabs keeps you at the same
        moment -- which is the point: you find the second you care about in
        the stills, then watch the clip from there.
        """
        days = _day_dirs()
        day = request.args.get("day") or (days[0] if days else None)
        shots, clips = _gather(day) if day else ([], [])
        return render_template("media.html", days=days, day=day,
                               shots=shots, clips=clips,
                               open_since=_open_since(day),
                               cutting=_cutting(),
                               poll_ms=int((cfg.web or {}).get("media_poll_ms", 5000)),
                               roots=sorted(roots))

    @app.route("/api/media")
    @protected
    def api_media():
        """New stills since a moment, plus the clips as they stand.

        The strip is appended to rather than rebuilt, so a poll must not
        return what the page already has -- during an incident that is one
        still a second, and rebuilding would lose the scroll position and the
        selection every time.
        """
        day = request.args.get("day") or ""
        try:
            after = int(request.args.get("after", -1))
        except ValueError:
            after = -1
        shots, clips = _gather(day) if day else ([], [])
        fresh = [s for s in shots if s["secs"] > after]
        return jsonify(day=day, shots=fresh, clips=clips,
                       open_since=_open_since(day),
                       cutting=_cutting(),
                       total_shots=len(shots))

    @app.route("/browse/<root_key>/", defaults={"subpath": ""})
    @app.route("/browse/<root_key>/<path:subpath>")
    @protected
    def browse(root_key, subpath):
        base, target = _safe(roots, root_key, subpath)
        if not target.exists():
            abort(404)
        if target.is_file():
            return file_view(root_key, subpath)
        items = []
        # Newest first: the reason anyone opens this page is "what happened
        # last night", not "what happened in March".
        for p in sorted(target.iterdir(), reverse=True):
            rel = p.relative_to(base)
            items.append({
                "name": p.name + ("/" if p.is_dir() else ""),
                "href": (url_for("browse", root_key=root_key, subpath=str(rel))
                         if p.is_dir()
                         else url_for("file_view", root_key=root_key,
                                      subpath=str(rel))),
                "is_dir": p.is_dir(),
                "size": "" if p.is_dir() else _human(p.stat().st_size),
            })
        parent = None
        if target != base:
            rel = target.parent.relative_to(base)
            parent = url_for("browse", root_key=root_key,
                             subpath="" if str(rel) == "." else str(rel))
        return render_template("browse.html", root_key=root_key, items=items,
                               cwd=str(target.relative_to(base)) if target != base else "/",
                               parent_href=parent, roots=sorted(roots))

    @app.route("/view/<root_key>/<path:subpath>")
    @protected
    def file_view(root_key, subpath):
        base, target = _safe(roots, root_key, subpath)
        if not target.is_file():
            abort(404)
        rel_dir = target.parent.relative_to(base)
        file_url = url_for("media", root_key=root_key,
                           subpath=("" if str(rel_dir) == "." else str(rel_dir)),
                           filename=target.name)
        low = target.name.lower()
        kind = ("image" if low.endswith((".jpg", ".jpeg", ".png", ".gif"))
                else "video" if low.endswith(".mp4") else "download")
        return render_template("view.html", root_key=root_key, kind=kind,
                               filename=target.name, file_url=file_url,
                               roots=sorted(roots))

    @app.route("/media/<root_key>/<path:subpath>/<filename>")
    @app.route("/media/<root_key>//<filename>", defaults={"subpath": ""})
    @app.route("/media/<root_key>/<filename>", defaults={"subpath": ""})
    @protected
    def media(root_key, subpath, filename):
        _, folder = _safe(roots, root_key, subpath)
        return send_from_directory(folder, filename, as_attachment=False)

    # ------------------------------------------------------------ live view
    def grab():
        return ptz.snapshot()

    @app.route("/snapshot.jpg")
    @protected
    def snapshot():
        need_ptz()
        try:
            return Response(grab(), mimetype="image/jpeg",
                            headers={"Cache-Control": "no-store"})
        except Exception as e:
            log.warning("snapshot failed: %s", e)
            abort(503)

    @app.route("/stream.mjpg")
    @protected
    def stream():
        """Multipart MJPEG.

        'snapshot' mode polls the camera's own JPEG endpoint -- slow but works
        with no subprocess and on any browser. 'ffmpeg' mode transcodes the
        sub-stream, which is smoother but costs a process per viewer.
        """
        mode = web.get("stream_mode", "snapshot")
        # ffmpeg mode transcodes the RTSP stream directly, so it does not need
        # the PTZ driver at all -- which means a live view works against any
        # camera, including one whose control protocol we do not speak.
        if mode != "ffmpeg":
            need_ptz()
        if mode == "ffmpeg":
            return Response(
                _ffmpeg_mjpeg(cfg),
                mimetype=("multipart/x-mixed-replace; "
                          f"boundary={FFMPEG_BOUNDARY}"))
        try:
            fps = float(request.args.get("fps", web.get("stream_fps", 3)))
        except ValueError:
            fps = 3.0
        fps = max(0.2, min(fps, 10.0))
        return Response(_snapshot_mjpeg(grab, fps),
                        mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

    # ------------------------------------------------------------------- api
    @app.route("/detect.mjpg")
    @protected
    def detect_stream():
        """What the NPU is looking at, with its boxes drawn on.

        Served from the detector's own frames rather than the camera, so it
        shows exactly what the model saw -- including nothing, which is itself
        the answer when a threshold is set too high.
        """
        if live is None:
            abort(503, "the detector is not running in this process")

        def gen():
            seq = -1
            idle = 0
            while True:
                jpeg, seq = live.wait_for_new(seq, timeout=5.0)
                if jpeg is None:
                    idle += 1
                    if idle > 12:      # a minute with no frames at all
                        return
                    continue
                idle = 0
                yield _part(jpeg)

        return Response(gen(),
                        mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

    @app.route("/detect.jpg")
    @protected
    def detect_still():
        if live is None:
            abort(503)
        jpeg, at, _ = live.get()
        if not jpeg:
            abort(503, "the detector has not produced a frame yet")
        return Response(jpeg, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.route("/api/status")
    @protected
    def api_status():
        out = {"time": dt.datetime.now().strftime("%H:%M:%S"),
               "has_ptz": ptz is not None}
        if controller is not None:
            try:
                out.update(controller.status())
            except Exception:
                log.exception("controller status failed")
        if ptz is not None:
            try:
                out["ptz"] = ptz.status()
            except Exception:
                log.exception("ptz status failed")
        if live is not None:
            out["detector_frame_age_s"] = (round(live.age, 1)
                                           if live.age is not None else None)
        if health is not None:
            try:
                out["health"] = health.status(cfg.events_root)
            except Exception:
                log.exception("health status failed")
        return jsonify(out)

    @app.route("/api/ptz/move", methods=["POST"])
    @protected
    def api_move():
        need_ptz()
        direction = (request.form.get("dir") or "").lower()
        _take_manual()
        try:
            ptz.move(direction, source="manual")
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200
        return jsonify(ok=True)

    @app.route("/api/ptz/zoom", methods=["POST"])
    @protected
    def api_zoom():
        need_ptz()
        _take_manual()
        try:
            ptz.zoom((request.form.get("dir") or "").lower(), source="manual")
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200
        return jsonify(ok=True)

    @app.route("/api/ptz/stop", methods=["POST", "GET"])
    @protected
    def api_stop():
        """Also accepts GET so a dying page can fire it via an <img> src.

        Belt and braces only -- the server-side deadline is what actually
        guarantees the motors stop.
        """
        need_ptz()
        try:
            ptz.stop(reason="panel")
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200
        return jsonify(ok=True)

    @app.route("/api/ptz/preset", methods=["POST"])
    @protected
    def api_preset():
        need_ptz()
        name = request.form.get("name") or ""
        _take_manual()
        try:
            ptz.goto_preset(name, source="manual")
        except Exception as e:
            return jsonify(ok=False, error=str(e)), 200
        return jsonify(ok=True)

    @app.route("/api/scan", methods=["POST"])
    @protected
    def api_scan():
        if controller is None:
            abort(503)
        controller.on_pir(_FakePir())
        return jsonify(ok=True)

    @app.route("/api/arm", methods=["POST"])
    @protected
    def api_arm():
        sch = schedule if schedule is not None else getattr(controller, "schedule", None)
        if sch is None:
            abort(503)
        armed = request.form.get("armed") == "1"
        try:
            minutes = int(request.form.get("minutes", 120))
        except ValueError:
            minutes = 120
        minutes = max(1, min(minutes, 24 * 60))
        sch.override(armed, dt.datetime.now() + dt.timedelta(minutes=minutes))
        return jsonify(ok=True, armed=armed, minutes=minutes)

    @app.route("/api/arm/clear", methods=["POST"])
    @protected
    def api_arm_clear():
        sch = schedule if schedule is not None else getattr(controller, "schedule", None)
        if sch is None:
            abort(503)
        sch.clear_override()
        return jsonify(ok=True)

    def _take_manual():
        if controller is not None:
            try:
                controller.on_manual(True)
            except Exception:
                log.exception("could not hand control to the panel")

    @app.route("/api/mute", methods=["POST"])
    @protected
    def api_mute():
        """The switch a person can reach. A voice nobody can silence is a
        voice that gets unplugged."""
        if announcer is None:
            abort(503)
        if request.form.get("muted") == "0":
            announcer.unmute()
            return jsonify(ok=True, muted=False)
        try:
            minutes = int(request.form.get("minutes", 120))
        except ValueError:
            minutes = 120
        until = announcer.mute(minutes)
        return jsonify(ok=True, muted=True, until=until.strftime("%H:%M"))

    @app.route("/healthz")
    def healthz():
        return jsonify(ok=True)

    @app.route("/index.html")
    def index_html():
        return redirect(url_for("panel"))

    return app


class _FakePir:
    kind = "active"
    at = 0.0
    duration = 0.0
    detail = "requested from the wall panel"


# ------------------------------------------------------------------ helpers

def _safe(roots, root_key, subpath=""):
    if root_key not in roots:
        abort(404)
    base = roots[root_key]
    target = (base / subpath).resolve()
    if base != target and base not in target.parents:
        abort(403)
    return base, target


def _human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f}{unit}"
        n /= 1024


def _part(jpeg):
    return (b"--" + BOUNDARY.encode() + b"\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
            + jpeg + b"\r\n")


def _snapshot_mjpeg(grab, fps):
    interval = 1.0 / fps
    fails = 0
    while True:
        started = time.time()
        try:
            frame = grab()
            fails = 0
            if frame:
                yield _part(frame)
        except GeneratorExit:
            return
        except Exception as e:
            fails += 1
            log.debug("snapshot for the stream failed: %s", e)
            if fails > 20:
                log.warning("giving up on the snapshot stream after %d "
                            "failures", fails)
                return
        delay = interval - (time.time() - started)
        if delay > 0:
            time.sleep(delay)


def _ffmpeg_mjpeg(cfg):
    web = cfg.web or {}
    scale = int(web.get("ffmpeg_scale", 960))
    fps = float(web.get("stream_fps", 3))
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-rtsp_transport", "tcp", "-i", cfg.detection_rtsp,
           "-vf", f"scale={scale}:-2,fps={fps}",
           "-f", "mpjpeg", "-q:v", "6", "-"]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
        while True:
            chunk = proc.stdout.read(16384)
            if not chunk:
                return
            yield chunk
    except GeneratorExit:
        return
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
