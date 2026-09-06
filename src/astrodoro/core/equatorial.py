"""Equatorial platform monitoring.

The problem that matters is the residual field rotation left over from a polar
alignment done by eye. The useful question is not "how much platform travel is
left" — you know that by looking at it — but "how much integration is left
before the corner stars turn into streaks". That is computable from the rotation
already measured frame by frame during registration, and it is all this module
does.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ..i18n import gettext as _


@dataclass
class Fit:
    rate: float            # units per minute
    r2: float              # goodness of fit (0..1)
    n: int
    span_s: float

    @property
    def solid(self) -> bool:
        """Trustworthy fit: enough samples, enough time, decent r2."""
        return self.n >= 8 and self.span_s >= 90 and self.r2 >= 0.5


def fit_rate(times: list[float], values: list[float]) -> Fit:
    """Least-squares rate of `values` against `times`, in units per minute.

    Shared with `platform_align`, which fits the same shape of data — a
    rotation angle against a timestamp — over a whole measurement instead of a
    sliding window.
    """
    if len(times) < 4:
        return Fit(float("nan"), 0.0, len(times), 0.0)
    t = np.asarray(times, dtype=float)
    v = np.asarray(values, dtype=float)
    m = np.isfinite(v)
    if m.sum() < 4:
        return Fit(float("nan"), 0.0, int(m.sum()), 0.0)
    t, v = t[m], v[m]
    t = (t - t[0]) / 60.0                      # minutes
    span = float(t[-1] * 60.0)
    if t[-1] <= 1e-6:
        return Fit(float("nan"), 0.0, len(t), span)
    a, b = np.polyfit(t, v, 1)
    pred = a * t + b
    ss_res = float(((v - pred) ** 2).sum())
    ss_tot = float(((v - v.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return Fit(float(a), float(np.clip(r2, 0.0, 1.0)), len(t), span)


class PlatformMonitor:
    def __init__(self, window_minutes: float = 12.0):
        self.window_s = window_minutes * 60.0
        self.started = False
        self._t: list[float] = []
        self._rot: list[float] = []
        self._dx: list[float] = []
        self._dy: list[float] = []
        self._fwhm: list[float] = []

    # ------------------------------------------------------------------- data
    def add(self, rotation_deg: float, dx: float, dy: float,
            fwhm: float, t: float | None = None) -> None:
        t = time.time() if t is None else t
        self._t.append(t)
        self._rot.append(rotation_deg)
        self._dx.append(dx)
        self._dy.append(dy)
        self._fwhm.append(fwhm)
        self._trim()

    def _trim(self) -> None:
        if not self._t:
            return
        cut = self._t[-1] - self.window_s
        k = 0
        while k < len(self._t) - 1 and self._t[k] < cut:
            k += 1
        if k:
            for lst in (self._t, self._rot, self._dx, self._dy, self._fwhm):
                del lst[:k]

    def clear(self) -> None:
        self.started = False
        for lst in (self._t, self._rot, self._dx, self._dy, self._fwhm):
            lst.clear()

    # ------------------------------------------------------------------- fits
    def _fit(self, values: list[float]) -> Fit:
        return fit_rate(self._t, values)

    def rotation_fit(self) -> Fit:
        """Residual rotation in degrees per minute.

        This is the number that measures the polar alignment. A perfectly
        aligned platform gives zero; the further from the pole, the faster the
        field turns.
        """
        return self._fit(self._rot)

    def drift_fit(self) -> tuple[Fit, Fit]:
        return self._fit(self._dx), self._fit(self._dy)

    # --------------------------------------------------------- rotation budget
    def corner_radius(self, shape: tuple[int, int]) -> float:
        h, w = shape
        return 0.5 * float(np.hypot(w, h))

    def corner_smear_rate(self, shape: tuple[int, int]) -> float:
        """Rotational smear at the frame corner, in px per minute.

        Field rotation does not blur uniformly: nothing blurs at the centre of
        rotation, and the displacement grows linearly with radius. The corner is
        the worst case, and where stars turn into streaks first.
        """
        f = self.rotation_fit()
        if not np.isfinite(f.rate):
            return float("nan")
        return abs(np.deg2rad(f.rate)) * self.corner_radius(shape)

    def useful_seconds(self, shape: tuple[int, int],
                       fwhm_px: float | None = None) -> float:
        """Seconds of integration until the corner smear equals the FWHM.

        The practical limit on total exposure: past this, the edge stars are
        visibly elongated in the stack even if the platform still has travel.
        """
        rate = self.corner_smear_rate(shape)
        if not np.isfinite(rate) or rate <= 1e-9:
            return float("inf")
        if fwhm_px is None:
            vals = [v for v in self._fwhm if np.isfinite(v)]
            fwhm_px = float(np.median(vals)) if vals else 3.0
        return float(fwhm_px / rate * 60.0)

    # ---------------------------------------------------------------- summary
    def report(self, shape: tuple[int, int]) -> dict:
        rot = self.rotation_fit()
        fdx, fdy = self.drift_fit()
        drift_rate = (float(np.hypot(fdx.rate, fdy.rate))
                      if np.isfinite(fdx.rate) else float("nan"))
        drift_angle = (float(np.degrees(np.arctan2(fdy.rate, fdx.rate)))
                       if np.isfinite(fdx.rate) else float("nan"))
        return {
            "rotation_deg_min": rot.rate,
            "rotation_r2": rot.r2,
            "rotation_solid": rot.solid,
            "drift_px_min": drift_rate,
            "drift_angle_deg": drift_angle,
            "corner_smear_px_min": self.corner_smear_rate(shape),
            "useful_s": self.useful_seconds(shape),
            "n_samples": rot.n,
            "span_s": rot.span_s,
        }

    def advice(self, shape: tuple[int, int]) -> str:
        r = self.report(shape)
        if not r["rotation_solid"]:
            need = max(0, 90 - int(r["span_s"]))
            text = _("measuring rotation… {n} samples").format(n=r["n_samples"])
            if need:
                text += _(", ~{s}s to go").format(s=need)
            return text
        parts = [_("residual rotation {rate:.3f} deg/min").format(
            rate=abs(r["rotation_deg_min"]))]
        useful = r["useful_s"]
        if np.isfinite(useful):
            parts.append(_("~{min:.0f} min of useful integration before the "
                           "corner smears").format(min=useful / 60))
        return " · ".join(parts)
