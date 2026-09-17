from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import scipy.ndimage as ndi

from ..i18n import N_
from ..i18n import gettext as _
from .catalog import Obj
from .iers import use_bundled_table

use_bundled_table()


@dataclass(frozen=True)
class Body:
    key: str
    name: str
    radius_km: float
    surface_mag: float
    mag: float
    ring_ratio: float = 1.0

    @property
    def label(self) -> str:
        return _(self.name)


BODIES: dict[str, Body] = {
    b.key: b
    for b in (
        Body("moon", N_("Moon"), 1737.4, 3.4, -12.7),
        Body("mercury", N_("Mercury"), 2439.7, 2.9, -0.4),
        Body("venus", N_("Venus"), 6051.8, 1.9, -4.4),
        Body("mars", N_("Mars"), 3396.2, 3.7, -2.0),
        Body("jupiter", N_("Jupiter"), 71492.0, 5.4, -2.7),
        Body("saturn", N_("Saturn"), 60268.0, 6.9, 0.4, ring_ratio=2.27),
        Body("uranus", N_("Uranus"), 25559.0, 7.5, 5.7),
        Body("neptune", N_("Neptune"), 24764.0, 8.0, 7.8),
    )
}

MOON = BODIES["moon"]

HEADROOM = 0.85

FREEZE_S = 0.02


def exposure_for(body: str, moon_exposure_s: float) -> tuple[float, float]:
    b = BODIES[body]
    want = float(moon_exposure_s) * 10 ** (0.4 * (b.surface_mag - MOON.surface_mag))
    exposure = min(want, FREEZE_S)
    return exposure, (want / exposure if exposure > 0 else 1.0)


@dataclass
class BodyState:
    body: str
    when: datetime
    alt: float
    az: float
    ra: float
    dec: float
    illum: float
    waxing: bool
    diameter_arcmin: float
    distance_km: float

    @property
    def info(self) -> Body:
        return BODIES[self.body]

    @property
    def name(self) -> str:
        return self.info.label

    @property
    def up(self) -> bool:
        return self.alt > 0.0

    @property
    def is_moon(self) -> bool:
        return self.body == "moon"

    @property
    def extent_arcmin(self) -> float:
        return self.diameter_arcmin * self.info.ring_ratio

    def phase_name(self) -> str:
        if not self.is_moon:
            if self.illum > 0.96:
                return _("full disc")
            if self.illum > 0.58:
                return _("gibbous")
            if self.illum > 0.42:
                return _("half")
            return _("crescent")
        if self.illum < 0.02:
            return _("new")
        if self.illum > 0.98:
            return _("full")
        if 0.46 <= self.illum <= 0.54:
            return _("first quarter") if self.waxing else _("last quarter")
        if self.illum < 0.5:
            return _("waxing crescent") if self.waxing else _("waning crescent")
        return _("waxing gibbous") if self.waxing else _("waning gibbous")

    def frame_fraction(self, fov_arcmin: tuple[float, float]) -> float:
        short = min(fov_arcmin)
        return self.extent_arcmin / short if short > 0 else float("inf")

    def disc_px(self, arcsec_per_px: float) -> float:
        return self.diameter_arcmin * 60.0 / arcsec_per_px if arcsec_per_px > 0 else 0.0

    def summary(self) -> str:
        where = (
            _("{alt:.0f}° up, az {az:.0f}°").format(alt=self.alt, az=self.az)
            if self.up
            else _("below the horizon")
        )
        size = (
            _("{arcmin:.1f}'").format(arcmin=self.diameter_arcmin)
            if self.diameter_arcmin >= 1.0
            else _('{arcsec:.1f}"').format(arcsec=self.diameter_arcmin * 60)
        )
        return _("{phase}, {pct:.0f}% lit · {size} · {where}").format(
            phase=self.phase_name(), pct=self.illum * 100, size=size, where=where
        )

    def as_target(self) -> Obj:
        return Obj(
            name=self.name,
            kind="Moon" if self.is_moon else "Planet",
            ra=self.ra,
            dec=self.dec,
            major_arcmin=self.extent_arcmin,
            minor_arcmin=self.extent_arcmin,
            mag=self.info.mag,
            messier="",
            common="",
        )


