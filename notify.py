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

"""A push notification to a phone, with the still attached.

The alert policy already decides *whether* something is worth sending -- the
schedule, the persistence gates, the class gate, the rate limit. This only
carries the verdict, so that adding a second way out of the building does not
mean a second set of rules about when to use it.

ntfy because it needs no account to start, no app store review, no push
certificates, and the phone end is one topic subscription. The cost is that a
topic on a public server is readable by anyone who knows or guesses its name,
which is why the topic belongs in secrets.yaml, wants to be long and random,
and why sending the image is a separate switch from sending the text.

Data matters here: this runs on a mobile bundle. A full-resolution still is
200-400 kB, and at three or four notifications a night that is noticeable but
not ruinous. The image is downscaled before sending when ffmpeg is available,
and dropped entirely rather than sent oversized if it is not.
"""

import datetime as dt
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("notify")

DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT_S = 20
MAX_IMAGE_BYTES = 300 * 1024


class NotifyError(Exception):
    pass


def _ascii_header(value):
    """ntfy headers are latin-1 on the wire; keep them boring.

    A label from the model is ASCII, but a site name is whatever the operator
    typed, and a stray accent would otherwise raise at send time -- in the
    middle of the night, on the one path whose job is to tell somebody.
    """
    text = " ".join(str(value).split())
    return text.encode("ascii", "replace").decode("ascii")


def shrink(src, dst, width=640, quality=6):
    """Downscale a still for sending. Returns dst, or None if it cannot.

    Not fatal: a notification without a picture is still a notification.
    """
    if not shutil.which("ffmpeg"):
        return None
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
             "-vf", "scale=%d:-2" % int(width), "-q:v", str(int(quality)),
             str(dst)],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("could not shrink %s: %s", src, e)
        return None
    if r.returncode != 0 or not os.path.exists(dst):
        log.debug("ffmpeg refused %s: %s", src, (r.stderr or b"")[:200])
        return None
    return dst


class Notifier:
    """Sends one notification per alert, and says plainly when it cannot."""

    def __init__(self, cfg=None, opener=None, clock=time.time):
        c = dict(cfg or {})
        self.enabled = bool(c.get("enabled", False))
        self.server = str(c.get("server") or DEFAULT_SERVER).rstrip("/")
        self.topic = str(c.get("topic") or "").strip()
        self.token = str(c.get("token") or "").strip()
        self.priority = str(c.get("priority", "default"))
        self.attach_image = bool(c.get("attach_image", True))
        self.image_width = int(c.get("image_width", 640))
        self.max_image_bytes = int(c.get("max_image_bytes", MAX_IMAGE_BYTES))
        self.timeout_s = float(c.get("timeout_s", TIMEOUT_S))
        self.retries = int(c.get("retries", 2))
        self.site = str(c.get("site") or "Surveillance")
        self._opener = opener or urllib.request.urlopen
        self._clock = clock
        self._lock = threading.Lock()
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self.last_sent_at = None

    # ---------------------------------------------------------------- state
    @property
    def configured(self):
        return bool(self.enabled and self.topic)

    def status(self):
        with self._lock:
            return {"enabled": self.enabled, "configured": self.configured,
                    "server": self.server,
                    # The topic is the secret; saying it is set is enough.
                    "topic_set": bool(self.topic),
                    "sent": self.sent, "failed": self.failed,
                    "last_error": self.last_error,
                    "last_sent_at": self.last_sent_at}

    # ----------------------------------------------------------------- send
    def _url(self):
        return "%s/%s" % (self.server, urllib.parse.quote(self.topic, safe=""))

    def _headers(self, title, message, tags, click=None, filename=None):
        h = {"Title": _ascii_header(title),
             "Priority": _ascii_header(self.priority)}
        if message:
            h["Message"] = _ascii_header(message)
        if tags:
            h["Tags"] = _ascii_header(",".join(tags))
        if click:
            h["Click"] = _ascii_header(click)
        if filename:
            h["Filename"] = _ascii_header(filename)
        if self.token:
            h["Authorization"] = "Bearer %s" % self.token
        return h

    def _post(self, body, headers, content_type):
        req = urllib.request.Request(self._url(), data=body, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("Content-Type", content_type)
        last = None
        for attempt in range(max(1, self.retries + 1)):
            try:
                with self._opener(req, timeout=self.timeout_s) as r:
                    return getattr(r, "status", 200) or 200
            except urllib.error.HTTPError as e:
                # A 4xx is a configuration mistake; retrying it just wastes
                # the bundle and delays the log line that explains it.
                if 400 <= e.code < 500:
                    raise NotifyError("ntfy refused: HTTP %d" % e.code)
                last = e
            except (urllib.error.URLError, OSError) as e:
                last = e
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 4))
        raise NotifyError("could not reach %s: %s" % (self.server, last))

    def send(self, title, message, image=None, tags=(), click=None):
        """Send one notification. Returns True, or False with last_error set."""
        if not self.configured:
            return False
        body, ctype, filename = b"", "text/plain; charset=utf-8", None
        tmp = None
        try:
            if image and self.attach_image:
                tmp = tempfile.mkdtemp(prefix="notify-")
                small = shrink(image, os.path.join(tmp, "alert.jpg"),
                               width=self.image_width)
                pick = small or image
                try:
                    size = os.path.getsize(pick)
                except OSError:
                    size = None
                if size is not None and size <= self.max_image_bytes:
                    with open(pick, "rb") as fh:
                        body = fh.read()
                    ctype = "image/jpeg"
                    filename = "alert.jpg"
                elif size is not None:
                    log.info("not attaching the still: %d bytes is over the "
                             "%d limit, and this is a mobile bundle",
                             size, self.max_image_bytes)

            headers = self._headers(title, message, tags, click, filename)
            if not body:
                # No picture: the message becomes the body, because ntfy shows
                # a header-only message but a header-only *attachment* post is
                # an empty file.
                body = _ascii_header(message).encode("utf-8")
                headers.pop("Message", None)
            self._post(body, headers, ctype)
        except NotifyError as e:
            with self._lock:
                self.failed += 1
                self.last_error = str(e)
            log.warning("notification not sent: %s", e)
            return False
        except Exception as e:                      # never break the caller
            with self._lock:
                self.failed += 1
                self.last_error = repr(e)
            log.exception("notification failed unexpectedly")
            return False
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
        with self._lock:
            self.sent += 1
            self.last_error = ""
            self.last_sent_at = dt.datetime.now().isoformat(timespec="seconds")
        log.info("notification sent%s", " with the still" if filename else "")
        return True

    # ------------------------------------------------------------- incident
    def send_incident(self, incident, url=None):
        who = ", ".join(sorted(incident.labels)) or "something"
        when = incident.first_seen.strftime("%H:%M")
        title = "%s: %s" % (self.site, who)
        message = ("%s at %s, %ds, confidence %.2f"
                   % (who, when, round(incident.duration_s),
                      incident.max_confidence))
        tags = ["rotating_light"] if "person" in incident.labels else ["eyes"]
        return self.send(title, message,
                         image=(incident.snapshot or None),
                         tags=tags, click=url)


def from_config(cfg):
    """Build a Notifier from config + secrets, or a disabled one."""
    try:
        c = dict(cfg._get("notify", default={}) or {})
    except AttributeError:
        c = dict(cfg or {})
    try:
        c.setdefault("site", cfg._get("web", "title", default="Surveillance"))
    except AttributeError:
        pass
    return Notifier(c)
