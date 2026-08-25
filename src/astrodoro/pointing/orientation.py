"""Tube orientation from the sensors of a phone strapped to the telescope.

No encoders, no goto and no plate solving: the phone is already a two-axis
digital setting circle. The mechanics follow AstroHopper (Artyom Beilis,
GPL-3): the ZXY matrix of the DeviceOrientation event, the top of the device as
the sighting axis, and a single alignment on a known star.

Why one star is enough: gravity already locks two of the three degrees of
freedom — the device knows which way is down to a fraction of a degree. What
remains is rotation about the vertical, which is exactly what a compass gets
wrong by several degrees next to a metal tube with a mirror and a focuser.
Sighting a star and naming it resolves that degree; the same measurement also
absorbs the altitude error of a crooked mount, which is why the alignment
corrects two axes rather than one.

After alignment the compass is no longer used for anything: the `alpha` that
enters here is always the one from the `deviceorientation` event (relative,
integrated from the gyroscope), and its unknown offset is baked into the
alignment matrix. The compass only serves the map before the first alignment.

The equatorial platform needs no special treatment: the sensor measures against
gravity, so it also measures whatever the platform tilted. A Dobsonian encoder
measures the tube relative to its base and therefore lies once the platform is
running — here the problem does not exist.

Alignment is always on **one** star, and a new one replaces the previous. See
docs/design-notes.md for what a removed two-star model achieved and why mount
twist cannot be corrected by a rotation in the sky.

Convention: unit ENU vectors, (east, north, up). Azimuth from north towards
east, altitude above the horizon, as everywhere else in the program.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

DEG = np.pi / 180.0


def rotation_matrix(alpha: float, beta: float, gamma: float) -> np.ndarray:
    """DeviceOrientation ZXY matrix: device coordinates -> ENU.

    This is the W3C normative example (orientation-event, worked example 2).
    Rewriting it "more elegantly" invites a sign error: the three angles are
    intrinsic and the Z-X'-Y'' order does not commute.
    """
    x, y, z = beta * DEG, gamma * DEG, alpha * DEG
    cX, cY, cZ = np.cos(x), np.cos(y), np.cos(z)
    sX, sY, sZ = np.sin(x), np.sin(y), np.sin(z)
    return np.array([
        [cZ * cY - sZ * sX * sY, -cX * sZ,  cY * sZ * sX + cZ * sY],
        [cY * sZ + cZ * sX * sY,  cZ * cX,  sZ * sY - cZ * cY * sX],
        [-cX * sY,                sX,       cX * cY],
    ])


def normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else np.array([0.0, 1.0, 0.0])


def altaz_to_enu(alt_deg: float, az_deg: float) -> np.ndarray:
    a, z = alt_deg * DEG, az_deg * DEG
    return np.array([np.cos(a) * np.sin(z), np.cos(a) * np.cos(z), np.sin(a)])


def enu_to_altaz(v: np.ndarray) -> tuple[float, float]:
    v = normalize(np.asarray(v, dtype=float))
    alt = float(np.degrees(np.arcsin(np.clip(v[2], -1.0, 1.0))))
    az = float(np.degrees(np.arctan2(v[0], v[1])) % 360.0)
    return alt, az


def separation(u: np.ndarray, v: np.ndarray) -> float:
    """Angular separation between two vectors, in degrees."""
    d = float(np.clip(np.dot(normalize(u), normalize(v)), -1.0, 1.0))
    return float(np.degrees(np.arccos(d)))


@dataclass
class Alignment:
    """The correction measured on one star: the rotation taking the sighting
    direction onto the real sky."""
    mat: np.ndarray = field(default_factory=lambda: np.eye(3))
    star: str = ""
    delta_alt_deg: float = 0.0
    delta_az_deg: float = 0.0

    @property
    def aligned(self) -> bool:
        return bool(self.star)

    @property
    def error_deg(self) -> float:
        """Size of the correction — how wrong the sensor was when aligning."""
        return float(np.hypot(self.delta_alt_deg, self.delta_az_deg))

    def apply(self, v: np.ndarray) -> np.ndarray:
        return self.mat @ v

    @classmethod
    def solve(cls, fwd: np.ndarray, left: np.ndarray, star_enu: np.ndarray,
              name: str = "") -> Alignment:
        """Align: the tube points at `fwd` according to the sensor, but really
        at `star_enu`. Returns the rotation that corrects the difference.

        Two rotations rather than a general Wahba matrix because there is only
        one measurement: a turn about the vertical (azimuth) and a turn about
        the camera's horizontal axis (altitude). A general rotation would need a
        second star and would also spin the field about the sighting axis, a
        degree of freedom a Dobsonian does not care about.
        """
        fwd = normalize(np.asarray(fwd, dtype=float))
        star = normalize(np.asarray(star_enu, dtype=float))
        left = normalize(np.asarray(left, dtype=float))

        fh, sh = normalize(fwd * [1, 1, 0]), normalize(star * [1, 1, 0])
        # atan2, not asin: with asin the alignment only works while the azimuth
        # error fits in +-90 degrees. Without a compass the device's `alpha`
        # starts with an arbitrary offset and the initial error can be 150
        # degrees; asin returns the supplement, the alignment "succeeds" without
        # complaining, and the tube then points at the wrong side of the sky.
        d_az = float(np.arctan2(np.cross(sh, fh)[2], float(np.dot(sh, fh))))
        d_alt = float(np.arcsin(np.clip(star[2], -1, 1))
                      - np.arcsin(np.clip(fwd[2], -1, 1)))

        c, s = np.cos(d_az), np.sin(d_az)
        m_az = np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])
        # Rodrigues about the camera's horizontal axis.
        u0, u1, u2 = left
        W = np.array([[0.0, -u2, u1], [u2, 0.0, -u0], [-u1, u0, 0.0]])
        t = -d_alt
        m_alt = np.eye(3) + np.sin(t) * W + (1.0 - np.cos(t)) * (W @ W)

        return cls(m_az @ m_alt, name,
                   float(np.degrees(d_alt)), float(np.degrees(d_az)))


def rays_from_fwd(fwd: np.ndarray, align: Alignment | None = None
                  ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(up, left, forward) from an already known sighting direction.

    Separate from `camera_rays` because smoothing happens on the sighting
    vector: everything else derives from it, so there is nothing to smooth
    twice.
    """
    fwd = normalize(np.asarray(fwd, dtype=float))
    h = np.hypot(fwd[0], fwd[1])
    if h < 1e-9:
        # Pointing at the zenith: azimuth is meaningless, any horizontal will do.
        left = np.array([-1.0, 0.0, 0.0])
    else:
        left = np.array([-fwd[1] / h, fwd[0] / h, 0.0])
    top = np.cross(fwd, left)

    if align is not None:
        return align.apply(top), align.apply(left), align.apply(fwd)
    return top, left, fwd


