from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import sep


@dataclass
class StarField:
    xy: np.ndarray
    flux: np.ndarray
    fwhm: np.ndarray
    background: float
    noise: float
    hfr: np.ndarray = field(default_factory=lambda: np.empty(0))
    peak: np.ndarray = field(default_factory=lambda: np.empty(0))
    elong: np.ndarray = field(default_factory=lambda: np.empty(0))
    halo: np.ndarray = field(default_factory=lambda: np.empty(0))

    def __len__(self) -> int:
        return len(self.xy)

    @property
    def median_fwhm(self) -> float:
        return float(np.median(self.fwhm)) if len(self) else float("nan")

    @property
    def median_elongation(self) -> float:
        return float(np.median(self.elong)) if len(self.elong) else float("nan")

    @property
    def median_halo(self) -> float:
        if not len(self.halo):
            return float("nan")
        v = self.halo[np.isfinite(self.halo)]
        return float(np.median(v)) if len(v) else float("nan")

    @property
    def median_hfr(self) -> float:
        return float(np.median(self.hfr)) if len(self.hfr) else float("nan")


def detect(
    lum: np.ndarray,
    scale: float = 2.0,
    thresh_sigma: float = 5.0,
    min_area: int = 4,
    max_stars: int = 60,
    central: float = 0.70,
    saturation: float | None = None,
    min_central: int = 12,
    halo_radius: float = 50.0,
) -> StarField:
    data = np.ascontiguousarray(lum, dtype=np.float32)
    bkg = sep.Background(data)
    sub = data - bkg.back()
    rms = float(bkg.globalrms)

    try:
        objs = sep.extract(sub, thresh_sigma, err=rms, minarea=min_area)
    except Exception:
        objs = sep.extract(sub, thresh_sigma * 2, err=rms, minarea=min_area)

    if len(objs) == 0:
        return StarField(
            np.empty((0, 2)), np.empty(0), np.empty(0), float(bkg.globalback), rms
        )

    x, y = objs["x"], objs["y"]
    keep = np.ones(len(objs), dtype=bool)

    if 0 < central < 1:
        h, w = data.shape
        mx, my = w * (1 - central) / 2, h * (1 - central) / 2
        inner = (x > mx) & (x < w - mx) & (y > my) & (y < h - my)
        if np.count_nonzero(keep & inner) >= min_central:
            keep &= inner

    if saturation is not None:
        keep &= objs["peak"] < saturation * 0.95

    a, b = objs["a"], objs["b"]
    with np.errstate(divide="ignore", invalid="ignore"):
        elong = np.where(b > 0, a / b, np.inf)
    keep &= elong < 3.0

    objs, x, y = objs[keep], x[keep], y[keep]
    if len(objs) == 0:
        return StarField(
            np.empty((0, 2)), np.empty(0), np.empty(0), float(bkg.globalback), rms
        )

    order = np.argsort(objs["flux"])[::-1][:max_stars]
    objs = objs[order]

    fwhm = 2.3548 * np.sqrt(np.abs(objs["a"] * objs["b"])) * scale
    xy = np.column_stack([objs["x"] * scale, objs["y"] * scale]).astype(np.float64)

    try:
        rmax = np.clip(np.asarray(objs["a"], dtype=np.float64) * 8.0, 4.0, 60.0)
        hfr, _flags = sep.flux_radius(
            sub, objs["x"], objs["y"], rmax, 0.5, normflux=objs["flux"], subpix=5
        )
        hfr = np.asarray(hfr, dtype=np.float64) * scale
    except Exception:
        hfr = fwhm / 2.3548 * 1.1774

    try:
        m = min(len(objs), 20)
        r_core = np.maximum(np.asarray(hfr[:m], float) / scale * 2.5, 3.0)
        r_out = np.full(m, halo_radius / scale, dtype=np.float64)
        annulus = (halo_radius / scale * 3.0, halo_radius / scale * 4.0)
        f_core, _e1, _f1 = sep.sum_circle(
            sub, objs["x"][:m], objs["y"][:m], r_core, bkgann=annulus, subpix=5
        )
        f_out, _e2, _f2 = sep.sum_circle(
            sub, objs["x"][:m], objs["y"][:m], r_out, bkgann=annulus, subpix=5
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            halo = np.where(
                np.asarray(f_core) > 0, np.asarray(f_out) / np.asarray(f_core), np.nan
            )
    except Exception:
        halo = np.empty(0)

    a2, b2 = np.asarray(objs["a"], float), np.asarray(objs["b"], float)
    with np.errstate(divide="ignore", invalid="ignore"):
        el = np.where(b2 > 0, a2 / b2, np.nan)
    return StarField(
        xy,
        objs["flux"].astype(np.float64),
        fwhm,
        float(bkg.globalback),
        rms,
        hfr=hfr,
        peak=objs["peak"].astype(np.float64),
        elong=el,
        halo=np.asarray(halo, dtype=np.float64),
    )
