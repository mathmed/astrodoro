from __future__ import annotations

import time
from collections import deque

import numpy as np

from . import brightstars, orientation
from .orientation import Alignment, Smoother

STALE_S = 1.0

ALIGN_WINDOW_S = 0.6


def sky_vectors(
    ra, dec, lat: float, lon: float, when=None, elevation_m: float = 0.0
) -> np.ndarray:
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    t = Time(when) if when is not None else Time.now()
    site = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elevation_m * u.m)
    aa = SkyCoord(ra=np.asarray(ra) * u.deg, dec=np.asarray(dec) * u.deg).transform_to(
        AltAz(obstime=t, location=site)
    )
    alt = np.radians(aa.alt.deg)
    az = np.radians(aa.az.deg)
    return np.column_stack(
        [np.cos(alt) * np.sin(az), np.cos(alt) * np.cos(az), np.sin(alt)]
    )


class Pointing:
    def __init__(
        self, lat: float, lon: float, elevation_m: float = 0.0, tau: float = 0.25
    ):
        self.lat, self.lon, self.elevation_m = lat, lon, elevation_m
        self.align: Alignment | None = None
        self.star: brightstars.Star | None = None
        self.compass: float | None = None
        self.manual_az: float = 0.0
        self._compass_offset: float = 0.0
        self._smooth = Smoother(tau=tau)
        self._buf: deque[tuple[float, float, float, float]] = deque(maxlen=200)
        self._fwd: np.ndarray | None = None
        self._t: float = 0.0
        self._radec: tuple[float, float] | None = None
        self._radec_t: float = -1e9
        self._radec_fwd: np.ndarray | None = None

    def feed(
        self,
        alpha: float,
        beta: float,
        gamma: float,
        compass: float | None = None,
        t: float | None = None,
    ) -> None:
        t = time.monotonic() if t is None else t
        self.compass = compass
        if self.align is None and compass is not None:
            self._compass_offset = (float(compass) - alpha) % 360.0
        alpha += self._compass_offset + self.manual_az
        self._buf.append((t, alpha, beta, gamma))
        self._fwd = self._smooth.update(self._sight(alpha, beta, gamma), t)
        self._t = t

    def nudge_az(self, delta_deg: float) -> None:
        self.manual_az = (self.manual_az - delta_deg) % 360.0

    def _sight(self, alpha: float, beta: float, gamma: float) -> np.ndarray:
        M = orientation.rotation_matrix(alpha, beta, gamma)
        return orientation.normalize(M @ np.array([0.0, 1.0, 0.0]))

    def reset(self) -> None:
        self.align = None
        self.star = None
        self.manual_az = 0.0
        self._compass_offset = 0.0
        self._smooth.reset()
        self._buf.clear()
        self._fwd = None
        self._radec = None
        self._radec_fwd = None

    @property
    def live(self) -> bool:
        return self._fwd is not None and (time.monotonic() - self._t) < STALE_S

    @property
    def age(self) -> float:
        return time.monotonic() - self._t if self._fwd is not None else float("inf")

    @property
    def aligned(self) -> bool:
        return self.align is not None

    def rays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        if self._fwd is None:
            return None
        return orientation.rays_from_fwd(self._fwd, self.align)

    @property
    def altaz(self) -> tuple[float, float] | None:
        r = self.rays()
        return None if r is None else orientation.enu_to_altaz(r[2])

    @property
    def radec(self) -> tuple[float, float] | None:
        aa = self.altaz
        if aa is None:
            return None
        now = time.monotonic()
        still = (
            self._radec_fwd is not None
            and self._fwd is not None
            and orientation.separation(self._radec_fwd, self._fwd) < 0.005
        )
        if self._radec is not None and still and now - self._radec_t < 0.3:
            return self._radec
        from astropy import units as u
        from astropy.coordinates import ICRS, AltAz, EarthLocation, SkyCoord
        from astropy.time import Time

        site = EarthLocation(
            lat=self.lat * u.deg, lon=self.lon * u.deg, height=self.elevation_m * u.m
        )
        c = SkyCoord(
            alt=aa[0] * u.deg,
            az=aa[1] * u.deg,
            frame=AltAz(obstime=Time.now(), location=site),
        )
        icrs = c.transform_to(ICRS())
        self._radec = (float(icrs.ra.deg), float(icrs.dec.deg))
        self._radec_t = now
        self._radec_fwd = None if self._fwd is None else self._fwd.copy()
        return self._radec

    def steadiness(self, window: float = ALIGN_WINDOW_S) -> float:
        samples = [
            self._sight(a, b, g) for t, a, b, g in self._buf if self._t - t <= window
        ]
        if len(samples) < 2:
            return float("inf")
        m = orientation.normalize(np.mean(samples, axis=0))
        return max(orientation.separation(m, v) for v in samples)

    def candidates(
        self, limit: int = 5, min_alt: float = 20.0, max_mag: float = 3.0
    ) -> list[tuple]:
        aa = self.altaz
        if aa is None:
            return []
        return brightstars.nearest(
            aa[0],
            aa[1],
            self.lat,
            self.lon,
            min_alt=min_alt,
            max_mag=max_mag,
            limit=limit,
            elevation_m=self.elevation_m,
        )

    def align_on(self, star: brightstars.Star) -> Alignment | None:
        window = [
            (t, a, b, g) for t, a, b, g in self._buf if self._t - t <= ALIGN_WINDOW_S
        ]
        if not window:
            return None
        _t, a, b, g = window[len(window) // 2]
        M = orientation.rotation_matrix(a, b, g)
        alt, az = brightstars.altaz(
            [star], self.lat, self.lon, elevation_m=self.elevation_m
        )[0]
        u = orientation.altaz_to_enu(alt, az)
        _top, left, fwd = orientation.rays_from_fwd(
            orientation.normalize(M @ np.array([0.0, 1.0, 0.0]))
        )
        self.align = Alignment.solve(fwd, left, u, star.full)
        self.star = star
        self._smooth.reset()
        self._radec = None
        return self.align