def body_at(
    body: str,
    latitude: float,
    longitude: float,
    when: datetime | None = None,
    elevation_m: float = 0.0,
) -> BodyState:
    from astropy.coordinates import get_body
    from astropy.time import Time

    if body not in BODIES:
        raise KeyError(body)
    t = Time(when) if when is not None else Time.now()
    site = _site(latitude, longitude, elevation_m)
    stamp = t.to_datetime() if when is None else when
    return _state(
        body, t, site, get_body("sun", t, location=site), stamp, lookahead=True
    )


def bodies_at(
    latitude: float,
    longitude: float,
    when: datetime | None = None,
    elevation_m: float = 0.0,
) -> list[BodyState]:
    from astropy.coordinates import get_body
    from astropy.time import Time

    t = Time(when) if when is not None else Time.now()
    site = _site(latitude, longitude, elevation_m)
    sun = get_body("sun", t, location=site)
    stamp = t.to_datetime() if when is None else when
    return [
        _state(key, t, site, sun, stamp, lookahead=(key == "moon")) for key in BODIES
    ]


def _site(latitude: float, longitude: float, elevation_m: float):
    from astropy import units as u
    from astropy.coordinates import EarthLocation

    return EarthLocation(
        lat=latitude * u.deg, lon=longitude * u.deg, height=elevation_m * u.m
    )


def _state(body: str, t, site, sun, when: datetime, lookahead: bool) -> BodyState:
    from astropy import units as u
    from astropy.coordinates import AltAz, get_body

    obj = get_body(body, t, location=site)
    altaz = obj.transform_to(AltAz(obstime=t, location=site))
    illum, dist_km = _phase(sun, obj)

    waxing = False
    if lookahead:
        later = t + 6 * u.hour
        illum_later, _d = _phase(
            get_body("sun", later, location=site), get_body(body, later, location=site)
        )
        waxing = bool(illum_later > illum)

    radius = BODIES[body].radius_km
    diameter = float(np.degrees(2 * np.arcsin(min(radius / dist_km, 1.0))) * 60)
    return BodyState(
        body=body,
        when=when,
        alt=float(altaz.alt.deg),
        az=float(altaz.az.deg),
        ra=float(obj.ra.deg),
        dec=float(obj.dec.deg),
        illum=illum,
        waxing=waxing,
        diameter_arcmin=diameter,
        distance_km=dist_km,
    )


def moon_at(
    latitude: float,
    longitude: float,
    when: datetime | None = None,
    elevation_m: float = 0.0,
) -> BodyState:
    return body_at("moon", latitude, longitude, when, elevation_m)


def _phase(sun, obj) -> tuple[float, float]:
    from astropy import units as u

    delta = float(obj.distance.to(u.km).value)
    r_sun = float(sun.distance.to(u.km).value)
    elong = float(sun.separation(obj).rad)
    r = np.sqrt(max(r_sun**2 + delta**2 - 2 * r_sun * delta * np.cos(elong), 0.0))
    denom = 2 * r * delta
    cos_i = ((r**2 + delta**2 - r_sun**2) / denom) if denom > 0 else -1.0
    return float((1 + np.clip(cos_i, -1.0, 1.0)) / 2), delta


DENSE_BELOW_PX = 96

FILL = 0.5

MIN_SIDE = 16

MARGIN = 1.6


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.w / 2.0, self.y + self.h / 2.0

    def crop(self, a: np.ndarray) -> np.ndarray:
        return a[self.y : self.y + self.h, self.x : self.x + self.w]

    def scaled(self, k: float) -> Box:
        return Box(
            int(self.x * k) & ~1,
            int(self.y * k) & ~1,
            int(self.w * k) & ~1,
            int(self.h * k) & ~1,
        )

    def recentred(self, cx: float, cy: float, shape: tuple[int, int]) -> Box:
        h, w = shape[0], shape[1]
        x = int(round(cx - self.w / 2.0))
        y = int(round(cy - self.h / 2.0))
        return Box(
            int(np.clip(x, 0, max(w - self.w, 0))),
            int(np.clip(y, 0, max(h - self.h, 0))),
            self.w,
            self.h,
        )


