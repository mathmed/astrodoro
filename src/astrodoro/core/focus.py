"""Focusing aid.

On a Dobsonian, focus is manual: your hand is on the focuser and your eye
rarely on the screen. So this module delivers three things — a large, stable
number, the recent trend (you need to know whether it is improving, not just
the absolute value), and audio feedback.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from ..i18n import gettext as _
from .stars import StarField


@dataclass
class FocusSample:
    t: float
    hfr: float
    fwhm: float
    n_stars: int
    peak: float
    temperature: float | None = None


class FocusMeter:
    """Focus history with a trend and a session best.

    The session best is the reference: in manual focusing you want to know "am I
    better or worse than the best I have managed", not an absolute number that
    depends on the seeing and the target.
    """

    def __init__(self, window: int = 400, trend_seconds: float = 20.0):
        self.samples: deque[FocusSample] = deque(maxlen=window)
        self.trend_seconds = trend_seconds
        self.best: FocusSample | None = None

    def add(self, stars: StarField,
            temperature: float | None = None) -> FocusSample:
        hfr = stars.median_hfr
        s = FocusSample(
            t=time.time(), hfr=hfr, fwhm=stars.median_fwhm,
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
        """HFR slope over the last `trend_seconds`, in px/min.

        Negative means improving. That is the signal that matters while you turn
        the focuser, because the instantaneous value swings with the seeing.
        """
        t, h = self.series()
        m = np.isfinite(h) & (t > -self.trend_seconds)
        if m.sum() < 4:
            return float("nan")
        t, h = t[m], h[m]
        if np.ptp(t) < 1.0:      # ndarray.ptp() was removed in numpy 2.0
            return float("nan")
        return float(np.polyfit(t, h, 1)[0] * 60.0)

    @property
    def current(self) -> FocusSample | None:
        return self.samples[-1] if self.samples else None

    def ratio_to_best(self) -> float:
        """Current HFR over the session best. 1.0 means at the best focus seen."""
        c = self.current
        if (c is None or self.best is None or not np.isfinite(c.hfr)
                or self.best.hfr <= 0):
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
            percent=(r - 1) * 100, arrow=arrow)

    def reset_best(self) -> None:
        self.best = None


def loupe(lum: np.ndarray, xy: tuple[float, float], half: int = 32,
          zoom: int = 4) -> np.ndarray:
    """A magnified crop around a point, for visual inspection of the star.

    Magnified by pixel repetition (nearest) on purpose: interpolating would
    smooth exactly what you are trying to judge.
    """
    h, w = lum.shape[:2]
    x, y = int(round(xy[0])), int(round(xy[1]))
    x0, y0 = max(x - half, 0), max(y - half, 0)
    x1, y1 = min(x + half, w), min(y + half, h)
    crop = lum[y0:y1, x0:x1]
    if crop.size == 0:
        return np.zeros((half * 2 * zoom, half * 2 * zoom), dtype=lum.dtype)
    return np.repeat(np.repeat(crop, zoom, axis=0), zoom, axis=1)


def brightest_usable(stars: StarField, saturation: float | None = None):
    """Brightest unsaturated star — the natural candidate for the loupe."""
    if len(stars) == 0:
        return None
    ok = np.ones(len(stars), dtype=bool)
    if saturation is not None and len(stars.peak):
        ok &= stars.peak < saturation * 0.9
    idx = np.argsort(stars.flux)[::-1]
    for i in idx:
        if ok[i]:
            return tuple(stars.xy[i])
    return tuple(stars.xy[idx[0]])
