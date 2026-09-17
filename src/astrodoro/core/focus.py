from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from ..i18n import gettext as _


class Measured(Protocol):
    """What the focus meter reads. `StarField`, or a stand-in over an outcome
    that already measured the same numbers."""

    @property
    def median_hfr(self) -> float: ...

    @property
    def median_fwhm(self) -> float: ...

    @property
    def peak(self) -> np.ndarray: ...

    def __len__(self) -> int: ...


@dataclass
class FocusSample:
    t: float
    hfr: float
    fwhm: float
    n_stars: int
    peak: float
    temperature: float | None = None


class FocusMeter:
    def __init__(self, window: int = 400, trend_seconds: float = 20.0):
        self.samples: deque[FocusSample] = deque(maxlen=window)
        self.trend_seconds = trend_seconds
        self.best: FocusSample | None = None

    def add(self, stars: Measured, temperature: float | None = None) -> FocusSample:
        hfr = stars.median_hfr
        s = FocusSample(
            t=time.time(),
            hfr=hfr,
            fwhm=stars.median_fwhm,
            n_stars=len(stars),
            peak=float(np.median(stars.peak)) if len(stars.peak) else float("nan"),
            temperature=temperature,
        )
        self.samples.append(s)
        if np.isfinite(hfr) and len(stars) >= 3:
            if self.best is None or hfr < self.best.hfr:
                self.best = s
        return s

    def series(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.samples:
            return np.empty(0), np.empty(0)
        t0 = self.samples[-1].t
        t = np.array([s.t - t0 for s in self.samples])
        h = np.array([s.hfr for s in self.samples])
        return t, h

    def trend(self) -> float:
        t, h = self.series()
        m = np.isfinite(h) & (t > -self.trend_seconds)
        if m.sum() < 4:
            return float("nan")
        t, h = t[m], h[m]
        if np.ptp(t) < 1.0:
            return float("nan")
        return float(np.polyfit(t, h, 1)[0] * 60.0)

    @property
    def current(self) -> FocusSample | None:
        return self.samples[-1] if self.samples else None

    def ratio_to_best(self) -> float:
        c = self.current
        if (
            c is None
            or self.best is None
            or not np.isfinite(c.hfr)
            or self.best.hfr <= 0
        ):
            return float("nan")
        return c.hfr / self.best.hfr

    def verdict(self) -> str:
        r = self.ratio_to_best()
        tr = self.trend()
        if not np.isfinite(r):
            return _("not enough stars")
        if r < 1.03:
            return _("at the session's best focus")
        arrow = ""
        if np.isfinite(tr):
            if tr < -0.05:
                arrow = _(" (improving)")
            elif tr > 0.05:
                arrow = _(" (getting worse)")
        return _("{percent:.0f}% above the best{arrow}").format(
            percent=(r - 1) * 100, arrow=arrow
        )

    def reset_best(self) -> None:
        self.best = None


def loupe(
    lum: np.ndarray, xy: tuple[float, float], half: int = 32, zoom: int = 4
) -> np.ndarray:
    h, w = lum.shape[:2]
    x, y = int(round(xy[0])), int(round(xy[1]))
    x0, y0 = max(x - half, 0), max(y - half, 0)
    x1, y1 = min(x + half, w), min(y + half, h)
    crop = lum[y0:y1, x0:x1]
    if crop.size == 0:
        return np.zeros((half * 2 * zoom, half * 2 * zoom), dtype=lum.dtype)
    return np.repeat(np.repeat(crop, zoom, axis=0), zoom, axis=1)


SHARPNESS_SCALE = 1000.0


def sharpness(lum: np.ndarray) -> float:
    a = np.asarray(lum, dtype=np.float32)
    if a.ndim != 2 or a.shape[0] < 2 or a.shape[1] < 2:
        return 0.0
    m = float(a.mean())
    if m <= 0.0:
        return 0.0
    gy = np.diff(a, axis=0)
    gx = np.diff(a, axis=1)
    energy = float(np.mean(gy * gy) + np.mean(gx * gx))
    return SHARPNESS_SCALE * energy / (m * m)


@dataclass
class SharpnessSample:
    t: float
    value: float
    temperature: float | None = None


class SharpnessMeter:
    def __init__(self, window: int = 400, trend_seconds: float = 20.0):
        self.samples: deque[SharpnessSample] = deque(maxlen=window)
        self.trend_seconds = trend_seconds
        self.best: SharpnessSample | None = None

    def add(self, value: float, temperature: float | None = None) -> SharpnessSample:
        s = SharpnessSample(t=time.time(), value=float(value), temperature=temperature)
        self.samples.append(s)
        if np.isfinite(s.value) and (self.best is None or s.value > self.best.value):
            self.best = s
        return s

    def series(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.samples:
            return np.empty(0), np.empty(0)
        t0 = self.samples[-1].t
        t = np.array([s.t - t0 for s in self.samples])
        v = np.array([s.value for s in self.samples])
        return t, v

    def trend(self) -> float:
        t, v = self.series()
        m = np.isfinite(v) & (t > -self.trend_seconds)
        if m.sum() < 4:
            return float("nan")
        t, v = t[m], v[m]
        if np.ptp(t) < 1.0:
            return float("nan")
        return float(np.polyfit(t, v, 1)[0] * 60.0)

    @property
    def current(self) -> SharpnessSample | None:
        return self.samples[-1] if self.samples else None

    def ratio_to_best(self) -> float:
        c = self.current
        if c is None or self.best is None or not np.isfinite(c.value) or c.value <= 0:
            return float("nan")
        return self.best.value / c.value

    def verdict(self) -> str:
        r = self.ratio_to_best()
        tr = self.trend()
        if not np.isfinite(r):
            return _("no contrast in the frame")
        if r < 1.03:
            return _("at the session's sharpest")
        arrow = ""
        if np.isfinite(tr):
            if tr > 0.05:
                arrow = _(" (improving)")
            elif tr < -0.05:
                arrow = _(" (getting worse)")
        return _("{percent:.0f}% below the sharpest{arrow}").format(
            percent=(1 - 1 / r) * 100, arrow=arrow
        )

    def reset_best(self) -> None:
        self.best = None
