"""Polar alignment of the platform, measured from the residual field rotation.

`polar.py` determines the platform's axis from solved positions, which nothing
in this program produces. This module gets to the same axis from what the
program *does* measure all night: how fast the field rotates.

The physics is one line. The sky turns about the celestial pole `P`; the
platform turns the tube about its own mechanical axis `p`. What the sensor sees
is the difference, a slow rotation about `e = p - P`. Decomposed against the
line of sight `u` of whatever is being imaged:

* the part of `e` perpendicular to `u` moves the target across the frame — the
  drift, whose direction on the sensor is unknown without a plate solve;
* the part **along** `u` turns the field about its own centre, at

      rotation rate = sidereal rate x (e . u)

  which is a signed scalar, and needs no plate solve at all.

So each target measured gives one linear equation in `e`, which has two degrees
of freedom (it is perpendicular to `P`). Two targets far apart on the sky
determine it; three or more leave a residual to judge the fit by. From `e`
comes the axis, and `polar.correction` turns the axis into the two screws.

The equation is written in hour angle, not in right ascension, and evaluated at
the middle of each measurement. The platform is bolted to the ground: its axis
is fixed against the horizon, not against the stars, so `e` is only constant in
a frame that turns with the Earth. Using RA instead reads the rate at the start
of the measurement rather than at its middle, and biases the answer by 3% over
ten minutes and 13% over forty — measured in `tests/test_platform_align.py`.

Two things this cannot do on its own. The measured rotation lives in sensor
coordinates, so whether its sign agrees with the sky depends on the parity of
the optical train — a constant of the setup, carried here as `parity`. What
fixes `parity = 1` is the synthetic sensor in `tests/test_platform_align.py`,
whose axes are right-handed with the line of sight; a train with an odd number
of reflections mirrors the image and wants the other sign, which `verify`
settles from a re-measurement instead of from counting mirrors. And a single
target only ever constrains the projection of `e` along it: `ideal_next` is
what says where to point to close the other half.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from ..i18n import gettext as _
from . import polar, register
from .catalog import radec_to_vec
from .equatorial import Fit, fit_rate
from .tonight import horizon

#: Degrees the sky turns per minute: 360° in a sidereal day.
SIDEREAL_DEG_PER_MIN = 360.0 / (23.9344696 * 60.0)

#: A measurement is only as good as its baseline: the rotation to be measured
#: is thousandths of a degree per minute, and the slope's uncertainty falls
#: with the span faster than with the number of frames.
MIN_SPAN_S = 240.0
MIN_SAMPLES = 12

#: Two stations whose lines of sight project onto nearly the same direction
#: give nearly the same equation twice. Above this condition number the
#: solution is dominated by whichever way the noise fell.
MAX_CONDITION = 4.0


@dataclass
class Station:
    """One completed measurement: a target, and how fast its field turned."""
    ra: float
    dec: float
    #: Local sidereal time at the middle of the measurement, in degrees. With
    #: it the target becomes an hour angle, which is the frame the platform's
    #: error stands still in.
    lst_deg: float
    when: float                   # unix time, middle of the measurement
    rate_deg_min: float
    r2: float
    n: int
    span_s: float
    name: str = ""

    @property
    def weight(self) -> float:
        """How much this station is trusted against the others.

        The span dominates because the slope's uncertainty does: doubling the
        baseline halves it, while doubling the frame count only helps by √2.
        """
        return float(np.clip(self.r2, 0.0, 1.0) * self.span_s / 60.0)


class RotationRun:
    """Measures one station: the field's rotation against its own reference.

    Deliberately independent of `LiveStacker`. Alignment happens before there
    is anything to integrate, and measuring must not consume disk, platform
    travel or the accumulator — only the star lists the capture already
    produces.
    """

    def __init__(self, ra: float, dec: float, name: str = "",
                 min_span_s: float = MIN_SPAN_S,
                 min_samples: int = MIN_SAMPLES, min_stars: int = 6):
        self.ra = float(ra)
        self.dec = float(dec)
        self.name = name
        self.min_span_s = float(min_span_s)
        self.min_samples = int(min_samples)
        self.min_stars = int(min_stars)
        self.reference: np.ndarray | None = None
        self.reason = ""
        self.n_rejected = 0
        self._t: list[float] = []
        self._rot: list[float] = []

    # ------------------------------------------------------------------- data
    def add(self, stars_xy: np.ndarray, t: float | None = None) -> bool:
        """Feed one frame's star positions. True when it became a sample."""
        t = time.time() if t is None else t
        xy = np.asarray(stars_xy, dtype=float)
        if len(xy) < self.min_stars:
            self.reason = _("only {n} stars — the field is too poor to "
                            "measure").format(n=len(xy))
            self.n_rejected += 1
            return False
        if self.reference is None:
            self.reference = xy
            self._t.append(t)
            self._rot.append(0.0)
            self.reason = ""
            return True
        al = register.estimate(xy, self.reference)
        if not al.ok:
            self.reason = al.reason
            self.n_rejected += 1
            return False
        return self.add_rotation(al.rotation_deg, t)

    def add_rotation(self, rotation_deg: float, t: float | None = None) -> bool:
        """Feed a rotation already measured against a reference of its own.

        What the stacker reports frame by frame is this same angle, so a
        measurement can run over a live stack instead of over its own
        reference — the only condition is that the reference does not move,
        which is exactly what `LiveStacker` guarantees once the geometry is
        locked.
        """
        if not np.isfinite(rotation_deg):
            self.n_rejected += 1
            return False
        if self.reference is None:
            self.reference = np.empty((0, 2))
        self.reason = ""
        self._t.append(time.time() if t is None else t)
        self._rot.append(float(rotation_deg))
        return True

    def clear(self) -> None:
        self.reference = None
        self.reason = ""
        self.n_rejected = 0
        self._t.clear()
        self._rot.clear()

    # ------------------------------------------------------------------- fits
    @property
    def n(self) -> int:
        return len(self._t)

    @property
    def span_s(self) -> float:
        return self._t[-1] - self._t[0] if len(self._t) > 1 else 0.0

    def fit(self) -> Fit:
        return fit_rate(self._t, self._rot)

    @property
    def ready(self) -> bool:
        return (self.n >= self.min_samples and self.span_s >= self.min_span_s
                and self.fit().r2 >= 0.5)

    @property
    def progress(self) -> float:
        """0..1 over whichever of the two floors is further from being met."""
        if self.min_span_s <= 0 or self.min_samples <= 0:
            return 1.0
        return float(np.clip(min(self.span_s / self.min_span_s,
                                 self.n / self.min_samples), 0.0, 1.0))

    def station(self, lst_deg: float) -> Station | None:
        """The finished measurement, or None while it is not finished.

        `lst_deg` is the sidereal time at the middle of the run, which the
        caller is the one able to compute — it owns the clock and the site.
        """
        return station_from(self.status(), lst_deg)

    @property
    def middle(self) -> float:
        """Unix time of the middle of the run so far — where its rate applies."""
        return 0.5 * (self._t[0] + self._t[-1]) if self._t else time.time()

    def status(self) -> dict:
        f = self.fit()
        return {"name": self.name, "ra": self.ra, "dec": self.dec,
                "n": self.n, "span_s": self.span_s, "progress": self.progress,
                "middle": self.middle, "rate_deg_min": f.rate, "r2": f.r2,
                "ready": self.ready, "rejected": self.n_rejected,
                "reason": self.reason}


