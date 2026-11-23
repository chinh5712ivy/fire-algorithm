from dataclasses import dataclass, field
from collections import deque
import math

def area(b):
    return max(0, b[2]-b[0]) * max(0, b[3]-b[1])

def center(b):
    return ((b[0]+b[2])/2, (b[1]+b[3])/2)

def iou(a, b):
    intersection = area((max(a[0],b[0]), max(a[1],b[1]), min(a[2],b[2]), min(a[3],b[3])))
    return intersection / max(area(a)+area(b)-intersection, 1e-9)

@dataclass
class Track:
    id: int
    class_name: str
    history: deque = field(default_factory=deque)
    state: str = 'DETECTING'
    last_request: float = -float('inf')
    reason: str = ''
    results: list = field(default_factory=list)
    @property
    def latest(self):
        return self.history[-1]

class Tracker:
    """Greedy one-to-one association, separated by class, using video time."""
    def __init__(self, cfg):
        self.cfg, self.tracks, self.next_id = cfg, {}, 1

    def update(self, detections, timestamp):
        expired = [k for k,t in self.tracks.items() if timestamp-t.latest['timestamp'] > self.cfg['track_max_gap_s']]
        for k in expired:
            del self.tracks[k]
        pairs = []
        for n,d in enumerate(detections):
            for k,t in self.tracks.items():
                if d['class_name'] != t.class_name:
                    continue
                overlap = iou(d['bbox'], t.latest['bbox'])
                distance = math.dist(center(d['bbox']), center(t.latest['bbox']))
                if overlap >= self.cfg['track_iou'] or distance <= self.cfg['track_center_px']:
                    pairs.append((overlap - distance/10000, n, k))
        used_d, used_t, assignments = set(), set(), {}
        for _,n,k in sorted(pairs, reverse=True):
            if n not in used_d and k not in used_t:
                used_d.add(n); used_t.add(k); assignments[n] = k
        observed = []
        for n,d in enumerate(detections):
            if n not in assignments:
                k = self.next_id; self.next_id += 1
                self.tracks[k] = Track(k, d['class_name'])
                assignments[n] = k
            t = self.tracks[assignments[n]]
            t.history.append(d)
            while len(t.history)>1 and timestamp-t.history[0]['timestamp'] > self.cfg['history_s']:
                t.history.popleft()
            observed.append(t)
        return observed
