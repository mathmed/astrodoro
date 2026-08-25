"""Star detection and per-frame quality metrics."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import sep


@dataclass
class StarField:
    xy: np.ndarray          # (N, 2) float64, full-resolution coordinates
    flux: np.ndarray        # (N,)
    fwhm: np.ndarray        # (N,) in full-resolution pixels
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
        """Median elongation (major axis / minor axis).

        Per star, high elongation is a satellite trail or a cosmic ray and is
        already filtered during detection. A high *median* means something
        else: every star is elongated, so the tube moved during the exposure.
        No per-star filter catches that.
        """
        return float(np.median(self.elong)) if len(self.elong) else float("nan")

    @property
    def median_halo(self) -> float:
        """Wide-aperture flux over core flux, median of the 20 brightest stars.

        Sees what elongation cannot: when the tube is nudged *during* the
        exposure, the star keeps a point-like core (where the tube sat still)
        with a faint trail around it (the excursion). `sep` detects the core,
        which measures better than the session average, and the trail never
        enters the object's moments.

        A perfect field gives ~1.0 (all flux in the core); 2.0 means half the
        flux is outside it. Only the brightest stars are measured: on a faint
        star the wide aperture sums more noise than signal and the ratio is
        unstable.
        """
        if not len(self.halo):
            return float("nan")
        v = self.halo[np.isfinite(self.halo)]
        return float(np.median(v)) if len(v) else float("nan")

    @property
    def median_hfr(self) -> float:
        """Median HFR. More stable than FWHM when focus is bad, because it does
        not depend on fitting a profile to a star that turned into a donut."""
        return float(np.median(self.hfr)) if len(self.hfr) else float("nan")


def detect(lum: np.ndarray, scale: float = 2.0, thresh_sigma: float = 5.0,
           min_area: int = 4, max_stars: int = 60, central: float = 0.70,
           saturation: float | None = None, min_central: int = 12,
           halo_radius: float = 50.0) -> StarField:
    """Extract stars from a luminance image.

    `lum` normally comes from `cfa_to_luminance` (half resolution); `scale`
    converts coordinates back to full resolution.

    `central` restricts detection to the central fraction of the frame. On a
    fast Dobsonian, coma at the edges biases the centroid and poisons the fit —
    the stack uses the whole frame, but registration only trusts the centre.

    Saturated stars are discarded: their centroid is flattened and drags the
    transform.

    `halo_radius` (full-resolution pixels) is the wide halo aperture; see
    `StarField.median_halo`.
    """
    data = np.ascontiguousarray(lum, dtype=np.float32)
    bkg = sep.Background(data)
    sub = data - bkg.back()
    rms = float(bkg.globalrms)

    try:
        objs = sep.extract(sub, thresh_sigma, err=rms, minarea=min_area)
    except Exception:
        # sep raises if the deblending buffer fills up in a very dense field.
        objs = sep.extract(sub, thresh_sigma * 2, err=rms, minarea=min_area)

    if len(objs) == 0:
        return StarField(np.empty((0, 2)), np.empty(0), np.empty(0),
                         float(bkg.globalback), rms)

    x, y = objs["x"], objs["y"]
    keep = np.ones(len(objs), dtype=bool)

    if 0 < central < 1:
        h, w = data.shape
        mx, my = w * (1 - central) / 2, h * (1 - central) / 2
        inner = (x > mx) & (x < w - mx) & (y > my) & (y < h - my)
        # Giving up the edges is only worth it if the centre still yields
        # enough stars. In a poor field, or right after a manual recentring
        # that left the target off centre, requiring the centre would starve
        # registration and the stack would stall rejecting everything.
        if np.count_nonzero(keep & inner) >= min_central:
            keep &= inner

    if saturation is not None:
        keep &= objs["peak"] < saturation * 0.95

    # Absurd ellipticity: trail, cosmic ray or hot column.
    a, b = objs["a"], objs["b"]
    with np.errstate(divide="ignore", invalid="ignore"):
        elong = np.where(b > 0, a / b, np.inf)
    keep &= elong < 3.0

    objs, x, y = objs[keep], x[keep], y[keep]
    if len(objs) == 0:
        return StarField(np.empty((0, 2)), np.empty(0), np.empty(0),
                         float(bkg.globalback), rms)

    order = np.argsort(objs["flux"])[::-1][:max_stars]
    objs = objs[order]

    fwhm = 2.3548 * np.sqrt(np.abs(objs["a"] * objs["b"])) * scale
    xy = np.column_stack([objs["x"] * scale,
                          objs["y"] * scale]).astype(np.float64)

    # HFR: the radius containing half the flux. For a Gaussian it equals
    # sigma*sqrt(2 ln 2), but it stays well defined once a defocused star stops
    # being Gaussian — which is why it is the focusing metric.
    try:
        # Per-star integration radius, not a shared median: a badly defocused
        # star needs a wider window, and truncating it underestimates the HFR
        # exactly when you need it most.
        rmax = np.clip(np.asarray(objs["a"], dtype=np.float64) * 8.0, 4.0, 60.0)
        # Normalised by the isophotal flux. That underestimates HFR at extreme
        # defocus (~19% at sigma=7) because the isophotal flux ignores the
        # profile wings. Correcting with an aperture flux is worse: the wide
        # aperture picks up neighbours and background, and the error flips to
        # +250%. Over the useful focus range (HFR 1.4 to 3 px) the error stays
        # within +-2% and the metric is monotonic throughout, which is what
        # focusing needs.
        hfr, _flags = sep.flux_radius(sub, objs["x"], objs["y"], rmax, 0.5,
                                      normflux=objs["flux"], subpix=5)
        hfr = np.asarray(hfr, dtype=np.float64) * scale
    except Exception:
        hfr = fwhm / 2.3548 * 1.1774

    # Halo: wide-aperture flux against the core's. Costs 2.7 ms for 20 stars
    # (detect goes from 19.5 to 22.2 ms on a bin2 frame), which is 0.5% of a 5 s
    # sub cycle, and it is the only measurement that sees a smear with a
    # point-like core.
    try:
        m = min(len(objs), 20)
        r_core = np.maximum(np.asarray(hfr[:m], float) / scale * 2.5, 3.0)
        r_out = np.full(m, halo_radius / scale, dtype=np.float64)
        # A local background annulus on both sums: without it the sky residual
        # (and any nebulosity) enters multiplied by the wide aperture's area,
        # and the halo of a perfect field starts at 1.9 instead of 1.0.
        # The annulus sits far out (3x to 4x the radius) on purpose: closer in
        # it swallowed the tip of the smear itself and the measured frame fell
        # from 5.1 to 3.3 — the annulus measured the defect and subtracted it
        # from itself.
        annulus = (halo_radius / scale * 3.0, halo_radius / scale * 4.0)
        f_core, _e1, _f1 = sep.sum_circle(sub, objs["x"][:m], objs["y"][:m],
                                          r_core, bkgann=annulus, subpix=5)
        f_out, _e2, _f2 = sep.sum_circle(sub, objs["x"][:m], objs["y"][:m],
                                         r_out, bkgann=annulus, subpix=5)
        with np.errstate(divide="ignore", invalid="ignore"):
            halo = np.where(np.asarray(f_core) > 0,
                            np.asarray(f_out) / np.asarray(f_core), np.nan)
    except Exception:
        halo = np.empty(0)

    a2, b2 = np.asarray(objs["a"], float), np.asarray(objs["b"], float)
    with np.errstate(divide="ignore", invalid="ignore"):
        el = np.where(b2 > 0, a2 / b2, np.nan)
    return StarField(xy, objs["flux"].astype(np.float64), fwhm,
                     float(bkg.globalback), rms,
                     hfr=hfr, peak=objs["peak"].astype(np.float64), elong=el,
                     halo=np.asarray(halo, dtype=np.float64))
