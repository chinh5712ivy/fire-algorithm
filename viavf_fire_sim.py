"""Offline replay of the VIAVF fire gate and stationary-headlight filter."""
import argparse
from collections import Counter
from dataclasses import dataclass
import json
from math import dist, isfinite
from pathlib import Path
import sys

MIN_CONFIDENCE = 0.25
MIN_SIDE = 9
TRACK_TIMEOUT = 3
AREA_RATIO = 0.005
ANCHOR_IOU = 0.90
ROOT = Path(__file__).resolve().parent
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm"}

sys.path.insert(0, str(ROOT / "mini_viavf_stage"))
from tracker import Tracker, area, center, iou


@dataclass
class StationaryFire:
    anchor: tuple | None = None
    since: float = 0
    last_seen: float = 0
    frame_size: tuple | None = None
    observed_motion: bool = False
    anchor_iou: float | None = None

    def observe(self, bbox, size, now):
        width, height = size
        clipped = tuple(min(bound, max(0, value))
                        for value, bound in zip(bbox, (width, height, width, height)))
        clipped_area = area(clipped)
        ratio = clipped_area / (width * height)
        if clipped_area < width * height * AREA_RATIO:
            self.__init__()
            return ratio
        if self.frame_size != size or now - self.last_seen > TRACK_TIMEOUT:
            self.__init__()
        self.frame_size = size
        self.last_seen = now
        if self.anchor is None:
            self.anchor = clipped
            self.since = now
            self.anchor_iou = 1.0
        else:
            self.anchor_iou = iou(self.anchor, clipped)
            if self.anchor_iou < ANCHOR_IOU:
                self.anchor = clipped
                self.since = now
                self.observed_motion = True
        return ratio

    def decision(self, duration):
        if self.anchor is None:
            return "pass_small"
        if self.elapsed + 1e-9 >= duration:
            return "suppressed"
        if self.observed_motion:
            return "pass_moving"
        return "holding"

    @property
    def elapsed(self):
        return self.last_seen - self.since if self.anchor is not None else None


def fire_gate(track, now):
    if now - track.history[0]["timestamp"] + 1e-9 < 1:
        return "dwell"
    x1, y1, x2, y2 = track.latest["bbox"]
    if max(x2 - x1, y2 - y1) <= 36:
        return "pass"
    boxes = [d["bbox"] for d in track.history if now - d["timestamp"] <= 1 + 1e-9][-64:]
    pairs = list(zip(boxes, boxes[1:]))
    if not pairs:
        return "pass"
    if any(dist(center(a), center(b)) >= 4
           or abs(area(a) - area(b)) / min(area(a), area(b)) >= 0.12 for a, b in pairs):
        return "pass"
    return "pass" if sum(iou(a, b) for a, b in pairs) / len(pairs) < 0.98 else "static"


class Replay:
    def __init__(self, duration):
        self.duration = duration
        self.tracker = Tracker({"track_iou": 0.15, "track_center_px": 100,
                                "track_max_gap_s": TRACK_TIMEOUT, "history_s": float("inf")})
        self.stationary = {}
        self.previous_time = -1

    def step(self, now, size, detections):
        if not isfinite(now) or now < 0 or now <= self.previous_time:
            raise ValueError("Frame timestamps must be finite, nonnegative and strictly increasing")
        if len(size) != 2 or any(not isfinite(side) or side <= 0 for side in size):
            raise ValueError("Frame width and height must be positive and finite")
        self.previous_time = now
        accepted, rows = [], []
        for detection in detections:
            if detection["class_name"] != "fire":
                continue
            bbox = detection["bbox"]
            confidence = detection["confidence"]
            row = {"bbox": bbox, "confidence": confidence, "track_id": None,
                   "stationary_filter": None}
            if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or not all(
                isinstance(v, (int, float)) and isfinite(v) for v in bbox
            ):
                row["viavf_rule"] = "invalid_bbox"
            elif not isinstance(confidence, (int, float)) or not isfinite(confidence) or confidence < MIN_CONFIDENCE:
                row["viavf_rule"] = "confidence"
            elif min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < MIN_SIDE:
                row["viavf_rule"] = "min_side"
            else:
                accepted.append({**detection, "timestamp": now})
                continue
            rows.append(row)
        tracks = self.tracker.update(accepted, now)
        self.stationary = {t.id: self.stationary.get(t.id, StationaryFire()) for t in tracks}
        for track in tracks:
            state = self.stationary[track.id]
            ratio = state.observe(track.latest["bbox"], size, now)
            baseline = fire_gate(track, now)
            rows.append({"bbox": track.latest["bbox"], "confidence": track.latest["confidence"],
                         "track_id": track.id, "viavf_rule": baseline, "area_ratio": ratio,
                         "anchor_iou": state.anchor_iou, "stable_seconds": state.elapsed,
                         "stationary_filter": state.decision(self.duration) if baseline == "pass" else None})
            # Keep the dwell start while dropping samples outside the dynamics window.
            cutoff = now - 1 - 1e-9
            while len(track.history) > 2 and track.history[1]["timestamp"] < cutoff:
                del track.history[1]
        return rows



def create_detector(checkpoint):
    from detector import Detector

    if not checkpoint.is_file():
        raise ValueError("Supply a YOLOX-S fire/smoke checkpoint using --checkpoint")
    cfg = {
        "checkpoint": str(checkpoint),
        "exp_file": str(ROOT / "mini_viavf_stage" / "models" / "yolox_s_fire_smoke.py"),
        "class_names": ["fire", "smoke"],
        "device": "auto",
        "cpu_threads": 4,
        "fire_confidence": 0.01,
        "smoke_confidence": 0.55,
        "nms_iou": 0.45,
    }
    return Detector(cfg)