def station_from(status: dict, lst_deg: float) -> Station | None:
    """A finished station out of a `RotationRun.status()`, or None if it is not
    finished yet.

    The run happens in the capture thread and the sidereal time belongs to
    whoever holds the clock and the site, so the two meet here.
    """
    if not status or not status.get("ready"):
        return None
    rate = status.get("rate_deg_min", float("nan"))
    if not np.isfinite(rate):
        return None
    return Station(ra=status["ra"], dec=status["dec"], lst_deg=float(lst_deg),
                   when=status["middle"], rate_deg_min=rate,
                   r2=status["r2"], n=status["n"], span_s=status["span_s"],
                   name=status.get("name", ""))


@dataclass
class AlignResult:
    """The correction, and everything needed to judge whether to trust it."""
    alt_error_deg: float          # positive: the platform axis is too high
    az_error_deg: float           # positive: axis rotated east of the pole
    total_error_deg: float
    axis_ra: float
    axis_dec: float
    n_stations: int
    condition: float              # 1 is ideal geometry, large is degenerate
    residual_deg_min: float       # how well the stations agree, nan with n < 3
    advice: str = ""
    note: str = ""

    @property
    def confident(self) -> bool:
        return self.n_stations >= 2 and self.condition <= MAX_CONDITION

    @property
    def good(self) -> bool:
        return self.total_error_deg < 0.25


