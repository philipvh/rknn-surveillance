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

"""Detection loop and trigger logic.

Reads the detection stream, runs YOLOv10 on the NPU, and turns runs of
sightings into a single clip cut out of the main recording tier.

Everything that used to be a module-level constant here now comes from
config.yaml, so the RTSP URL and credentials are declared exactly once.
"""

import datetime as dt
import logging
import time
from pathlib import Path

import cv2
from rknn.api import RKNN

import preprocess
import retention
from concat_mgr import ConcatJob
from controller import Detection
from segments import DAY_FMT, TS_FMT, list_segments_between, pinned_paths
from yolov10 import post_process_yolov10, CLASSES

log = logging.getLogger("surveillance")

# ------------------------------------------------------------------ main entry

def run_surveillance(cfg, concat_mgr, controller, recorders=(), health=None,
                     live=None, annotated=None, capture=None, settings=None):
    det = cfg.detection
    target_fps = float(det.get("target_fps", 2))
    conf_threshold = float(det.get("conf_threshold", 0.6))
    # What to draw on the detector view but not act on. Lower it while tuning
    # to see what the model is scoring, rather than guessing why nothing fired.
    debug_draw = float(det.get("debug_draw_threshold", 0.4))
    # Keep the last N annotated frames on disk while tuning. The overlay says
    # what the model scored, but only for the second it is on screen; this is
    # what lets a walk-through be examined afterwards.
    debug_frames = int(det.get("debug_keep_frames", 0))
    debug_dir = cfg.resolve("debug_frames")
    if debug_frames:
        debug_dir.mkdir(parents=True, exist_ok=True)
        for old_f in sorted(debug_dir.glob("*.jpg")):
            old_f.unlink(missing_ok=True)
        log.warning("keeping the last %d annotated frames in %s",
                    debug_frames, debug_dir)
    debug_n = 0
    # The panel can change these while the loop runs, so read them per frame
    # from the settings overlay rather than binding the set once at startup.
    # Falling back to the config keeps behaviour identical when no override
    # has ever been saved.
    def current_triggers():
        if settings is not None:
            return settings.trigger_classes
        return cfg.trigger_classes

    trigger_classes = current_triggers()

    jpeg_interval_s = float(cfg._get("capture", "jpeg_interval_s", default=1.0))
    last_jpeg = 0.0

    trg = cfg.trigger
    quiet_period_s = float(trg.get("quiet_period_s", 15.0))
    pre_roll_s = float(trg.get("pre_roll_s", 2.0))
    post_roll_s = float(trg.get("post_roll_s", 15.0))
    max_duration = dt.timedelta(minutes=float(trg.get("max_duration_min", 10)))

    main_tier = cfg.tier("main")
    events_root = cfg.events_root
    detections_root = cfg.detections_root
    segment_seconds = cfg.segment_seconds
    retention_interval = cfg.retention_interval_s

    events_root.mkdir(parents=True, exist_ok=True)
    detections_root.mkdir(parents=True, exist_ok=True)

    log.info("detector reading %s",
             cfg.rtsp_url(det.get("source", "sub"), redacted=True))
    log.info("trigger classes: %s", sorted(trigger_classes))
    prep = preprocess.from_config(cfg)
    log.info("preprocessing: %s", type(prep).__name__.lower())

    rknn = RKNN()
    rknn.load_rknn(det.get("model", "./model/yolov10.rknn"))
    rknn.init_runtime(target=det.get("target", "rk3588"))

    cap = cv2.VideoCapture(cfg.detection_rtsp, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 0)
    if not cap.isOpened():
        log.error("could not open the detection stream")
        try:
            rknn.release()
        except Exception:
            pass
        raise SystemExit(1)

    for _ in range(5):
        cap.read()
        time.sleep(0.05)

    last_infer = 0.0
    min_interval = 1.0 / target_fps
    last_maintenance = time.time()

    log.info("starting YOLOv10 detection loop")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.2)
                continue

            # Counted before any gating: a frame arriving is what proves the
            # capture pipeline is alive, whether or not we infer on it.
            if health is not None:
                health.note_frame()

            now = time.time()
            if (now - last_infer) < min_interval:
                continue
            last_infer = now

            controller.tick()
            if health is not None:
                health.tick(main_tier.path)

            # Frames taken while the camera is panning are motion-blurred: a
            # waste of NPU time and a source of false positives.
            if not controller.detection_enabled():
                continue

            h, w = frame.shape[:2]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            inp = prep.apply(rgb)

            outs = rknn.inference(inputs=[inp])
            boxes, classes, scores = post_process_yolov10(outs)

            def to_frame(box):
                return prep.to_frame(box, w, h)

            hit = False
            others = []
            seen = 0
            best = 0.0
            labels = set()
            scaled_boxes = []
            scaled_scores = []
            if boxes is not None:
                trigger_classes = current_triggers()
                for box, cls, score in zip(boxes, classes, scores):
                    # The COCO labels carry trailing spaces on several entries
                    # ('motorbike ', 'bus ', 'truck '). Without .strip() those
                    # three classes silently never matched, which is how the
                    # previous version only ever triggered on person and bicycle.
                    label = (CLASSES[cls].strip() if cls < len(CLASSES)
                             else f"id:{cls}")
                    if label not in trigger_classes or score < conf_threshold:
                        # Drawn dim on the detector view so it is obvious the
                        # model is working even when nothing triggers -- the
                        # difference between "not detecting" and "detecting
                        # things we do not care about".
                        if live is not None and score >= debug_draw:
                            others.append((to_frame(box), label, float(score)))
                        continue
                    x1, y1, x2, y2 = to_frame(box)
                    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                    cv2.putText(frame, f"{label} {score:.2f}", (x1, y1 - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                    log.info("%s @ (%d,%d,%d,%d) score=%.2f",
                             label, x1, y1, x2, y2, score)
                    hit = True
                    seen += 1
                    best = max(best, float(score))
                    labels.add(label)
                    scaled_boxes.append([x1, y1, x2, y2])
                    scaled_scores.append(float(score))

            # Publish what the NPU just looked at, boxes and all, so the
            # panel can answer "is it seeing me?" without waiting for a clip.
            if live is not None:
                annotated_frame = frame
                if others:
                    annotated_frame = frame if hit else frame.copy()
                    for (x1, y1, x2, y2), lbl, sc in others:
                        cv2.rectangle(annotated_frame, (x1, y1), (x2, y2),
                                      (140, 140, 140), 1)
                        cv2.putText(annotated_frame, f"{lbl} {sc:.2f}",
                                    (x1, y1 - 4), cv2.FONT_HERSHEY_SIMPLEX,
                                    0.45, (160, 160, 160), 1, cv2.LINE_AA)
                # Call out near-misses explicitly: "person 0.42" when the
                # threshold is 0.60 is the single most useful thing to know.
                near = [(l, sc) for (_b, l, sc) in others if l in trigger_classes]
                if hit:
                    summary = f"TRIGGER: {seen}x {'/'.join(sorted(labels))} @ {best:.2f}"
                elif near:
                    near.sort(key=lambda x: -x[1])
                    summary = ("BELOW THRESHOLD: "
                               + ", ".join(f"{l} {sc:.2f}" for l, sc in near[:3]))
                else:
                    summary = (f"no trigger class; {len(others)} other detection(s)"
                               if others else "nothing detected")
                if debug_frames:
                    debug_n += 1
                    cv2.imwrite(str(debug_dir / f"{debug_n % debug_frames:04d}.jpg"),
                                annotated_frame)
                    if near or hit:
                        log.info("detector: %s", summary)
                # Buffer the annotated frame while an incident is open, so a
                # clip showing what the model saw can be written when it closes.
                if annotated is not None:
                    ok_enc, buf = cv2.imencode(
                        ".jpg", annotated_frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), 75])
                    if ok_enc:
                        annotated.add(buf.tobytes(), when=dt.datetime.now())

                live.publish(annotated_frame, overlay=[
                    dt.datetime.now().strftime("%H:%M:%S")
                    + f"   state={controller.state.value}"
                    + ("   ARMED" if controller.schedule.is_armed() else ""),
                    summary,
                    f"threshold {conf_threshold:.2f}   "
                    f"triggers: {' '.join(sorted(trigger_classes))}",
                ])

            # A JPEG a second, but only while triggered and only for frames
            # that actually contain a trigger class. Frames with nothing in
            # them are not worth keeping, and between incidents nothing is
            # written at all.
            if (hit and controller.triggered
                    and (now - last_jpeg) >= jpeg_interval_s):
                stamp = dt.datetime.now()
                day_dir = detections_root / stamp.strftime(DAY_FMT)
                day_dir.mkdir(parents=True, exist_ok=True)
                jpg = day_dir / (stamp.strftime(TS_FMT) + ".jpg")
                cv2.imwrite(str(jpg), annotated_frame)
                controller.note_snapshot(jpg)
                last_jpeg = now

            # ---- hand the frame's sightings to the controller ----
            if hit:
                controller.on_detection(Detection(
                    at=dt.datetime.now(), count=seen,
                    max_confidence=best, labels=labels))
                # Boxes are already in frame coordinates here.
                controller.track(scaled_boxes, scaled_scores, w, h)

            # ---- segment retention: ready prunes, triggered keeps ----
            if capture is not None:
                try:
                    # The concat queue is the authority on what is still
                    # needed: a clip queued moments ago has not been cut yet.
                    opened = controller.open_incident_start()
                    capture.sweep(pinned=pinned_paths(
                        concat_mgr, main_tier.path,
                        (opened - dt.timedelta(seconds=pre_roll_s)
                         if opened else None),
                        segment_seconds))
                except Exception:
                    log.exception("capture sweep failed")

            # ---- retention ----
            if (time.time() - last_maintenance) > retention_interval:
                last_maintenance = time.time()
                opened = controller.open_incident_start()
                since = (opened - dt.timedelta(seconds=pre_roll_s)
                         if opened else None)
                try:
                    retention.run_once(
                        cfg,
                        pinned=pinned_paths(concat_mgr, main_tier.path,
                                            since, segment_seconds),
                    )
                except Exception as e:
                    log.exception("retention sweep failed: %s", e)

                for r in recorders:
                    if not r.healthy:
                        log.warning("recorder %r has not written a segment "
                                    "recently (%d restarts)", r.name_, r.restarts)

            time.sleep(0.001)

    except KeyboardInterrupt:
        pass
    finally:
        log.info("cleaning up")
        try:
            cap.release()
        except Exception:
            pass
        try:
            rknn.release()
        except Exception:
            pass
