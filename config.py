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

"""Configuration loading for the RKNN surveillance system.

One source of truth: config.yaml, with the camera password kept separately in
secrets.yaml so it never lands in git or in a support paste. Replaces the RTSP
URL and credentials that used to be declared twice, in surveillance_main.py and
again in surveillance_core.py, where they could silently disagree.
"""

# The board runs Python 3.9; `float | None` in an annotation is a
# runtime TypeError before 3.10 unless annotations are deferred.
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
import os

import yaml

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.yaml"
# Board-specific overrides. config.yaml is deployed and overwritten; this is
# not, so the camera's address, the GPIO line and anything else particular to
# one installation survive a code sync. Same deep merge as secrets.
DEFAULT_LOCAL = BASE_DIR / "config.local.yaml"
DEFAULT_SECRETS = BASE_DIR / "secrets.yaml"

REDACTED = "***"


class ConfigError(Exception):
    """Raised with a message meant to be readable in a systemd log."""


@dataclass(frozen=True)
class Tier:
    """One class of recorded data, with its own lifetime."""
    name: str
    path: Path
    max_age_days: float | None = None
    max_age_minutes: float | None = None
    protected: bool = False
    stream: str | None = None

    @property
    def max_age_s(self) -> float | None:
        """Lifetime in seconds.

        A rolling buffer of one-minute segments is measured in minutes, not
        days: writing 0.02 days when you mean half an hour is the kind of
        thing nobody reads correctly at midnight.
        """
        if self.max_age_minutes is not None:
            return float(self.max_age_minutes) * 60.0
        if self.max_age_days is not None:
            return float(self.max_age_days) * 86400.0
        return None


