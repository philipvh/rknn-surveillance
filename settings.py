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

"""Settings the operator changes from the panel, kept apart from the config.

config.yaml and config.local.yaml are hand-written, commented, and deployed
from a development machine. A screen that rewrote either would lose the
comments and be overwritten by the next deploy. So anything set from the panel
lands in its own JSON file on the data volume, and reads fall back to the
config when it says nothing -- config.yaml stays the default, this file is the
override.

The detection loop asks for the trigger classes on every frame, so a change
takes effect immediately rather than at the next restart.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets as _secrets
import tempfile
import threading
import time

log = logging.getLogger("settings")


class Settings:
    # How often to stat the file looking for an outside edit. The rescue CLI
    # writes this file while the service is running, and a change nobody
    # notices until the next restart is not much of a rescue.
    POLL_S = 1.0

    def __init__(self, path, defaults=None, clock=time.time):
        self.path = path
        self._defaults = dict(defaults or {})
        self._lock = threading.RLock()
        self._data = {}
        self._clock = clock
        self._mtime = None
        self._checked = 0.0
        self.revision = 0
        self.reload()

    # ---------------------------------------------------------------- load
    def _stamp(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _maybe_reload(self):
        """Pick up an edit made behind our back, at most once a second."""
        now = self._clock()
        if (now - self._checked) < self.POLL_S:
            return
        self._checked = now
        if self._stamp() != self._mtime:
            self.reload()

    def reload(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as e:
            # A corrupt overrides file must not take the service down with it;
            # falling back to the config is always a working state.
            log.warning("could not read %s (%s); using config defaults",
                        self.path, e)
            raw = {}
        if not isinstance(raw, dict):
            log.warning("%s is not an object; ignoring it", self.path)
            raw = {}
        with self._lock:
            changed = raw != self._data
            self._data = raw
            self._mtime = self._stamp()
            self._checked = self._clock()
            if changed:
                # Bumped so callers that cache a decision derived from these
                # values -- the panel caches password verdicts -- can notice.
                self.revision += 1
        return self

    # ---------------------------------------------------------------- save
    def _save(self):
        """Write via a temp file in the same directory, then rename.

        A half-written settings file read after a power cut is worse than no
        settings file, and rename is atomic within a filesystem.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=1, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(self.path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ----------------------------------------------------------- accessors
    def get(self, key, default=None):
        self._maybe_reload()
        with self._lock:
            if key in self._data:
                return self._data[key]
        if key in self._defaults:
            return self._defaults[key]
        return default

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._save()
            self._mtime = self._stamp()
            self.revision += 1
        return value

    def clear(self, key):
        """Drop an override so the config value applies again."""
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._save()

    def overridden(self, key):
        with self._lock:
            return key in self._data

    def as_dict(self):
        with self._lock:
            return dict(self._data)

    # ------------------------------------------------------------ triggers
    @property
    def trigger_classes(self):
        """The live set the detector matches against.

        Returned as a set of stripped labels, exactly like Config does, so the
        loop cannot tell which layer answered.
        """
        v = self.get("trigger_classes")
        if not isinstance(v, (list, tuple, set)):
            v = self._defaults.get("trigger_classes", ())
        return {str(c).strip() for c in v if str(c).strip()}

    def set_trigger_classes(self, classes, known=None):
        """Store the trigger set. Returns the set actually stored.

        `known` is the model's label list. A class the model cannot emit would
        never fire and there is no way to tell from the logs, so they are
        dropped here rather than saved and silently ignored.
        """
        wanted = {str(c).strip() for c in classes if str(c).strip()}
        if known is not None:
            allowed = {str(c).strip() for c in known}
            unknown = wanted - allowed
            if unknown:
                log.warning("ignoring %d unknown class(es): %s",
                            len(unknown), ", ".join(sorted(unknown)))
            wanted &= allowed
        self.set("trigger_classes", sorted(wanted))
        log.info("trigger classes set to: %s", sorted(wanted) or "(none)")
        return wanted

    # --------------------------------------------------------------- sweep
    # When triggered, the camera can oscillate between two saved positions to
    # cover a scene wider than one field of view. The endpoints are camera
    # presets (this camera reports no absolute angles), saved from the panel;
    # here we keep only whether each has been set, plus the toggle and dwell.
    @property
    def sweep_enabled(self):
        return bool(self.get("sweep_enabled", False))

    def set_sweep_enabled(self, on):
        self.set("sweep_enabled", bool(on))
        log.info("trigger sweep %s", "on" if self.sweep_enabled else "off")
        return self.sweep_enabled

    @property
    def sweep_dwell_s(self):
        try:
            return max(0.5, min(float(self.get("sweep_dwell_s", 4.0)), 60.0))
        except (TypeError, ValueError):
            return 4.0

    def set_sweep_dwell_s(self, seconds):
        self.set("sweep_dwell_s", max(0.5, min(float(seconds), 60.0)))
        return self.sweep_dwell_s

    @property
    def sweep_left_saved(self):
        return bool(self.get("sweep_left_saved", False))

    @property
    def sweep_right_saved(self):
        return bool(self.get("sweep_right_saved", False))

    @property
    def sweep_home_saved(self):
        return bool(self.get("sweep_home_saved", False))

    def mark_sweep_saved(self, side, saved=True):
        key = {"left": "sweep_left_saved", "right": "sweep_right_saved",
               "home": "sweep_home_saved"}.get(side)
        if key is None:
            raise ValueError("side must be 'left', 'right' or 'home'")
        self.set(key, bool(saved))

    @property
    def sweep_ready(self):
        """Both endpoints saved -- the sweep has somewhere to go."""
        return self.sweep_left_saved and self.sweep_right_saved

    # --------------------------------------------------------- credentials
    # PBKDF2-HMAC-SHA256: in the standard library on the board's Python 3.9,
    # no extra dependency, and the stored form reveals nothing useful if the
    # settings file is read. The iteration count is stored per record so it
    # can be raised later without invalidating existing passwords.
    ITERATIONS = 240_000

    @staticmethod
    def hash_password(password, salt=None, iterations=None):
        iterations = int(iterations or Settings.ITERATIONS)
        salt = salt or _secrets.token_hex(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), iterations)
        return {"algo": "pbkdf2_sha256", "salt": salt,
                "iterations": iterations, "hash": dk.hex()}

    @property
    def web_user(self):
        v = self.get("web_user")
        return str(v) if v else ""

    def has_credentials(self):
        rec = self.get("web_password")
        return bool(self.web_user and isinstance(rec, dict) and rec.get("hash"))

    def check_password(self, password):
        """Constant-time check against the stored hash."""
        rec = self.get("web_password")
        if not isinstance(rec, dict) or rec.get("algo") != "pbkdf2_sha256":
            return False
        try:
            got = self.hash_password(password, rec["salt"], rec["iterations"])
        except (KeyError, ValueError, TypeError):
            log.warning("stored credential record is malformed; refusing it")
            return False
        return hmac.compare_digest(got["hash"], str(rec.get("hash", "")))

    def set_web_credentials(self, user, password):
        """Store the panel login. The password is never kept in the clear."""
        user = str(user).strip()
        if not user:
            raise ValueError("the user name cannot be empty")
        if len(password) < 8:
            raise ValueError("the password must be at least 8 characters")
        with self._lock:
            self._data["web_user"] = user
            self._data["web_password"] = self.hash_password(password)
            self._save()
            self._mtime = self._stamp()
            self.revision += 1
        log.info("panel credentials changed; user is now %r", user)

    def clear_web_credentials(self):
        """Fall back to the configured user and secrets.yaml password."""
        with self._lock:
            changed = False
            for k in ("web_user", "web_password"):
                if k in self._data:
                    del self._data[k]
                    changed = True
            if changed:
                self._save()
                self._mtime = self._stamp()
                self.revision += 1
        if changed:
            log.warning("panel credentials cleared; the configured password "
                        "in secrets.yaml applies again")

    # -------------------------------------------------------------- access
    # Who has to type a password. A club LAN with no route to the internet is
    # a different threat model from a box on the open web, and forcing Basic
    # Auth on a wall tablet that lives in a locked clubhouse buys nothing.
    MODES = ("password", "trusted", "open")

    @property
    def auth_mode(self):
        v = str(self.get("web_auth_mode") or "").strip().lower()
        return v if v in self.MODES else ""

    def set_auth_mode(self, mode, networks=None):
        """Set who must authenticate. Returns the networks actually stored.

        'password'  everyone, always.
        'trusted'   no password from the listed networks; password elsewhere.
        'open'      nobody, from anywhere.
        """
        mode = str(mode).strip().lower()
        if mode not in self.MODES:
            raise ValueError("unknown access mode: %r" % mode)
        nets = self.parse_networks(networks or [])
        if mode == "trusted" and not nets:
            raise ValueError(
                "a trusted network is needed, for example 192.168.1.0/24")
        with self._lock:
            self._data["web_auth_mode"] = mode
            self._data["web_trusted_networks"] = [str(n) for n in nets]
            self._save()
            self._mtime = self._stamp()
            self.revision += 1
        log.warning("panel access mode set to %r%s", mode,
                    (" for " + ", ".join(str(n) for n in nets)) if nets else "")
        return nets

    @staticmethod
    def parse_networks(values):
        """Turn text into networks, rejecting anything that will not match.

        Accepts a list or a string separated by commas, spaces or newlines. A
        bare address becomes a single-host network, which is what someone
        typing one IP means.
        """
        if isinstance(values, str):
            values = values.replace(",", " ").split()
        out = []
        for v in values:
            v = str(v).strip()
            if not v:
                continue
            try:
                out.append(ipaddress.ip_network(v, strict=False))
            except ValueError:
                raise ValueError("%r is not an address or network" % v)
        return out

    @property
    def trusted_networks(self):
        raw = self.get("web_trusted_networks") or []
        try:
            return self.parse_networks(raw)
        except ValueError:
            # Stored badly somehow: trust nobody rather than everybody.
            log.warning("stored trusted networks are unusable; ignoring them")
            return []

    def is_trusted(self, addr):
        """Is this client on a network that skips the password?"""
        if not addr:
            return False
        try:
            ip = ipaddress.ip_address(str(addr).strip())
        except ValueError:
            return False
        for net in self.trusted_networks:
            if ip.version == net.version and ip in net:
                return True
        return False

    def clear_access(self):
        with self._lock:
            changed = False
            for k in ("web_auth_mode", "web_trusted_networks"):
                if k in self._data:
                    del self._data[k]
                    changed = True
            if changed:
                self._save()

    # ----------------------------------------------------------- overrides
    # Values that belong to config.yaml but were set from the panel. They are
    # kept here rather than written back into the yaml because config.yaml is
    # overwritten by every deploy and config.local.yaml is hand-written with
    # comments that a machine rewrite would destroy.
    @property
    def config_overrides(self):
        v = self.get("config")
        return v if isinstance(v, dict) else {}

    def set_config_overrides(self, tree):
        """Replace the whole override tree. Empty sections are dropped."""
        if not isinstance(tree, dict):
            raise ValueError("the overrides must be a mapping")
        clean = {k: v for k, v in tree.items()
                 if not (isinstance(v, dict) and not v)}
        with self._lock:
            if clean:
                self._data["config"] = clean
            else:
                self._data.pop("config", None)
            self._save()
            self._mtime = self._stamp()
            self.revision += 1
        return clean

    def set_config_value(self, path, value):
        """Set one dotted key, e.g. "camera.host". None removes it."""
        parts = [p for p in str(path).split(".") if p]
        if not parts:
            raise ValueError("an empty setting name")
        tree = copy.deepcopy(self.config_overrides)
        node = tree
        for p in parts[:-1]:
            if not isinstance(node.get(p), dict):
                node[p] = {}
            node = node[p]
        if value is None:
            node.pop(parts[-1], None)
        else:
            node[parts[-1]] = value
        return self.set_config_overrides(tree)

    def clear_config_overrides(self):
        with self._lock:
            if "config" in self._data:
                del self._data["config"]
                self._save()
                self._mtime = self._stamp()
                self.revision += 1