def _design(stations: list[Station]) -> tuple[np.ndarray, np.ndarray,
                                              np.ndarray]:
    """Weighted (A, b, w): one row per station, `A e = b` in radians.

    `e` comes out in the hour-angle frame — the equatorial frame turned by the
    sidereal time — because that is where it is a constant.
    """
    u = radec_to_vec(np.array([s.ra - s.lst_deg for s in stations]),
                     np.array([s.dec for s in stations]))
    A = u[:, :2]
    b = -np.array([s.rate_deg_min for s in stations]) / SIDEREAL_DEG_PER_MIN
    w = np.array([max(s.weight, 1e-6) for s in stations])
    return A, b, w


def solve(stations: list[Station], latitude: float, longitude: float,
          lst_deg: float, when=None, elevation_m: float = 0.0,
          parity: int = 1) -> AlignResult | None:
    """The platform's alignment error from a list of measured stations.

    `when` and `lst_deg` are the instant the correction will be applied — the
    present, not the instant of the measurements: the error is measured against
    the horizon and the screws are turned now, so it is this instant's sidereal
    time that takes it back to equatorial coordinates.
    """
    usable = [s for s in stations if np.isfinite(s.rate_deg_min)]
    if not usable:
        return None
    if when is None:
        when = datetime.now(UTC)

    A, b, w = _design(usable)
    b = b * (1.0 if parity >= 0 else -1.0)
    sw = np.sqrt(w)[:, None]
    Aw, bw = A * sw, b * sw[:, 0]

    sol, _res, _rank, sv = np.linalg.lstsq(Aw, bw, rcond=None)
    cond = float(sv[0] / sv[-1]) if len(sv) > 1 and sv[-1] > 0 else float("inf")
    e = np.array([sol[0], sol[1], 0.0])          # radians, perpendicular to z

    residual = float("nan")
    if len(usable) > 2:
        r = (A @ sol - b) * SIDEREAL_DEG_PER_MIN
        residual = float(np.sqrt(np.mean(r ** 2)))

    # The axis is a line; name it by the pole the observer can see, which is
    # the end `polar.correction` compares against and the end you aim at.
    sign = -1.0 if latitude < 0 else 1.0
    axis = sign * (np.array([0.0, 0.0, 1.0]) + _rotate_z(e, lst_deg))
    axis = axis / np.linalg.norm(axis)

    # The residual is a disagreement in rate; as an axis error it is that rate
    # over the sidereal rate, which comes out in radians.
    rms = (float(np.degrees(residual / SIDEREAL_DEG_PER_MIN))
           if np.isfinite(residual) else 0.0)
    pol = polar.correction(axis, latitude, longitude, when, elevation_m,
                           n_points=len(usable), rms_deg=rms)
    res = AlignResult(
        alt_error_deg=pol.alt_error_deg, az_error_deg=pol.az_error_deg,
        total_error_deg=pol.total_error_deg,
        axis_ra=pol.axis_ra, axis_dec=pol.axis_dec,
        n_stations=len(usable), condition=cond, residual_deg_min=residual,
        advice=pol.advice)
    res.note = _note(res)
    return res


