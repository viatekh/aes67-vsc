"""Elastic buffer: bridges two audio clocks (sound card vs network) without clicks.

The writer pushes frames at its clock rate; the reader pulls at its own.
A PI controller watches the buffer fill and resamples by a tiny ratio
(within +-0.5 %, typically < 100 ppm) with cubic interpolation, so the fill
stays at the target instead of over/underflowing.
"""
from __future__ import annotations

import numpy as np

from .audio import Ring


def _cubic(x: np.ndarray, pos: np.ndarray) -> np.ndarray:
    """Catmull-Rom interpolation of x (frames, ch) at fractional positions (x[i+1] is position i)."""
    i = np.floor(pos).astype(np.int64)
    f = (pos - i)[:, None].astype(np.float32)
    p0, p1, p2, p3 = x[i], x[i + 1], x[i + 2], x[i + 3]
    return p1 + 0.5 * f * (p2 - p0 + f * (2 * p0 - 5 * p1 + 4 * p2 - p3 + f * (3 * (p1 - p2) + p3 - p0)))


class ElasticBuffer:
    def __init__(self, channels: int, rate: int, target_frames: int, max_ppm: float = 5000):
        self.ring = Ring(channels, max(rate, target_frames * 8))
        self.channels, self.rate, self.target = channels, rate, target_frames
        self.max_dev = max_ppm * 1e-6
        self.ratio = 1.0          # input frames consumed per output frame
        self._integral = 0.0
        self._fill = float(target_frames)
        self._hist = np.zeros((3, channels), np.float32)
        self._frac = 0.0
        self.primed = False
        self.underruns = self.overruns = 0

    def __len__(self):
        return len(self.ring)

    def write(self, frames: np.ndarray):
        self.ring.write(frames)
        if len(self.ring) > 6 * self.target:  # way off (e.g. after a stall): hard resync
            self.ring.drop(len(self.ring) - self.target)
            self.overruns += 1

    def read(self, n: int) -> np.ndarray:
        if not self.primed:
            if len(self.ring) < self.target + n:
                return np.zeros((n, self.channels), np.float32)
            self.primed, self._fill = True, float(len(self.ring))
        # --- control loop ---
        dt = n / self.rate
        self._fill += (len(self.ring) - self._fill) * min(1.0, dt / 0.05)  # 50 ms smoothing
        err = (self._fill - self.target) / self.rate                      # seconds
        self._integral = max(-self.max_dev, min(self.max_dev, self._integral + 0.02 * err * dt))
        self.ratio = 1.0 + max(-self.max_dev, min(self.max_dev, 0.2 * err + self._integral))
        # --- resample ---
        pos = self._frac + self.ratio * np.arange(n)
        need = int(np.floor(pos[-1])) + 4 - len(self._hist)
        if need > len(self.ring):
            self.primed = False
            self.underruns += 1
            return np.zeros((n, self.channels), np.float32)
        x = np.concatenate([self._hist, self.ring.read(max(0, need))]) if need > 0 else self._hist
        out = _cubic(x, pos)
        nxt = pos[-1] + self.ratio
        adv = int(np.floor(nxt))
        self._frac = nxt - adv
        self._hist = x[adv:]
        return out.astype(np.float32, copy=False)