@dataclass
class Config:
    raw: dict
    path: Path
    _password: str = field(repr=False, default="")
    _web_password: str = field(repr=False, default="")
    _link_psk: str = field(repr=False, default="")

    # ---------------------------------------------------------------- camera
    @property
    def camera_host(self) -> str:
        return self._req("camera", "host")

    @property
    def camera_port(self) -> int:
        return int(self._req("camera", "http_port"))

    @property
    def camera_user(self) -> str:
        return self._req("camera", "user")

    @property
    def rtsp_port(self) -> int:
        """RTSP port, which is not always the CGI port.

        Foscam serves RTSP on its HTTP port unless a separate RTSP port is
        configured (see the SD2X manual, section 3.4), while other cameras --
        the INSTAR used for bench testing among them -- keep CGI on 80/443 and
        RTSP on 554. Defaults to the HTTP port so Foscam configs stay short.
        """
        return int(self._get("camera", "rtsp_port", default=self.camera_port))

    def rtsp_url(self, stream: str = "main", redacted: bool = False) -> str:
        """RTSP URL for 'main' or 'sub'.

        The password is percent-encoded here so that characters like '=' or '@'
        work without anyone hand-escaping them in a config file.
        """
        key = f"{stream}_path"
        path = self._req("camera", key)
        user = quote(self.camera_user, safe="")
        pw = REDACTED if redacted else quote(self._password, safe="")
        return (f"rtsp://{user}:{pw}@{self.camera_host}:{self.rtsp_port}/"
                f"{str(path).lstrip('/')}")

    def cgi_url(self, redacted: bool = False) -> str:
        return f"http://{self.camera_host}:{self.camera_port}/cgi-bin/CGIProxy.fcgi"

    def cgi_params(self, cmd: str, redacted: bool = False, **extra) -> dict:
        p = {"cmd": cmd, "usr": self.camera_user,
             "pwd": REDACTED if redacted else self._password}
        p.update(extra)
        return p

    # ------------------------------------------------------------- detection
    @property
    def detection(self) -> dict:
        return self._req("detection")

    @property
    def detection_rtsp(self) -> str:
        return self.rtsp_url(self.detection.get("source", "sub"))

    @property
    def trigger_classes(self) -> set[str]:
        """Normalised for comparison.

        The COCO label list in yolov10.py has trailing spaces on several
        entries ('motorbike ', 'bus ', 'truck '), so labels must be stripped
        on both sides or those classes silently never match.
        """
        return {c.strip() for c in self.detection.get("trigger_classes", [])}

    # --------------------------------------------------------------- trigger
    @property
    def trigger(self) -> dict:
        return self._req("trigger")

    # ------------------------------------------------------------- recording
    @property
    def segment_seconds(self) -> int:
        return int(self._req("recording", "segment_seconds"))

    @property
    def tiers(self) -> list[Tier]:
        out = []
        for t in self._req("recording", "tiers"):
            for k in ("name", "path"):
                if k not in t:
                    raise ConfigError(f"recording tier is missing '{k}': {t!r}")
            if t.get("max_age_days") is None and t.get("max_age_minutes") is None:
                raise ConfigError(
                    f"recording tier {t['name']!r} sets neither max_age_days "
                    f"nor max_age_minutes; footage would never be pruned")
            out.append(Tier(
                name=t["name"],
                path=self.resolve(t["path"]),
                max_age_days=t.get("max_age_days"),
                max_age_minutes=t.get("max_age_minutes"),
                protected=bool(t.get("protected", False)),
                stream=t.get("stream"),
            ))
        if not out:
            raise ConfigError("recording.tiers is empty -- nothing would be recorded")
        return out

    def tier(self, name: str) -> Tier:
        for t in self.tiers:
            if t.name == name:
                return t
        raise ConfigError(f"no recording tier named {name!r}")

    @property
    def recording_tiers(self) -> list[Tier]:
        """Tiers fed by a camera stream, i.e. the ones a recorder writes."""
        return [t for t in self.tiers if t.stream]

    # ------------------------------------------------------------- retention
    @property
    def target_free_ratio(self) -> float:
        pct = float(self._req("retention", "target_free_percent"))
        if not 0 <= pct < 100:
            raise ConfigError(f"retention.target_free_percent must be 0-99, got {pct}")
        return pct / 100.0

    @property
    def retention_interval_s(self) -> float:
        return float(self._get("retention", "check_interval_s", default=300))

    # ----------------------------------------------------------------- paths
    def resolve(self, p) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (BASE_DIR / p)

    @property
    def events_root(self) -> Path:
        return self.resolve(self._get("paths", "events_root", default="events"))

    @property
    def detections_root(self) -> Path:
        return self.resolve(self._get("paths", "detections_root", default="detections"))

    # ------------------------------------------------------------------- web
    @property
    def web(self) -> dict:
        return self._get("web", default={}) or {}

    @property
    def web_password(self) -> str:
        return self._web_password

    @property
    def link_psk(self) -> str:
        return self._link_psk

    @property
    def site_name(self) -> str:
        """What this installation calls itself, shown in the panel.

        The club's name is configuration, not source: the same code runs at
        the next site under a different one.
        """
        return str(self._get("web", "title", default="RKNN surveillance"))

    @property
    def web_auth_required(self) -> bool:
        return bool(self.web.get("auth_required", True))

    # ------------------------------------------------------------- internals
    def _req(self, *keys):
        node, seen = self.raw, []
        for k in keys:
            seen.append(k)
            if not isinstance(node, dict) or k not in node:
                raise ConfigError(
                    f"{self.path.name} is missing required setting: {'.'.join(seen)}")
            node = node[k]
        return node

    def _get(self, *keys, default=None):
        node = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(config_path=None, secrets_path=None, require_password=True,
         local_path=None) -> Config:
    """Load config.yaml, overlay config.local.yaml and secrets.yaml.

    Three layers, in order: the deployed defaults, whatever is particular to
    this board, and the secrets. Only the first is ever overwritten by a
    deploy, so editing the camera's address on the board sticks.
    """
    config_path = Path(config_path or os.environ.get("RKNN_CONFIG", DEFAULT_CONFIG))
    secrets_path = Path(secrets_path or os.environ.get("RKNN_SECRETS", DEFAULT_SECRETS))
    local_path = Path(local_path or os.environ.get("RKNN_LOCAL", DEFAULT_LOCAL))

    if not config_path.exists():
        raise ConfigError(f"config file not found: {config_path}")
    raw = yaml.safe_load(config_path.read_text()) or {}

    if local_path.exists():
        overlay = yaml.safe_load(local_path.read_text()) or {}
        raw = _deep_merge(raw, overlay)

    if secrets_path.exists():
        mode = secrets_path.stat().st_mode & 0o077
        if mode:
            raise ConfigError(
                f"{secrets_path} is readable by others (mode {oct(mode)}). "
                f"Run: chmod 600 {secrets_path}")
        raw = _deep_merge(raw, yaml.safe_load(secrets_path.read_text()) or {})

    password = os.environ.get("RKNN_CAMERA_PASSWORD") or \
        (raw.get("camera") or {}).get("password") or ""
    web_password = os.environ.get("RKNN_WEB_PASSWORD") or \
        (raw.get("web") or {}).get("password") or ""
    link_psk = os.environ.get("RKNN_LINK_PSK") or \
        (raw.get("link") or {}).get("psk") or ""

    # Keep secrets out of the parsed tree so a stray dump can't leak them.
    if isinstance(raw.get("camera"), dict):
        raw["camera"] = {k: v for k, v in raw["camera"].items() if k != "password"}
    if isinstance(raw.get("web"), dict):
        raw["web"] = {k: v for k, v in raw["web"].items() if k != "password"}
    if isinstance(raw.get("link"), dict):
        raw["link"] = {k: v for k, v in raw["link"].items() if k != "psk"}

    cfg = Config(raw=raw, path=config_path, _password=password,
                 _web_password=web_password, _link_psk=link_psk)

    if require_password and not password:
        raise ConfigError(
            f"no camera password. Copy secrets.example.yaml to "
            f"{secrets_path.name}, chmod 600 it, or set RKNN_CAMERA_PASSWORD.")

    # Touch the properties that would otherwise fail late, at 3am, in a service.
    cfg.camera_host, cfg.camera_port, cfg.camera_user, cfg.rtsp_port
    cfg.tiers, cfg.target_free_ratio, cfg.segment_seconds
    for t in cfg.recording_tiers:
        cfg.rtsp_url(t.stream, redacted=True)
    cfg.detection_rtsp

    return cfg
