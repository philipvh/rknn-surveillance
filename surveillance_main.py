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

"""Entry point: recorders + clip cutter + detection loop."""

import logging
import signal
import sys

import datetime as dt
import json
import threading
import time

import argparse

import config
import ptz as ptz_mod
import recorder as recorder_mod
import trigger as trigger_mod
from alerts import AlertPolicy, ShadowLog
from annotated import AnnotatedClip
from capture import CaptureManager
from announcer import Announcer
from tracker import Tracker
from health import ClockGuard, HealthMonitor, SystemdNotifier
from concat_mgr import ConcatJob, ConcatManager
from controller import Controller
from datausage import DataUsage
from frames import LatestFrame
from schedule import Schedule
from segments import DAY_FMT, TS_FMT, list_segments_between
from settings import Settings
from surveillance_core import run_surveillance
from uplink import Uplink, disk_health
from webapp import create_app


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,          # journald picks this up
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="RKNN surveillance")
    ap.add_argument("--no-record", action="store_true",
                    help="run detection, PTZ and the panel but write no video. "
                         "For bench testing, and for a board whose disk is too "
                         "small to record onto.")
    args = ap.parse_args(argv)

    setup_logging()
    log = logging.getLogger("main")

    # Two passes. The settings store lives beside the recordings, and where
    # that is comes from the config -- but the config's last layer is the
    # settings store. So: load once to find the store, read it, load again
    # with its overrides applied. The first load is discarded.
    try:
        bootstrap = config.load()
        user_settings = Settings(
            bootstrap.events_root.parent / "settings.json",
            defaults={"trigger_classes": sorted(bootstrap.trigger_classes)})
        overrides = user_settings.config_overrides
        cfg = config.load(overrides=overrides) if overrides else bootstrap
    except config.ConfigError as e:
        log.error("%s", e)
        return 2

    if overrides:
        log.info("config overrides from the panel: %s",
                 ", ".join(sorted(overrides)))
        # The store may have moved with the overrides -- if the data root was
        # changed, settings.json belongs at the new one from now on.
        if cfg.events_root.parent != bootstrap.events_root.parent:
            log.warning("the data root moved to %s; the settings file follows "
                        "it on the next start", cfg.events_root.parent)

    # Before anything is written with a timestamp on it.
    clock_guard = ClockGuard(cfg.resolve(
        cfg._get("health", "clock_state", default="state/clock.json")))
    if not clock_guard.ok:
        log.error("continuing with a clock that cannot be trusted -- recordings "
                  "will be filed under the wrong date until it is fixed")

    notifier = SystemdNotifier()
    if notifier.enabled:
        log.info("running under systemd%s", f", watchdog every "
                 f"{notifier.watchdog_interval_s * 2:.0f}s"
                 if notifier.watchdog_interval_s else "")

    log.info("camera %s:%s as %s", cfg.camera_host, cfg.camera_port, cfg.camera_user)
    for t in cfg.tiers:
        log.info("tier %-11s %-28s keep %s%s", t.name, t.path,
                 f"{t.max_age_days}d" if t.max_age_days else "forever",
                 "  [protected]" if t.protected else "")

    if args.no_record:
        log.warning("--no-record: detection and the panel run, but nothing is "
                    "written to disk and no clips can be cut")
        recorders = []
    else:
        recorders = recorder_mod.recorders_from_config(cfg)
        for r in recorders:
            r.start()

    health = HealthMonitor(cfg, recorders=recorders, clock_guard=clock_guard,
                           notifier=notifier)

    if user_settings.overridden("trigger_classes"):
        log.info("trigger classes overridden from the panel: %s",
                 sorted(user_settings.trigger_classes))

    concat_mgr = ConcatManager(recorders=recorders)
    concat_mgr.start()

    def recover_pending_clips():
        """Re-queue clips that were promised but never written.

        The queue lives in memory, so a restart between an incident closing
        and its concat finishing used to lose the clip silently -- and the
        capture sweep would then delete the segments, taking the footage with
        it. The sidecar is written first and is the record of intent, so a
        sidecar with no mp4 beside it means an unfinished cut.
        """
        main = cfg.tier("main")
        for side in sorted(cfg.events_root.rglob("*.json")):
            # The open-incident marker lives here too and is not a sidecar;
            # recover_open_incident() owns it.
            if side.name == "incident_open.json":
                continue
            out = side.with_suffix(".mp4")
            if out.exists():
                continue
            try:
                meta = json.loads(side.read_text())
                t0 = dt.datetime.fromisoformat(meta["t0"])
                end = dt.datetime.fromisoformat(meta["window_end"])
            except (OSError, ValueError, KeyError) as e:
                log.warning("unreadable clip sidecar %s: %s", side.name, e)
                continue
            segs = list_segments_between(t0, end, main.path, cfg.segment_seconds)
            if segs:
                log.warning("resuming an unfinished clip from before the "
                            "restart: %s (%d segment(s))", out.name, len(segs))
                concat_mgr.submit(ConcatJob(
                    segs, out,
                    delete_sources=bool(cfg._get(
                        "capture", "delete_sources_after_concat", default=True))))
            else:
                ann = out.with_suffix("")
                ann = ann.with_name(ann.name + ".annotated.mp4")
                if ann.exists():
                    # Not an error any more: the media browser lists the
                    # companion on its own, so the event is still watchable.
                    log.warning(
                        "clip %s was never cut and its segments are gone; "
                        "the annotated companion and the stills remain and "
                        "are browsable", out.name)
                else:
                    log.error(
                        "clip %s was never cut, its segments are gone and "
                        "there is no companion -- that event has no footage "
                        "at all", out.name)

    try:
        recover_pending_clips()
    except Exception:
        log.exception("could not check for unfinished clips")

    schedule = Schedule.from_config(cfg)
    log.info("schedule: %s", schedule.describe())

    policy = AlertPolicy(cfg, schedule)
    shadow_root = cfg.resolve(
        cfg._get("alerts", "shadow_root", default="shadow"))
    shadow = ShadowLog(shadow_root)
    if cfg._get("alerts", "shadow_only", default=True):
        log.info("alerting is in SHADOW MODE: decisions are written to %s "
                 "and nothing is sent", shadow_root)

    uplink = Uplink(cfg)
    uplink.start()

    # Every decision goes to the shadow log; the uplink additionally queues
    # the ones that passed, and only once shadow mode is switched off.
    _record = shadow.record

    def record_and_queue(incident, decision, when=None):
        path = _record(incident, decision, when=when)
        try:
            uplink.on_decision(incident, decision,
                               health=disk_health(cfg.events_root))
        except Exception:
            log.exception("could not queue an alert for the radio link")
        return path

    shadow.record = record_and_queue

    ptz = ptz_mod.PTZ(cfg)

    main_tier = cfg.tier("main")
    live = LatestFrame()
    capture = CaptureManager(cfg, main_tier)
    log.info("capture: %d clip(s) kept while ready, everything kept while "
             "triggered", capture.ready_keep)

    # A window that is open right now, recorded on disk. Only one incident is
    # ever open, so one file is enough.
    marker = cfg.events_root / "incident_open.json"

    def mark_incident_open(start):
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(
                {"start": start.isoformat(timespec="seconds")}, indent=1))
        except OSError as e:
            log.warning("could not write the incident marker: %s", e)

    def mark_incident_done():
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not clear the incident marker: %s", e)

    def cut_clip(start, end):
        """Turn an incident window into a clip from the main tier."""
        segs = list_segments_between(start, end, main_tier.path,
                                     cfg.segment_seconds)
        if not segs:
            log.warning("no main-tier segments cover %s -> %s; no clip",
                        start, end)
            return None
        # Name it after the first segment's actual start, not the requested
        # window. Segments are whole minutes, so the file's first frame can be
        # up to a minute before the window began -- and a player seeking to
        # "window start" would land in the wrong place by that much.
        # A hard ceiling on the cut itself. controller.max_hold_s already
        # closes a long incident, but the quiet period runs past it and a
        # recovered window is not bounded at all -- and one enormous file is
        # worse than two, both to cut and to seek in.
        cap = float(cfg._get("capture", "max_clip_seconds", default=600.0))
        limit = max(1, int(cap // max(1, cfg.segment_seconds)))
        if len(segs) > limit:
            log.warning("window is %d segment(s); cutting the first %d "
                        "(%.0fs cap) and dropping the rest",
                        len(segs), limit, cap)
            segs = segs[:limit]

        from segments import parse_seg_start
        t0 = parse_seg_start(segs[0]) or start
        day_dir = cfg.events_root / t0.strftime(DAY_FMT)
        day_dir.mkdir(parents=True, exist_ok=True)
        out = day_dir / (f"clip_{t0.strftime(TS_FMT)}"
                         f"_{end.strftime(TS_FMT)}.mp4")
        # The sources go only after the clip exists and passes validation.
        drop = bool(cfg._get("capture", "delete_sources_after_concat",
                             default=True))
        log.info("cutting %d segment(s) into %s%s", len(segs), out.name,
                 " (sources will be removed)" if drop else "")
        concat_mgr.submit(ConcatJob(segs, out, delete_sources=drop))

        # A sidecar recording when this file's first frame actually happened.
        # The name carries it too, but the annotated companion starts later,
        # and a player needs each file's own t0 to seek to a wall-clock moment.
        try:
            side = out.with_suffix(".json")
            side.write_text(json.dumps({
                "t0": t0.isoformat(timespec="seconds"),
                "window_start": start.isoformat(timespec="seconds"),
                "window_end": end.isoformat(timespec="seconds"),
                "segments": len(segs),
            }, indent=1))
        except OSError as e:
            log.warning("could not write the clip sidecar: %s", e)
        return out

    def recover_open_incident():
        """Cut a clip for a window that was open when the process died.

        A clean stop closes the incident itself. This is for the other ways a
        process ends -- kill -9, a watchdog bounce, the power going -- where
        the only trace is the marker and the segments still on the card. The
        stills for that event exist, so without this the browser shows
        thumbnails for something that has no video at all.
        """
        try:
            meta = json.loads(marker.read_text())
            start = dt.datetime.fromisoformat(meta["start"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError) as e:
            log.warning("unreadable incident marker: %s", e)
            mark_incident_done()
            return
        main = cfg.tier("main")
        end = dt.datetime.now()
        if list_segments_between(start, end, main.path, cfg.segment_seconds):
            log.warning("an incident was open when the service stopped "
                        "(since %s); cutting its clip now",
                        start.strftime("%H:%M:%S"))
            cut_clip(start, end)
        else:
            log.error("an incident was open when the service stopped "
                      "(since %s) but its segments are gone; that event has "
                      "stills only", start.strftime("%H:%M:%S"))
        mark_incident_done()

    # The per-second dump writes an annotated JPEG for every triggered frame,
    # so a separate "_alert" snapshot was writing a second near-identical file
    # under a second naming scheme. The incident now simply points at the
    # first JPEG of its own window.
    def take_snapshot():
        return ""

    # Off by default: the per-second JPEGs already show the boxes, they are
    # browsable, and they cost nothing extra to produce. Turn it on if you
    # want the same thing as a playable clip.
    annotated = None
    if cfg._get("capture", "annotated_clip", default=True):
        annotated = AnnotatedClip(
            fps=float(cfg._get("detection", "target_fps", default=2)),
            max_frames=int(cfg._get("recording", "annotated_max_frames",
                                    default=1200)))
        log.info("annotated companion clips are on")
    tracker = Tracker(cfg, ptz)
    announcer = Announcer(cfg, schedule)
    if tracker.enabled:
        log.info("auto-tracking on: dead zone %.0f%%, at most %d pulses per "
                 "incident", tracker.dead_zone * 100,
                 tracker.max_pulses_per_incident)
    if announcer.enabled:
        log.info("spoken warning on: armed hours only, at most %d a day",
                 announcer.max_per_day)

    try:
        recover_open_incident()
    except Exception:
        log.exception("could not recover the interrupted incident")

    controller = Controller(cfg, ptz, schedule, policy, shadow,
                            clip_fn=cut_clip, snapshot_fn=take_snapshot,
                            mark_open_fn=mark_incident_open,
                            mark_done_fn=mark_incident_done,
                            tracker=tracker, announcer=announcer,
                            annotated=annotated, capture=capture,
                            settings=user_settings)

    # Panel-set motor tuning, applied before anything can move. The camera's
    # own speed setting has otherwise never been sent: config carried a
    # ptz.speed for a long time that nothing ever applied.
    try:
        ptz.apply_tuning(speed=(user_settings.sweep_speed
                                if user_settings.sweep_speed is not None
                                else cfg._get("ptz", "speed", default=None)),
                         auto_budget_s=user_settings.sweep_budget_s)
    except Exception:
        log.exception("could not apply the camera tuning")

    trigger = trigger_mod.TriggerInput(cfg, on_event=controller.on_pir)
    trigger.start()

    # The mobile-data meter. Sampling the kernel's own counters needs no root
    # and no extra package -- and it is the same number the bundle is billed on.
    u = (cfg._get("usage", default={}) or {})
    usage = DataUsage(
        cfg.resolve(u.get("state", "state/usage.json")),
        iface=u.get("interface") or None,
        limit_gb=u.get("limit_gb", 0),
        billing_day=u.get("billing_day", 1))
    if usage.iface:
        log.info("metering data on %s%s", usage.iface,
                 f", bundle {usage.limit_gb:g} GB" if usage.limit_gb else "")

    def usage_loop():
        while True:
            try:
                usage.sample()
            except Exception:
                log.exception("could not sample the data counter")
            time.sleep(60)

    threading.Thread(target=usage_loop, daemon=True, name="usage").start()

    # The wall panel runs in this process on purpose: it then drives the same
    # PTZ object as the controller, so every button goes through the same
    # deadline watchdog and the same motor budget. A separate service would
    # need a second PTZ client and the guarantee would stop being structural.
    web = cfg.web or {}
    app = create_app(cfg, ptz=ptz, controller=controller, schedule=schedule,
                     health=health, announcer=announcer, live=live,
                     concat=concat_mgr, settings=user_settings, usage=usage)

    def serve():
        try:
            app.run(host=web.get("bind", "0.0.0.0"),
                    port=int(web.get("port", 8080)),
                    threaded=True, debug=False, use_reloader=False)
        except Exception:
            log.exception("the web panel stopped; detection continues")

    threading.Thread(target=serve, daemon=True, name="webapp").start()

    def heartbeat_loop():
        """Silence must be distinguishable from a quiet week."""
        while True:
            time.sleep(300)
            try:
                h = health.status(cfg.events_root)
                h["armed"] = schedule.is_armed()
                h["camera_bad"] = not h["recorders_ok"] or not h["frames_fresh"]
                h["events_today"] = controller.incidents_closed
                uplink.maybe_heartbeat(h)
            except Exception:
                log.exception("heartbeat failed")

    threading.Thread(target=heartbeat_loop, daemon=True,
                     name="heartbeat").start()

    def camera_clock_loop():
        """Keep the camera's clock right, because it cannot itself.

        The camera is on the isolated segment with no route to NTP, and its
        firmware ignores its own DST flag, so left alone its clock free-runs
        and stays on winter time all summer. The board has NTP and handles DST,
        so it pushes UTC plus the correct offset -- on startup and hourly, the
        hour chosen so a DST changeover is corrected within the hour and a
        free-running drift never grows large. A wrong timestamp burned into a
        vandalism clip is the kind of error found only when the footage is
        needed, which is too late.
        """
        while True:
            try:
                ptz.set_time()
                log.info("camera clock synced")
            except Exception as e:
                log.warning("could not sync the camera clock: %s", e)
            time.sleep(3600)

    if getattr(ptz, "enabled", False):
        threading.Thread(target=camera_clock_loop, daemon=True,
                         name="camera-clock").start()

    log.info("wall panel on http://%s:%s/",
             web.get("bind", "0.0.0.0"), web.get("port", 8080))

    def shutdown(signum, _frame):
        log.info("signal %s, shutting down", signum)
        # Close an incident that is still open before anything else stops, so
        # its clip is cut and its annotated companion written. Otherwise the
        # event survives only as stills.
        try:
            if controller.close_open_incident("service is stopping"):
                log.info("closed an open incident before shutting down")
        except Exception:
            log.exception("could not close the open incident")
        trigger.stop()
        ptz.close()
        for r in recorders:
            r.stop()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Tell systemd we are up. Anything that fails after this point is a
    # running-but-broken service, which is exactly what the watchdog is for.
    notifier.ready()
    notifier.status("running")

    try:
        run_surveillance(cfg, concat_mgr, controller, recorders=recorders,
                         health=health, live=live, annotated=annotated,
                         capture=capture, settings=user_settings)
    except KeyboardInterrupt:
        pass
    finally:
        notifier.stopping()
        try:
            controller.close_open_incident("service is stopping")
        except Exception:
            log.exception("could not close the open incident")
        # Let a cut that is queued or running finish before we go. Without
        # this the job dies with the process and the footage is only
        # recoverable from the sidecar on the next start.
        try:
            drained = concat_mgr.drain(timeout=90)
            if not drained:
                log.warning("a clip was still being cut at shutdown; it will "
                            "be resumed on the next start")
        except Exception:
            log.exception("could not drain the clip queue")
        trigger.stop()
        uplink.stop()
        try:
            ptz.close()
        except Exception:
            pass
        for r in recorders:
            r.stop()
        for r in recorders:
            r.join(timeout=5)
        trigger.join(timeout=2)
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
