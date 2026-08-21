#!/usr/bin/env python3
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

"""One command that says what is wrong.

    ./doctor.py                # everything
    ./doctor.py --camera       # only the checks that touch the camera
    ./doctor.py --quiet        # only failures, for cron

Run it after installing, after any change, and when something is behaving
oddly. Every check prints what to do about a failure rather than only that one
happened -- this box lives in a cupboard at a tennis club and the person
reading the output may not be the person who built it.
"""

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
MARK = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL ", SKIP: " skip "}

BASE = Path(__file__).resolve().parent


class Doctor:
    def __init__(self, quiet=False):
        self.quiet = quiet
        self.results = []

    def check(self, name, status, detail="", fix=""):
        self.results.append((name, status, detail, fix))
        if self.quiet and status in (OK, SKIP):
            return status
        print(f"[{MARK[status]}] {name}" + (f": {detail}" if detail else ""))
        if fix and status in (WARN, FAIL):
            for line in fix.strip().splitlines():
                print(f"          {line.strip()}")
        return status

    def section(self, title):
        if not self.quiet:
            print(f"\n── {title} " + "─" * max(0, 58 - len(title)))

    @property
    def failures(self):
        return [r for r in self.results if r[1] == FAIL]

    @property
    def warnings(self):
        return [r for r in self.results if r[1] == WARN]


def run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
        return 127, str(e)


# ------------------------------------------------------------------- checks

def check_config(d):
    d.section("Configuration")
    try:
        import config
        cfg = config.load()
        d.check("config.yaml loads", OK, f"camera {cfg.camera_host}:{cfg.camera_port}")
    except Exception as e:
        d.check("config.yaml loads", FAIL, str(e),
                "cp secrets.example.yaml secrets.yaml && chmod 600 secrets.yaml")
        return None

    local = BASE / "config.local.yaml"
    d.check("config.local.yaml", OK if local.exists() else WARN,
            "present" if local.exists() else "not found",
            """Board-specific settings belong here -- config.yaml is
            overwritten by every deploy. cp config.local.example.yaml
            config.local.yaml""" if not local.exists() else "")

    sec = BASE / "secrets.yaml"
    if sec.exists():
        mode = sec.stat().st_mode & 0o077
        d.check("secrets.yaml permissions", FAIL if mode else OK,
                oct(sec.stat().st_mode & 0o777),
                "chmod 600 secrets.yaml" if mode else "")
    else:
        d.check("secrets.yaml present", WARN, "not found",
                "cp secrets.example.yaml secrets.yaml && chmod 600 secrets.yaml")

    if cfg.web_auth_required and not cfg.web_password:
        d.check("wall panel password", FAIL, "auth is on but no password is set",
                "set web.password in secrets.yaml")
    else:
        d.check("wall panel password", OK)

    presets = cfg._get("ptz", "scan_presets", default=[]) or []
    home = cfg._get("ptz", "home_preset", default="Home")
    d.check("ptz presets configured", OK if presets else WARN,
            f"home={home}, scan={presets}",
            "set ptz.scan_presets in config.yaml, then create them on the "
            "camera with ./ptz_cli.py addpreset" if not presets else "")

    windows = cfg._get("schedule", "armed", default=[]) or []
    d.check("arming schedule", OK if windows else WARN,
            f"{len(windows)} window(s)",
            "with no armed windows nothing will ever alert; set schedule.armed"
            if not windows else "")
    return cfg


def check_clock(d):
    d.section("Clock (there is no NTP here)")
    now = dt.datetime.now()
    plausible = now.year >= 2025
    d.check("system clock plausible", OK if plausible else FAIL,
            now.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "sudo date -s 'YYYY-MM-DD HH:MM:SS' && sudo hwclock -w")

    if Path("/dev/rtc0").exists():
        if shutil.which("hwclock") is None:
            d.check("RTC present", WARN, "/dev/rtc0 exists, hwclock not installed",
                    "sudo apt install util-linux  (to read and write the RTC)")
        else:
            rc, out = run(["hwclock", "-r"])
            d.check("RTC present", OK if rc == 0 else WARN,
                    out.splitlines()[0] if out else "/dev/rtc0",
                    "could not read it; try: sudo hwclock -r" if rc else "")
    else:
        d.check("RTC present", FAIL, "no /dev/rtc0",
                """Without an RTC and without NTP, every power cut leaves the
                clock wrong, recordings filed under a nonsense date and clips
                unfindable. Fit a battery to the Rock 5B's RTC header.""")

    try:
        import config
        from health import ClockGuard
        cfg = config.load(require_password=False)
        g = ClockGuard(cfg.resolve(cfg._get("health", "clock_state",
                                            default="state/clock.json")),
                       write_interval_s=1e9)
        if g.regressed_by_s:
            d.check("clock has not gone backwards", FAIL,
                    f"lost {g.regressed_by_s/3600:.1f} hours since last run",
                    "the RTC battery is probably dead")
        else:
            d.check("clock has not gone backwards", OK,
                    f"last seen {g.last_known}" if g.last_known else "first run")
    except Exception as e:
        d.check("clock history", SKIP, str(e))


