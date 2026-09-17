from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from ..i18n import gettext as _


@dataclass
class Fit:
    rate: float
    r2: float
    n: int
    span_s: float
    sigma: float = float("nan")

    @property
    def solid(self) -> bool:
        return self.n >= 8 and self.span_s >= 90 and self.r2 >= 0.5


def fit_rate(times: list[float], values: list[float]) -> Fit:
    if len(times) < 4:
        return Fit(float("nan"), 0.0, len(times), 0.0)
    t = np.asarray(times, dtype=float)
    v = np.asarray(values, dtype=float)
    m = np.isfinite(v)
    if m.sum() < 4:
        return Fit(float("nan"), 0.0, int(m.sum()), 0.0)
    t, v = t[m], v[m]
    t = (t - t[0]) / 60.0
    span = float(t[-1] * 60.0)
    if t[-1] <= 1e-6:
        return Fit(float("nan"), 0.0, len(t), span)
    a, b = np.polyfit(t, v, 1)
    pred = a * t + b
    ss_res = float(((v - pred) ** 2).sum())
    ss_tot = float(((v - v.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return Fit(
        float(a), float(np.clip(r2, 0.0, 1.0)), len(t), span, _slope_sigma(t, ss_res)
    )


def _slope_sigma(t: np.ndarray, ss_res: float) -> float:
    if len(t) < 3:
        return float("nan")
    spread = float(((t - t.mean()) ** 2).sum())
    if spread <= 0:
        return float("nan")
    return float(np.sqrt(ss_res / (len(t) - 2) / spread))


class PlatformMonitor:
    def __init__(self, window_minutes: float = 12.0):
        self.window_s = window_minutes * 60.0
        self.started = False
        self._t: list[float] = []
        self._rot: list[float] = []
        self._dx: list[float] = []
        self._dy: list[float] = []
        self._fwhm: list[float] = []

    def add(
        self,
        rotation_deg: float,
        dx: float,
        dy: float,
        fwhm: float,
        t: float | None = None,
    ) -> None:
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

    def _fit(self, values: list[float]) -> Fit:
        return fit_rate(self._t, values)

    def rotation_fit(self) -> Fit:
        return self._fit(self._rot)

    def drift_fit(self) -> tuple[Fit, Fit]:
        return self._fit(self._dx), self._fit(self._dy)

    def corner_radius(self, shape: tuple[int, int]) -> float:
        h, w = shape
        return 0.5 * float(np.hypot(w, h))

    def corner_smear_rate(self, shape: tuple[int, int]) -> float:
        f = self.rotation_fit()
        if not np.isfinite(f.rate):
            return float("nan")
        return abs(np.deg2rad(f.rate)) * self.corner_radius(shape)

    def useful_seconds(
        self, shape: tuple[int, int], fwhm_px: float | None = None
    ) -> float:
        rate = self.corner_smear_rate(shape)
        if not np.isfinite(rate) or rate <= 1e-9:
            return float("inf")
        if fwhm_px is None:
            vals = [v for v in self._fwhm if np.isfinite(v)]
            fwhm_px = float(np.median(vals)) if vals else 3.0
        return float(fwhm_px / rate * 60.0)

    def report(self, shape: tuple[int, int]) -> dict:
        rot = self.rotation_fit()
        fdx, fdy = self.drift_fit()
        drift_rate = (
            float(np.hypot(fdx.rate, fdy.rate))
            if np.isfinite(fdx.rate)
            else float("nan")
        )
        drift_angle = (
            float(np.degrees(np.arctan2(fdy.rate, fdx.rate)))
            if np.isfinite(fdx.rate)
            else float("nan")
        )
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
        parts = [
            _("residual rotation {rate:.3f} deg/min").format(
                rate=abs(r["rotation_deg_min"])
            )
        ]
        useful = r["useful_s"]
        if np.isfinite(useful):
            parts.append(
                _(
                    "~{min:.0f} min of useful integration before the corner smears"
                ).format(min=useful / 60)
            )
        return " · ".join(parts)