def window(
    lum: np.ndarray, side: int | None = None, threshold: float = 0.25, stride: int = 4
) -> Box | None:
    a = np.asarray(lum, dtype=np.float32)
    if a.ndim != 2 or a.size == 0:
        return None
    stride = max(1, min(stride, min(a.shape[0], a.shape[1]) // 128))
    s = a[::stride, ::stride]
    peak = float(s.max())
    sky = float(np.median(s))
    if peak <= sky:
        return None
    mask = s >= sky + threshold * (peak - sky)
    labels, _ = ndi.label(mask)
    py, px = np.unravel_index(np.argmax(s), s.shape)
    ys, xs = np.nonzero(labels == labels[py, px])
    if xs.size < 4:
        return None
    x0, x1 = np.percentile(xs, (1.0, 99.0))
    y0, y1 = np.percentile(ys, (1.0, 99.0))
    spans = (x1 - x0) > 0.8 * s.shape[1] and (y1 - y0) > 0.8 * s.shape[0]
    if spans and xs.size < FILL * max((x1 - x0 + 1) * (y1 - y0 + 1), 1.0):
        return None
    x0, x1, y0, y1 = x0 * stride, x1 * stride, y0 * stride, y1 * stride
    if side is None:
        size = max(int(round(max(x1 - x0, y1 - y0) * MARGIN)), MIN_SIDE)
    else:
        size = int(side)
    size = int(min(size, a.shape[0], a.shape[1]))
    return Box(0, 0, size, size).recentred((x0 + x1) / 2.0, (y0 + y1) / 2.0, a.shape)


def centroid(
    lum: np.ndarray, box: Box | None = None, threshold: float = 0.25, stride: int = 1
) -> tuple[float, float]:
    a = np.asarray(lum, dtype=np.float32)
    crop = box.crop(a) if box is not None else a
    if crop.size == 0:
        return (a.shape[1] / 2.0, a.shape[0] / 2.0)
    step = max(int(stride), 1)
    s = crop[::step, ::step]
    sky = float(np.median(s))
    lit = np.clip(s - (sky + threshold * (float(s.max()) - sky)), 0.0, None)
    if lit.any():
        labels, _ = ndi.label(lit > 0)
        py, px = np.unravel_index(np.argmax(s), s.shape)
        lit = np.where(labels == labels[py, px], lit, 0.0)
    total = float(lit.sum())
    if total <= 0.0:
        cx, cy = crop.shape[1] / 2.0, crop.shape[0] / 2.0
    else:
        ys, xs = np.indices(s.shape, dtype=np.float32)
        cx = float((lit * xs).sum() / total) * step
        cy = float((lit * ys).sum() / total) * step
    return (cx + (box.x if box else 0), cy + (box.y if box else 0))


def tracking_stride(box: Box) -> int:
    return max(1, min(box.w, box.h) // 128)


@dataclass
class Levels:
    peak: float
    clipped: float
    lit: float

    @property
    def factor(self) -> float:
        return HEADROOM / max(self.peak, 1e-4)

    def verdict(self) -> str:
        if self.lit < 0.005:
            return _("nothing bright in the frame")
        if self.clipped > 0.001:
            return _(
                "CLIPPING {pct:.2f}% — shorten the exposure to {factor:.2f}x"
            ).format(pct=self.clipped * 100, factor=self.factor)
        if self.peak < HEADROOM * 0.5:
            return _(
                "under-exposed at {pct:.0f}% — {factor:.1f}x would fill the range"
            ).format(pct=self.peak * 100, factor=self.factor)
        return _("peak at {pct:.0f}% of scale — good").format(pct=self.peak * 100)


def levels(
    frame: np.ndarray, box: Box | None = None, sky_floor: float = 0.06
) -> Levels:
    a = np.asarray(frame, dtype=np.float32)
    if box is not None:
        a = box.crop(a)
    step = 3 if min(a.shape[0], a.shape[1]) > DENSE_BELOW_PX else 1
    s = a[::step, ::step]
    if s.size == 0:
        return Levels(peak=0.0, clipped=0.0, lit=0.0)
    return Levels(
        peak=float(s.max()),
        clipped=float(np.count_nonzero(s >= 0.999) / s.size),
        lit=float(np.count_nonzero(s >= sky_floor) / s.size),
    )