def _rotate_z(v: np.ndarray, deg: float) -> np.ndarray:
    """Back from the hour-angle frame to equatorial: a turn by the sidereal
    time about the pole, which both frames share."""
    c, s = np.cos(np.deg2rad(deg)), np.sin(np.deg2rad(deg))
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1], v[2]])


def _note(r: AlignResult) -> str:
    if r.n_stations < 2:
        return _("one target only: this is the error along that direction, "
                 "not the whole of it — measure a second one")
    if r.condition > MAX_CONDITION:
        return _("the targets are too close together on the sky for the "
                 "correction to separate altitude from azimuth")
    if np.isfinite(r.residual_deg_min) and r.residual_deg_min > 0.002:
        return _("the targets disagree by {rate:.3f} deg/min — one of the "
                 "measurements is noisy").format(rate=r.residual_deg_min)
    return ""


def ideal_next(stations: list[Station], latitude: float, lst_deg: float,
               min_alt: float = 25.0, max_alt: float = 80.0
               ) -> tuple[float, float, float, float] | None:
    """Where to point next: (ra, dec, alt, az), or None if nothing qualifies.

    The direction worth measuring is the one the stations so far say least
    about — the weakest singular direction of the design matrix — and among the
    positions that see it, the one furthest from the pole, because the
    projection onto the equatorial plane is what carries the signal. The
    horizon is what decides the rest.
    """
    ra = np.arange(0.0, 360.0, 5.0)
    dec = np.arange(-80.0, 81.0, 5.0)
    ra, dec = np.meshgrid(ra, dec)
    ra, dec = ra.ravel(), dec.ravel()

    alt, az, _ha = horizon(ra, dec, latitude, lst_deg)
    keep = (alt >= min_alt) & (alt <= max_alt)
    if not keep.any():
        return None
    ra, dec, alt, az = ra[keep], dec[keep], alt[keep], az[keep]

    u = radec_to_vec(ra - lst_deg, dec)[:, :2]
    if stations:
        A, _b, w = _design(stations)
        _u, _s, Vt = np.linalg.svd(A * np.sqrt(w)[:, None])
        weak = Vt[-1]
        score = np.abs(u @ weak)
    else:
        score = np.linalg.norm(u, axis=1)
    j = int(np.argmax(score))
    return float(ra[j]), float(dec[j]), float(alt[j]), float(az[j])


def verify(before: Station, after: Station) -> dict:
    """Did the correction help? Re-measuring the same target answers it.

    A correction applied with the sign backwards does not leave the error
    alone, it doubles it — which is what makes this the practical test for the
    optical train's parity, and cheaper than reasoning about how many mirrors
    the light bounced off.
    """
    b, a = abs(before.rate_deg_min), abs(after.rate_deg_min)
    improved = a < b * 0.8
    worse = a > b * 1.2
    if improved:
        text = _("residual rotation fell from {before:.4f} to {after:.4f} "
                 "deg/min").format(before=b, after=a)
    elif worse:
        text = _("residual rotation rose from {before:.4f} to {after:.4f} "
                 "deg/min — the correction was applied backwards: invert the "
                 "image parity in the settings and align again").format(
                     before=b, after=a)
    else:
        text = _("residual rotation barely moved ({before:.4f} to {after:.4f} "
                 "deg/min) — either the correction was too small to see or it "
                 "was not applied").format(before=b, after=a)
    return {"improved": improved, "worse": worse, "parity_suspect": worse,
            "before_deg_min": b, "after_deg_min": a, "text": text}
