"""Calculate temporal features for existing fire tracks and confirm candidates."""

from dataclasses import dataclass
from math import ceil, floor, log

import cv2
import numpy as np


SIDE = 32
MAX_GAP_S = 0.31  # Allows normal jitter at 5 FPS; a skipped sample starts a new run.
EPS = 1e-8
FEATURES = ("hot_change", "side_change", "centered_change", "brightness_change",
            "warm_change", "aligned_hot_change", "area_change", "edge_smoothness")


def crop_features(frame, bbox):
    height, width = frame.shape[:2]
    left, top = max(0, floor(bbox[0])), max(0, floor(bbox[1]))
    right, bottom = min(width, ceil(bbox[2])), min(height, ceil(bbox[3]))
    if right <= left or bottom <= top:
        return None

    crop = cv2.resize(frame[top:bottom, left:right], (SIDE, SIDE),
                      interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255
    blue, green, red = cv2.split(crop)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    bright = red >= 245 / 255
    aligned_bright = bright
    if bright.any():
        yy, xx = np.nonzero(bright)
        shift = np.float32([[1, 0, (SIDE - 1) / 2 - xx.mean()],
                            [0, 1, (SIDE - 1) / 2 - yy.mean()]])
        aligned_bright = cv2.warpAffine(bright.astype(np.uint8), shift, (SIDE, SIDE),
                                         flags=cv2.INTER_NEAREST).astype(bool)
    # Use a continuous highlight image so changes near a color cutoff carry less weight.
    highlight = np.clip((red - 0.55) / 0.45, 0, 1)
    weights = highlight ** 4
    weight = float(weights.sum())
    if weight > EPS:
        yy, xx = np.indices(weights.shape, dtype=np.float32)
        dx = (SIDE - 1) / 2 - float((weights * xx).sum()) / weight
        dy = (SIDE - 1) / 2 - float((weights * yy).sum()) / weight
        centered = cv2.warpAffine(highlight, np.float32([[1, 0, dx], [0, 1, dy]]),
                                  (SIDE, SIDE), borderMode=cv2.BORDER_CONSTANT)
    else:
        centered = highlight
    warm = (red > 0.55) & (red - blue > 0.18) & (red - green > 0.07)
    return {
        "bright": bright,
        "aligned_bright": aligned_bright,
        "centered": centered,
        "gray_mean": float(gray.mean()),
        "edge_smoothness": max(0.0, 1 - float(np.abs(cv2.Laplacian(gray, cv2.CV_32F)).mean())),
        "warm_fraction": float(warm.mean()),
        "shape": (bbox[2] - bbox[0], bbox[3] - bbox[1]),
    }


def mask_change(a, b):
    union = int(np.count_nonzero(a | b))
    return int(np.count_nonzero(a ^ b)) / union if union >= 5 else 0.0


def compare(previous, current):
    shape_a = previous["centered"]
    shape_b = current["centered"]
    centered_change = float(np.abs(shape_a - shape_b).sum() /
                            max(EPS, float(np.maximum(shape_a, shape_b).sum())))
    dw, dh = (log(v / u) for u, v in zip(previous["shape"], current["shape"]))
    return {
        "hot_change": mask_change(previous["bright"], current["bright"]),
        "centered_change": centered_change,
        "side_change": (abs(dw) + abs(dh)) / 2,
        "brightness_change": abs(current["gray_mean"] - previous["gray_mean"]),
        "warm_change": abs(current["warm_fraction"] - previous["warm_fraction"]),
        "aligned_hot_change": mask_change(previous["aligned_bright"], current["aligned_bright"]),
        "area_change": abs(dw + dh),
        "edge_smoothness": (previous["edge_smoothness"] + current["edge_smoothness"]) / 2,
    }


@dataclass(frozen=True)
class Rule:
    pairs: int
    hot_change: float = 0.0
    side_change: float = 0.0
    centered_change: float = 0.0
    brightness_change: float = 0.0
    warm_change: float = 0.0
    aligned_hot_change: float = 0.0
    area_change: float = 0.0
    edge_smoothness: float = 0.0

    def __post_init__(self):
        if self.pairs < 1:
            raise ValueError("A confirmation window needs at least one pair")

    def evaluate(self, window):
        if len(window) < self.pairs:
            return "waiting"
        stats = window[self.pairs]
        for key in FEATURES:
            if stats[key] < getattr(self, key):
                return "low_" + key
        return "pass"


def measure_windows(transitions, max_pairs):
    """Use medians of consecutive changes so one jump cannot confirm a track."""
    return {
        pairs: {
            key: float(np.median([entry[key] for entry in transitions[-pairs:]]))
            for key in transitions[-1]
        }
        for pairs in range(1, min(max_pairs, len(transitions)) + 1)
    }
