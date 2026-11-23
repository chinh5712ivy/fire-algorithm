"""Run the VIAVF fire filter on a video, with optional preview and MP4 export."""

import argparse
from collections import Counter, deque
from contextlib import closing
from math import isfinite
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

import cv2

from fire_filter import MAX_GAP_S, Rule, compare, crop_features, measure_windows


HERE = Path(__file__).resolve().parent
SAMPLE_FPS = 5
EPSILON = 1e-9
WINDOW = "VIAVF fire filter - Q / Esc: exit"
STATUS = {
    "pass": ("FIRE CANDIDATE", (85, 240, 100)),
    "waiting": ("WAITING", (0, 220, 255)),
    "low_aligned_hot_change": ("SHAPE TOO STABLE", (45, 145, 255)),
    "low_area_change": ("AREA TOO STABLE", (45, 145, 255)),
    "low_edge_smoothness": ("SHARP EDGES", (45, 145, 255)),
}


class FireVideoFilter:
    """Keep one history per track. Supply one update per sampled video frame."""

    def __init__(self, rule=None):
        self.rule = rule if rule is not None else Rule(**json.loads(
            (HERE / "cau_hinh.json").read_text(encoding="utf-8-sig")
        ))
        self.history = {}
        self.last_time = -1.0

    def update(self, frame, timestamp, tracked_rows):
        if not isfinite(timestamp) or timestamp < 0 or timestamp <= self.last_time:
            raise ValueError("Frame timestamps must be finite, nonnegative and strictly increasing")
        self.last_time = timestamp
        self.history = {
            track: state for track, state in self.history.items()
            if timestamp - state[0] <= MAX_GAP_S
        }
        results = []
        for row in tracked_rows:
            track = row["track_id"]
            if track is None:
                continue
            features = crop_features(frame, row["bbox"])
            if features is None:
                continue
            previous = self.history.get(track)
            if previous is None:
                changes = deque(maxlen=self.rule.pairs)
            else:
                changes = previous[2]
                changes.append(compare(previous[1], features))
            self.history[track] = timestamp, features, changes
            if row["viavf_rule"] == "pass":
                window = measure_windows(list(changes), self.rule.pairs)
                results.append({
                    "track_id": track,
                    "bbox": row["bbox"],
                    "status": self.rule.evaluate(window),
                    "scores": window.get(self.rule.pairs),
                })
        return results


def trace_frames(capture, source_fps, path):
    index = -1
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            timestamp = record["timestamp"]
            target = round(timestamp * source_fps)
            if target <= index:
                raise ValueError("Trace timestamps map to repeated or out-of-order frames")
            while index < target:
                ok, frame = capture.read()
                if not ok:
                    raise ValueError(f"Trace extends past the end of the video at {timestamp:.3f}s")
                index += 1
            if list(frame.shape[1::-1]) != list(record["frame_size"]):
                raise ValueError("Video and trace have different frame sizes")
            pts = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if pts > 0 and abs(pts - timestamp) > 0.51 / source_fps + 0.002:
                raise ValueError(f"Video and trace timelines differ at {timestamp:.3f}s")
            yield timestamp, frame, record["fire"]


def detected_frames(capture, source_fps, sample_fps, checkpoint):
    # Keep detector dependencies optional when replaying a trace.
    work = str(HERE)
    if work not in sys.path:
        sys.path.insert(0, work)
    from viavf_fire_sim import Replay, create_detector

    detector = create_detector(checkpoint)
    replay = Replay(3)
    index, previous, next_sample = 0, -1.0, 0.0
    while True:
        ok, frame = capture.read()
        if not ok:
            return
        timestamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000
        if not isfinite(timestamp) or timestamp <= previous:
            timestamp = max(index / source_fps, previous + 1 / source_fps)
        index += 1
        previous = timestamp
        if timestamp + EPSILON < next_sample:
            continue
        while next_sample <= timestamp + EPSILON:
            next_sample += 1 / sample_fps
        rows = replay.step(timestamp, frame.shape[1::-1], detector.detect(frame, timestamp))
        yield timestamp, frame, rows