def camera_rays(alpha: float, beta: float, gamma: float,
                align: Alignment | None = None
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(up, left, forward) of the tube in ENU, corrected by the alignment.

    The sighting direction is the device's **+Y**: the top of the phone points
    where the tube points. Roll is discarded on purpose — `left` is forced
    horizontal — because spinning the phone about its own sighting axis does not
    change where the tube points, and demanding a perfectly straight mount would
    be asking too much in the dark.
    """
    M = rotation_matrix(alpha, beta, gamma)
    return rays_from_fwd(M @ np.array([0.0, 1.0, 0.0]), align)


def bearing(target_enu: np.ndarray, rays: tuple) -> tuple[float, float, float]:
    """The target in the camera frame: (x right, y up, z forward).

    z <= 0 means the target is behind the tube.
    """
    top, left, fwd = rays
    t = normalize(np.asarray(target_enu, dtype=float))
    return float(-np.dot(left, t)), float(np.dot(top, t)), float(np.dot(fwd, t))


@dataclass
class Smoother:
    """Exponential average of the sighting direction, as a vector.

    The sensor jitters a few tenths of a degree and the raw value makes the
    arrow flicker. The average is taken on the vector, not on the angles: in
    alpha/azimuth an arithmetic mean crosses the 360-degree discontinuity and
    jumps half a turn.
    """
    tau: float = 0.25          # seconds to reach ~63% of the response
    _v: np.ndarray | None = None
    _t: float | None = None

    def update(self, v: np.ndarray, t: float) -> np.ndarray:
        v = normalize(np.asarray(v, dtype=float))
        if self._v is None or self._t is None or t <= self._t:
            self._v, self._t = v, t
            return v
        k = 1.0 - np.exp(-(t - self._t) / max(self.tau, 1e-3))
        self._v = normalize(self._v + k * (v - self._v))
        self._t = t
        return self._v

    def reset(self) -> None:
        self._v = self._t = None
