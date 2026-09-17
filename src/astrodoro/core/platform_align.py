from __future__ import annotations

import time

import numpy as np

from ..i18n import gettext as _
from . import register
from .equatorial import Fit, fit_rate

SIDEREAL_DEG_PER_MIN = 360.0 / (23.9344696 * 60.0)

WINDOW_S = 360.0
MIN_SPAN_S = 75.0
MIN_SAMPLES = 8
MIN_STARS = 6

JUMP_DEG = 0.08
JUMP_PX = 40.0
REREFERENCE_MISSES = 5

GOOD_DEG = 0.25


def error_deg(rate_deg_min: float) -> float:
    if not np.isfinite(rate_deg_min):
        return float("nan")
    return float(np.degrees(abs(rate_deg_min) / SIDEREAL_DEG_PER_MIN))


class LiveAlign:
    def __init__(
        self,
        window_s: float = WINDOW_S,
        min_span_s: float = MIN_SPAN_S,
        min_samples: int = MIN_SAMPLES,
        min_stars: int = MIN_STARS,
    ):
        self.window_s = float(window_s)
        self.min_span_s = float(min_span_s)
        self.min_samples = int(min_samples)
        self.min_stars = int(min_stars)
        self.reference: np.ndarray | None = None
        self.reason = ""
        self.n_rejected = 0
        self.n_adjustments = 0
        self.before_deg = float("nan")
        self._t: list[float] = []
        self._rot: list[float] = []
        self._offset = 0.0
        self._last_rot = float("nan")
        self._last_shift: tuple[float, float] | None = None
        self._misses = 0
        self._moved_at = 0.0

    def add(self, stars_xy: np.ndarray, t: float | None = None) -> bool:
        t = time.time() if t is None else t
        xy = np.asarray(stars_xy, dtype=float)
        if len(xy) < self.min_stars:
            self.reason = _("only {n} stars — the field is too poor to measure").format(
                n=len(xy)
            )
            self.n_rejected += 1
            return False
        if self.reference is None:
            self._anchor(xy)
            return self.add_rotation(self._offset, t)
        al = register.estimate(xy, self.reference)
        if not al.ok or al.matrix is None:
            self.reason = al.reason
            self.n_rejected += 1
            self._misses += 1
            if self._misses >= REREFERENCE_MISSES:
                self._anchor(xy)
            return False
        self._misses = 0
        if self._shifted(al.shift):
            self.bump(t)
            self._anchor(xy)
            return self.add_rotation(self._offset, t)
        self._last_shift = al.shift
        return self.add_rotation(self._offset + al.rotation_deg, t)

    def add_rotation(self, rotation_deg: float, t: float | None = None) -> bool:
        if not np.isfinite(rotation_deg):
            self.n_rejected += 1
            return False
        t = time.time() if t is None else t
        jumped = abs(rotation_deg - self._last_rot) > JUMP_DEG
        if np.isfinite(self._last_rot) and jumped:
            self.bump(t)
        self.reason = ""
        self._last_rot = float(rotation_deg)
        self._t.append(t)
        self._rot.append(float(rotation_deg))
        self._trim()
        return True

    def bump(self, t: float | None = None) -> None:
        settled = self.settled
        f = self.fit()
        if settled and np.isfinite(f.rate):
            self.before_deg = error_deg(f.rate)
            self.n_adjustments += 1
        self._t.clear()
        self._rot.clear()
        self._last_rot = float("nan")
        self._last_shift = None
        self.reference = None
        self._offset = 0.0
        self._moved_at = time.time() if t is None else t

    def clear(self) -> None:
        self.bump()
        self.before_deg = float("nan")
        self.n_adjustments = 0
        self.n_rejected = 0
        self.reason = ""

    def _anchor(self, xy: np.ndarray) -> None:
        self.reference = xy
        self._offset = self._last_rot if np.isfinite(self._last_rot) else 0.0
        self._last_shift = None
        self._misses = 0

    def _shifted(self, shift: tuple[float, float]) -> bool:
        if self._last_shift is None:
            return False
        dx = shift[0] - self._last_shift[0]
        dy = shift[1] - self._last_shift[1]
        return bool(np.hypot(dx, dy) > JUMP_PX)

    def _trim(self) -> None:
        cut = self._t[-1] - self.window_s
        k = 0
        while k < len(self._t) - 1 and self._t[k] < cut:
            k += 1
        if k:
            del self._t[:k]
            del self._rot[:k]

    @property
    def n(self) -> int:
        return len(self._t)

    @property
    def span_s(self) -> float:
        return self._t[-1] - self._t[0] if len(self._t) > 1 else 0.0

    @property
    def settled(self) -> bool:
        return self.n >= self.min_samples and self.span_s >= self.min_span_s

    @property
    def progress(self) -> float:
        if self.min_span_s <= 0 or self.min_samples <= 0:
            return 1.0
        return float(
            np.clip(
                min(self.span_s / self.min_span_s, self.n / self.min_samples), 0.0, 1.0
            )
        )

    def fit(self) -> Fit:
        return fit_rate(self._t, self._rot)

    def status(self) -> dict:
        f = self.fit()
        settled = self.settled
        return {
            "n": self.n,
            "span_s": self.span_s,
            "progress": self.progress,
            "rate_deg_min": f.rate if settled else float("nan"),
            "sigma_deg_min": f.sigma if settled else float("nan"),
            "error_deg": error_deg(f.rate) if settled else float("nan"),
            "error_sigma_deg": error_deg(f.sigma) if settled else float("nan"),
            "settled": settled,
            "before_deg": self.before_deg,
            "adjustments": self.n_adjustments,
            "rejected": self.n_rejected,
            "moved_at": self._moved_at,
            "reason": self.reason,
        }


def reading(status: dict | None) -> str:
    if not status or not status.get("settled"):
        return "—"
    return _("{deg:.2f}° ± {sigma:.2f}°").format(
        deg=status["error_deg"], sigma=status["error_sigma_deg"]
    )


def advice(status: dict | None) -> str:
    if not status:
        return _("not measuring")
    if status.get("reason"):
        return str(status["reason"])
    if not status.get("settled"):
        left = max(0.0, MIN_SPAN_S - status.get("span_s", 0.0))
        return _("measuring… {n} frames, ~{s:.0f}s to the first reading").format(
            n=status.get("n", 0), s=left
        )
    err, sigma = status["error_deg"], status["error_sigma_deg"]
    if err <= max(GOOD_DEG, 2.0 * sigma):
        return _("aligned as far as this field can tell — point elsewhere to confirm")
    return _("turn one screw at a time and watch the number fall")


def verdict(status: dict | None) -> str:
    if not status or not status.get("settled"):
        return ""
    before = status.get("before_deg", float("nan"))
    if not np.isfinite(before):
        return ""
    after = status["error_deg"]
    if after < before * 0.8:
        return _("better: {before:.2f}° → {after:.2f}°").format(
            before=before, after=after
        )
    if after > before * 1.2:
        return _("worse: {before:.2f}° → {after:.2f}° — turn it the other way").format(
            before=before, after=after
        )
    return _("unchanged: {before:.2f}° → {after:.2f}°").format(
        before=before, after=after
    )