def draw(frame, timestamp, detections):
    height, width = frame.shape[:2]
    # H.264 with yuv420p requires even dimensions.
    picture = cv2.copyMakeBorder(frame, 44, height % 2, 0, width % 2,
                                 cv2.BORDER_CONSTANT, value=(24, 28, 34))
    for row in detections:
        status = row["status"]
        label, color = STATUS.get(status, (status.upper(), (45, 145, 255)))
        x1, y1, x2, y2 = map(round, row["bbox"])
        cv2.rectangle(picture, (x1, y1 + 44), (x2, y2 + 44), color, 2)
        cv2.putText(picture, f'#{row["track_id"]} {label}', (max(0, x1), max(59, y1 + 39)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, color, 1, cv2.LINE_AA)
    title = f"{timestamp:05.1f}s  |  VIAVF fire filter  |  {len(detections)} candidate(s)"
    cv2.putText(picture, title, (10, 28), cv2.FONT_HERSHEY_SIMPLEX,
                0.64, (240, 244, 247), 1, cv2.LINE_AA)
    return picture


def encoder_for(ffmpeg, path, frame, fps):
    height, width = frame.shape[:2]
    return subprocess.Popen([
        ffmpeg, "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(path),
    ], stdin=subprocess.PIPE)


def run_video(video, *, trace=None, checkpoint=None, output=None, show=False):
    if (trace is None) == (checkpoint is None):
        raise ValueError("Supply exactly one of trace or checkpoint")
    if output is None and not show:
        raise ValueError("Select output, show, or both")
    if output is not None and (output.suffix.lower() != ".mp4" or output.exists()):
        raise ValueError("Output must be a new .mp4 path")
    ffmpeg = shutil.which("ffmpeg") if output is not None else None
    if output is not None and ffmpeg is None:
        raise ValueError("FFmpeg in PATH is required for MP4 export")

    capture = cv2.VideoCapture(str(video))
    engine = FireVideoFilter()
    counts = Counter()
    first_pass = None
    frames = 0
    encoder = None
    preview_open = False
    stopped = False
    scratch = None
    try:
        source_fps = capture.get(cv2.CAP_PROP_FPS)
        if not capture.isOpened() or not isfinite(source_fps) or source_fps <= 0:
            raise ValueError(f"Cannot open video or read its frame rate: {video}")
        sample_fps = min(SAMPLE_FPS, source_fps)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            scratch = TemporaryDirectory(dir=output.parent)
            partial = Path(scratch.name) / "video.mp4"
        samples = (trace_frames(capture, source_fps, trace) if trace is not None else
                   detected_frames(capture, source_fps, sample_fps, checkpoint))
        if show:
            cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
            preview_open = True
            cv2.resizeWindow(WINDOW, 1024, 640)
        with closing(samples):
            for timestamp, frame, rows in samples:
                began = monotonic()
                results = engine.update(frame, timestamp, rows)
                counts.update(row["status"] for row in results)
                if first_pass is None and any(row["status"] == "pass" for row in results):
                    first_pass = timestamp
                picture = draw(frame, timestamp, results)
                if output is not None:
                    if encoder is None:
                        encoder = encoder_for(ffmpeg, partial, picture, sample_fps)
                    encoder.stdin.write(picture.tobytes())
                frames += 1
                if show:
                    cv2.imshow(WINDOW, picture)
                    delay = max(1, round((1 / sample_fps - (monotonic() - began)) * 1000))
                    key = cv2.waitKey(delay) & 0xFF
                    if key in (ord("q"), ord("Q"), 27) or cv2.getWindowProperty(
                        WINDOW, cv2.WND_PROP_VISIBLE
                    ) < 1:
                        stopped = True
                        break
        if frames == 0:
            raise ValueError("No matching video frames to process")
        if encoder is not None:
            encoder.stdin.close()
            if encoder.wait() != 0:
                raise RuntimeError("FFmpeg could not encode the result")
            if output.exists():
                raise FileExistsError(output)
            partial.rename(output)
        return {"frames": frames, "status_counts": dict(counts),
                "first_candidate_s": first_pass, "stopped_by_user": stopped}
    finally:
        capture.release()
        if encoder is not None and encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        if encoder is not None and not encoder.stdin.closed:
            encoder.stdin.close()
        if scratch is not None:
            scratch.cleanup()
        if preview_open:
            try:
                cv2.destroyWindow(WINDOW)
            except cv2.error:
                pass  # The user may have closed the window using the title bar.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--trace", type=Path, help="Tracked VIAVF JSONL, sampled at 5 FPS")
    input_group.add_argument("--checkpoint", type=Path, help="YOLOX-S fire/smoke checkpoint")
    parser.add_argument("--output", type=Path, help="Write an annotated H.264 MP4")
    parser.add_argument("--show", action="store_true", help="Open a live preview during video processing")
    args = parser.parse_args()
    try:
        summary = run_video(args.video, trace=args.trace, checkpoint=args.checkpoint,
                            output=args.output, show=args.show)
    except (ValueError, OSError, RuntimeError, ImportError, KeyError, TypeError, cv2.error) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
