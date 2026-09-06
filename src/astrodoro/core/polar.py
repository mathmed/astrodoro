"""Polar alignment of the platform, by geometric determination of its axis.

The method: points rotating about an axis A describe a circle, and that circle
lies in a plane perpendicular to A. So solve the field at three or more moments,
fit a plane to the resulting unit vectors, and the plane's normal *is* the real
axis of the platform. Comparing it with the celestial pole gives the correction
in altitude and azimuth.

This is exact and does not depend on the approximate drift-align formulas, which
besides being easy to get wrong presuppose stars in specific positions. Here any
target works, anywhere in the sky — which is what you have with a Dobsonian
pointed wherever it fits.

In the southern hemisphere it matters even more: Sigma Octantis is magnitude 5.4
and invisible from an urban sky, so aligning by eye on the pole is out.

With the plate solver removed nothing in the interface produces solved
positions, so `analyse` has no caller there. The second half of the module does:
`correction` turns an axis into the two screw movements, and `platform_align`
arrives at an axis from the residual field rotation instead of from positions.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..i18n import gettext as _
from .catalog import radec_to_vec, vec_to_radec

try:                                    # avoid a download attempt in the field
    from astropy.utils import iers
    iers.conf.auto_download = False
except Exception:
    pass


@dataclass
class PolarResult:
    axis_ra: float
    axis_dec: float
    total_error_deg: float
    alt_error_deg: float          # positive: the platform axis is too high
    az_error_deg: float           # positive: axis rotated east of the pole
    n_points: int
    rms_deg: float
    hemisphere: str
    advice: str = ""

    @property
    def good(self) -> bool:
        return self.total_error_deg < 0.25


def fit_rotation_axis(vectors: np.ndarray,
                      expect_south: bool = True) -> tuple[np.ndarray, float]:
    """Rotation axis from unit vectors of the same target at different times.

    Returns (axis, plane-fit rms in degrees). Needs at least 3 points; the
    longer the arc travelled, the better conditioned the fit — 15 to 30 minutes
    of rotation is much better than 2.
    """
    P = np.asarray(vectors, dtype=float)
    P = P / np.linalg.norm(P, axis=1, keepdims=True)
    if len(P) < 3:
        raise ValueError("at least 3 points are required")

    c = P.mean(axis=0)
    _u, _s, Vt = np.linalg.svd(P - c)
    n = Vt[2] / np.linalg.norm(Vt[2])

    # Residual: distance of the points to the fitted plane, in degrees.
    d = (P - c) @ n
    rms = float(np.degrees(np.sqrt(np.mean(d ** 2))))

    # Sign ambiguity: pick the expected hemisphere.
    if (expect_south and n[2] > 0) or (not expect_south and n[2] < 0):
        n = -n
    return n, rms


def correction(axis: np.ndarray, latitude: float, longitude: float, when,
               elevation_m: float = 0.0, n_points: int = 0,
               rms_deg: float = 0.0) -> PolarResult:
    """Mechanical correction that brings `axis` onto the visible pole.

    `axis` is the platform's rotation axis as a unit vector in equatorial
    coordinates, pointing at the hemisphere's own pole. `when` is the instant
    the correction is applied (datetime or astropy Time), and it matters: the
    axis is measured in equatorial coordinates but the screws you turn are in
    the local frame, and the conversion between the two depends on sidereal
    time.

    Shared by the two ways of arriving at an axis — `analyse` from solved
    positions, `platform_align` from the residual field rotation.
    """
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    south = latitude < 0
    ra, dec = vec_to_radec(axis)

    site = EarthLocation(lat=latitude * u.deg, lon=longitude * u.deg,
                         height=elevation_m * u.m)
    t = when if isinstance(when, Time) else Time(when)
    frame = AltAz(obstime=t, location=site)

    axis_h = SkyCoord(ra=ra * u.deg, dec=dec * u.deg,
                      frame="icrs").transform_to(frame)
    pole_dec = -90.0 if south else 90.0
    pole_h = SkyCoord(ra=0 * u.deg, dec=pole_dec * u.deg,
                      frame="icrs").transform_to(frame)

    alt_err = float(axis_h.alt.deg - pole_h.alt.deg)
    az_err = float(((axis_h.az.deg - pole_h.az.deg + 180.0) % 360.0) - 180.0)
    total = float(np.degrees(np.arccos(np.clip(
        np.dot(axis / np.linalg.norm(axis),
               radec_to_vec(np.array([0.0]), np.array([pole_dec]))[0]),
        -1, 1))))

    res = PolarResult(
        axis_ra=ra, axis_dec=dec, total_error_deg=total,
        alt_error_deg=alt_err, az_error_deg=az_err,
        n_points=n_points, rms_deg=rms_deg,
        hemisphere="south" if south else "north",
    )
    res.advice = _advice(res)
    return res


def analyse(radecs: list[tuple[float, float]], latitude: float,
            longitude: float, when, elevation_m: float = 0.0) -> PolarResult:
    """From a list of solved (RA, Dec) comes the platform correction.

    `when` is the mean instant of the observations.
    """
    vecs = radec_to_vec(np.array([r for r, _ in radecs]),
                        np.array([d for _, d in radecs]))
    axis, rms = fit_rotation_axis(vecs, expect_south=latitude < 0)
    return correction(axis, latitude, longitude, when, elevation_m,
                      n_points=len(radecs), rms_deg=rms)


def _advice(r: PolarResult) -> str:
    total = _("total error {arcmin:.1f}'").format(
        arcmin=r.total_error_deg * 60)
    if r.good:
        return _("good alignment: {total}").format(total=total)
    parts = []
    if abs(r.alt_error_deg) > 0.03:
        arcmin = abs(r.alt_error_deg) * 60
        parts.append(_("lower the platform {arcmin:.1f}'").format(arcmin=arcmin)
                     if r.alt_error_deg > 0
                     else _("raise the platform {arcmin:.1f}'").format(
                         arcmin=arcmin))
    if abs(r.az_error_deg) > 0.03:
        arcmin = abs(r.az_error_deg) * 60
        parts.append(_("rotate {arcmin:.1f}' west").format(arcmin=arcmin)
                     if r.az_error_deg > 0
                     else _("rotate {arcmin:.1f}' east").format(arcmin=arcmin))
    return f"{total} — " + ", ".join(parts) if parts else total


def rotate_about(v: np.ndarray, axis: np.ndarray,
                 angle_deg: float) -> np.ndarray:
    """Rodrigues rotation — used by tests to synthesise known rotations."""
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    th = np.deg2rad(angle_deg)
    return (v * np.cos(th) + np.cross(a, v) * np.sin(th)
            + a * (a @ v) * (1 - np.cos(th)))