def check_storage(d, cfg):
    d.section("Storage")
    rc, out = run(["lsblk", "-o", "NAME,SIZE,TYPE"])
    has_nvme = "nvme" in out.lower()
    d.check("NVMe present", OK if has_nvme else WARN,
            "found" if has_nvme else "none",
            "continuous recording to an SD card will wear it out in months"
            if not has_nvme else "")

    if cfg:
        for tier in cfg.tiers:
            try:
                u = shutil.disk_usage(str(tier.path.parent))
                pct = u.used / u.total * 100
                status = FAIL if pct > 95 else WARN if pct > 90 else OK
                d.check(f"space for {tier.name}", status,
                        f"{pct:.0f}% used, {u.free/1e9:.0f} GB free",
                        "retention keeps continuous footage in check, but event "
                        "clips are never deleted for space -- they are the "
                        "evidence" if status != OK else "")
                break
            except OSError as e:
                d.check("disk usage", WARN, str(e))
                break


def check_deps(d):
    d.section("Dependencies")
    # Import name -> Debian package, where they differ.
    PACKAGES = {"cv2": "python3-opencv", "gpiod": "python3-libgpiod",
                "serial": "python3-serial", "yaml": "python3-yaml",
                "torch": "python3-torch", "flask": "python3-flask",
                "numpy": "python3-numpy", "cryptography": "python3-cryptography"}
    for mod, why, hard in (("yaml", "config", True), ("numpy", "detection", True),
                           ("cv2", "frame capture", True),
                           ("torch", "yolov10 post-processing", True),
                           ("flask", "wall panel", True),
                           ("cryptography", "radio link", False),
                           ("serial", "radio link", False),
                           ("gpiod", "PIR input", False)):
        try:
            __import__(mod)
            d.check(f"python {mod}", OK, why)
        except ImportError:
            d.check(f"python {mod}", FAIL if hard else WARN, f"missing ({why})",
                    f"sudo apt install {PACKAGES.get(mod, 'python3-' + mod)}")
    try:
        __import__("rknn.api")
        d.check("rknn toolkit", OK)
    except ImportError:
        d.check("rknn toolkit", FAIL, "missing",
                "install the RKNN toolkit for the RK3588 NPU")

    rc, out = run(["ffmpeg", "-version"])
    d.check("ffmpeg", OK if rc == 0 else FAIL,
            out.splitlines()[0] if rc == 0 else "not found",
            "sudo apt install ffmpeg")
    if rc == 0:
        try:
            sys.path.insert(0, str(BASE))
            import recorder
            caps = recorder.ffmpeg_caps()
            d.check("ffmpeg rtsp timeout option", 
                    OK if caps["rtsp_timeout_opt"] else WARN,
                    caps["rtsp_timeout_opt"] or "neither -timeout nor -stimeout",
                    "without it a dead socket can hang ffmpeg indefinitely"
                    if not caps["rtsp_timeout_opt"] else "")
        except Exception as e:
            d.check("ffmpeg capabilities", SKIP, str(e))


def check_gpio(d, cfg):
    d.section("PIR input")
    if not cfg:
        return
    c = cfg._get("trigger_input", default={}) or {}
    if not c.get("enabled", True):
        d.check("trigger input", SKIP, "disabled in config")
        return
    try:
        import trigger
        b = trigger.open_backend(c.get("chip"), c.get("line"),
                                 c.get("bias", "pull-up"))
        raw = b.read()
        b.close()
        active = (raw == 0) if c.get("active_low", True) else (raw == 1)
        d.check("GPIO line readable", OK,
                f"{c.get('chip')} line {c.get('line')} reads "
                f"{'ACTIVE' if active else 'idle'}")
        if active:
            d.check("PIR idle at rest", WARN, "the line is active right now",
                    "if nobody is in front of the sensor, active_low is "
                    "probably the wrong way round")
    except Exception as e:
        d.check("GPIO line readable", WARN, str(e),
                """gpiodetect; gpioinfo; check the user is in the gpio group.
                Not fatal: detection still runs on the parked view.""")


