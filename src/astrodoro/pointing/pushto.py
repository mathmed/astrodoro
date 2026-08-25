"""Push-to: a digital setting circle without encoders.

Given where the tube points and a target, this says which way to push. On a
Dobsonian the movements are in altitude and azimuth — even on an equatorial
platform the tube still moves in alt-az — so that is how the guidance has to be
expressed.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.catalog import angular_sep
from ..i18n import gettext as _

try:
    from astropy.utils import iers
    iers.conf.auto_download = False
except Exception:
    pass


@dataclass
class Guidance:
    separation_deg: float
    delta_alt_deg: float          # positive: raise the tube
    delta_az_deg: float           # positive: turn west
    target_alt_deg: float
    target_az_deg: float
    on_target: bool
    text: str
    zenith_warning: bool = False

    @property
    def arrow(self) -> tuple[float, float]:
        """Unit vector for drawing the on-screen arrow (x = azimuth, y = altitude)."""
        v = np.array([self.delta_az_deg, self.delta_alt_deg])
        n = np.linalg.norm(v)
        return (0.0, 0.0) if n < 1e-9 else tuple(v / n)


def guide(current: tuple[float, float], target: tuple[float, float],
          latitude: float, longitude: float, when=None,
          elevation_m: float = 0.0, tolerance_deg: float = 0.25,
          fov_deg: float | None = None) -> Guidance:
    """`current` and `target` in (RA, Dec) degrees."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    t = Time(when) if when is not None else Time.now()
    site = EarthLocation(lat=latitude * u.deg, lon=longitude * u.deg,
                         height=elevation_m * u.m)
    frame = AltAz(obstime=t, location=site)

    cur = SkyCoord(ra=current[0] * u.deg,
                   dec=current[1] * u.deg).transform_to(frame)
    tgt = SkyCoord(ra=target[0] * u.deg,
                   dec=target[1] * u.deg).transform_to(frame)

    d_alt = float(tgt.alt.deg - cur.alt.deg)
    d_az = float(((tgt.az.deg - cur.az.deg + 180.0) % 360.0) - 180.0)
    sep = angular_sep(current[0], current[1], target[0], target[1])

    tol = tolerance_deg if fov_deg is None else min(tolerance_deg, fov_deg * 0.25)
    on = sep <= tol
    # Near the zenith the azimuth converges: the azimuth correction is amplified
    # by 1/cos(alt) and becomes unstable, and the Dobsonian itself gets hard to
    # move precisely there.
    zenith = float(tgt.alt.deg) > 80.0

    if float(tgt.alt.deg) < 0:
        text = _("target below the horizon (altitude {alt:.1f} deg)").format(
            alt=tgt.alt.deg)
    elif on:
        text = _("on target — {arcmin:.1f}' from the centre").format(
            arcmin=sep * 60)
    else:
        bits = []
        if abs(d_alt) > tol / 2:
            bits.append(_("up {amount}").format(amount=_fmt(abs(d_alt)))
                        if d_alt > 0
                        else _("down {amount}").format(amount=_fmt(abs(d_alt))))
        if abs(d_az) > tol / 2:
            bits.append(_("turn {amount} west").format(amount=_fmt(abs(d_az)))
                        if d_az > 0
                        else _("turn {amount} east").format(
                            amount=_fmt(abs(d_az))))
        text = " · ".join(bits) or _("{arcmin:.1f}' from the target").format(
            arcmin=sep * 60)
    if zenith and not on:
        text += _(" (near the zenith: azimuth is unstable, correct altitude "
                  "first)")
    return Guidance(sep, d_alt, d_az, float(tgt.alt.deg), float(tgt.az.deg),
                    on, text, zenith_warning=zenith)


def _fmt(deg: float) -> str:
    return f"{deg*60:.0f}'" if deg < 1.0 else f"{deg:.2f}°"