def check_camera(d, cfg):
    d.section("Camera")
    if not cfg:
        return
    rc, _ = run(["ping", "-c1", "-W2", cfg.camera_host], timeout=6)
    d.check("camera reachable", OK if rc == 0 else WARN,
            cfg.camera_host, "may simply be firewalled against ping")

    for stream in ("main", "sub"):
        url = cfg.rtsp_url(stream)
        rc, out = run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                       "-show_entries", "stream=codec_name,width,height",
                       "-of", "default=nw=1", url], timeout=20)
        if rc == 0 and out:
            d.check(f"{stream} stream", OK, " ".join(out.split()))
        else:
            d.check(f"{stream} stream", FAIL,
                    (out or "no answer").splitlines()[0][:80],
                    f"check camera.{stream}_path and camera.rtsp_port")

    try:
        import ptz as ptz_mod
        p = ptz_mod.PTZ(cfg, install_signal_handlers=False, stop_on_start=False)
        try:
            p.dev_state()
            d.check("CGI answers", OK)
            names = p.list_presets()
            want = set(cfg._get("ptz", "scan_presets", default=[]) or [])
            want.add(cfg._get("ptz", "home_preset", default="Home"))
            missing = sorted(want - set(names))
            d.check("presets exist on the camera", OK if not missing else FAIL,
                    f"camera has {names}",
                    f"missing {missing}: create them with ./ptz_cli.py addpreset"
                    if missing else "")
        finally:
            p._closed = True
    except Exception as e:
        d.check("CGI answers", FAIL, str(e)[:90],
                "./ptz_cli.py probe  -- the command names are expectations "
                "until a real camera confirms them")


# The unit name is derived from the directory, so a copy installed under a
# different name checks its own service rather than someone else's.
SERVICE = BASE.name.replace("_", "-")

# Services from the older rknn_yolov10 install. Left alone deliberately, but
# they cannot run at the same time as this one.
OLD_SERVICES = ("surveillance", "media-browser")


def check_service(d):
    d.section(f"Service ({SERVICE})")
    rc, out = run(["systemctl", "is-enabled", f"{SERVICE}.service"])
    d.check("enabled at boot", OK if out.strip() == "enabled" else WARN,
            out.strip() or "not installed",
            f"sudo systemctl enable {SERVICE}.service")
    rc, out = run(["systemctl", "is-active", f"{SERVICE}.service"])
    d.check("running", OK if out.strip() == "active" else WARN, out.strip(),
            f"sudo systemctl start {SERVICE}.service; "
            f"journalctl -u {SERVICE} -n50")

    for old in OLD_SERVICES:
        if old == SERVICE:
            continue
        rc, out = run(["systemctl", "is-active", f"{old}.service"])
        if out.strip() == "active":
            d.check(f"older {old}.service", FAIL, "still running",
                    f"""It will fight this install for port 8080 and for the
                    camera's RTSP session. Stop it when you switch over:
                    sudo systemctl disable --now {old}.service""")

    rc, out = run(["systemctl", "show", f"{SERVICE}.service",
                   "-p", "WatchdogUSec", "-p", "Restart", "-p", "NRestarts"])
    if rc == 0 and out:
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        n = vals.get("NRestarts", "0")
        d.check("restart count", WARN if n not in ("0", "") else OK,
                f"{n} restart(s), Restart={vals.get('Restart')}, "
                f"watchdog={vals.get('WatchdogUSec')}",
                f"journalctl -u {SERVICE} | grep -i error" if n not in ("0", "") else "")


def check_link(d, cfg):
    d.section("Radio link")
    if not cfg:
        return
    c = cfg._get("link", default={}) or {}
    shadow = cfg._get("alerts", "shadow_only", default=True)
    if shadow:
        d.check("alerting", SKIP,
                "shadow mode: decisions are logged, nothing is sent")
    if not c.get("enabled", False):
        d.check("radio link", SKIP, "disabled in config")
        return
    d.check("pre-shared key", OK if cfg.link_psk else FAIL,
            "set" if cfg.link_psk else "missing",
            "./link_cli.py genkey, then put it in secrets.yaml at both ends")
    if c.get("transport") == "serial":
        port = c.get("port", "/dev/ttyUSB0")
        d.check("radio bridge", OK if Path(port).exists() else FAIL, port,
                "check the USB radio is plugged in and the user is in dialout")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quiet", action="store_true", help="only warnings and failures")
    ap.add_argument("--camera", action="store_true", help="only camera checks")
    ap.add_argument("--no-camera", action="store_true", help="skip camera checks")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(BASE))
    d = Doctor(quiet=args.quiet)
    if not args.quiet:
        print(f"TVW camera doctor -- {dt.datetime.now():%Y-%m-%d %H:%M:%S}")

    cfg = check_config(d)
    if not args.camera:
        check_clock(d)
        check_storage(d, cfg)
        check_deps(d)
        check_gpio(d, cfg)
        check_service(d)
        check_link(d, cfg)
    if cfg and not args.no_camera:
        check_camera(d, cfg)

    print()
    if d.failures:
        print(f"{len(d.failures)} problem(s) to fix"
              + (f", {len(d.warnings)} warning(s)" if d.warnings else ""))
        return 1
    if d.warnings:
        print(f"no failures, {len(d.warnings)} warning(s) worth a look")
        return 0
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
