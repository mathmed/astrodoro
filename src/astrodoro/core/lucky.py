"""The bright bodies: where they are, how big they are, and whether the
exposure is burning them.

The Moon and the planets break every assumption the rest of the pipeline is
built on. They are bright enough that the exposure is milliseconds instead of
seconds, so the enemy is saturation and not noise; there are no stars in the
frame to register on, so there is nothing to align and nothing to stack live;
and the limit on detail is the atmosphere, which means lucky imaging — a few
hundred short frames of which the sharpest percent are stacked later.

So this module deliberately provides no pipeline. It provides the numbers that
actually decide such a session: where the body is (they are the targets no
catalogue contains), how much of the frame it fills, where in the frame it sits,
and whether the current exposure is clipping it.

The Sun is not in `BODIES` and must not be added: nothing in this program can
know whether a filter is on the tube, and the one mistake here destroys the
sensor and the eye behind it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np

from ..i18n import N_
from ..i18n import gettext as _
from .catalog import Obj

try:                                    # avoid a download attempt in the field
    from astropy.utils import iers
    iers.conf.auto_download = False
except Exception:
    pass


@dataclass(frozen=True)
class Body:
    """One body, and the two numbers a session needs before pointing.

    `surface_mag` is the mean visual magnitude of a square arcsecond of the
    disc. It is what sets the exposure: a body is not brighter to expose for
    because it is bigger, it is brighter because each patch of it is. The values
    are the usual published means, good to a few tenths — which is a third of a
    stop, and the exposure guard closes that gap in one press.
    """

    key: str
    name: str                    # deferred translation, see `label`
    radius_km: float             # equatorial, IAU 2015
    surface_mag: float           # mean V magnitude per square arcsecond
    mag: float                   # nominal integrated magnitude, near opposition
    #: Outer edge of the ring system over the equatorial radius. Saturn's rings
    #: are what has to fit in the frame, not its disc.
    ring_ratio: float = 1.0

    @property
    def label(self) -> str:
        return _(self.name)


#: The Moon first: it is the reference the others' exposures are scaled from,
#: and the one anybody starts the night with.
BODIES: dict[str, Body] = {
    b.key: b for b in (
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

#: Where the disc should peak, as a fraction of full scale. Not 1.0: the
#: brightest crater floors near the terminator — and the poles of Jupiter's
#: belts — are what saturates first, and a clipped highlight cannot be recovered
#: by any amount of stacking afterwards.
HEADROOM = 0.85

#: Longest exposure that still freezes the seeing. The atmosphere rearranges
#: itself in tens of milliseconds; a longer frame averages two atmospheres
#: together, and then there is no sharp frame in the burst for lucky imaging to
#: pick. Past this, more signal has to come from gain, not from time.
FREEZE_S = 0.02


def exposure_for(body: str, moon_exposure_s: float) -> tuple[float, float]:
    """(exposure, how much signal is still missing) for `body`.

    Scaled from the Moon's exposure by the ratio of surface brightnesses, which
    is the only part of it that transfers: same optics, same sensor, same
    headroom, a different number of photons per arcsecond of disc.

    The second number is what the cap costs. Saturn wants 25x the Moon's
    milliseconds and cannot have them — at 200 ms the seeing is averaged, not
    frozen — so the exposure stops at `FREEZE_S` and the rest is a factor the
    gain has to make up. 1.0 means nothing was left over.
    """
    b = BODIES[body]
    want = float(moon_exposure_s) * 10 ** (0.4 * (b.surface_mag -
                                                  MOON.surface_mag))
    exposure = min(want, FREEZE_S)
    return exposure, (want / exposure if exposure > 0 else 1.0)


@dataclass
class BodyState:
    """One body at one instant, from one site."""

    body: str
    when: datetime
    alt: float
    az: float
    ra: float
    dec: float
    #: Illuminated fraction of the disc, 0 at new and 1 at full.
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
        """What has to fit in the frame — the rings, where there are rings."""
        return self.diameter_arcmin * self.info.ring_ratio

    def phase_name(self) -> str:
        """The lunar phases for the Moon, the shape of the disc for the rest.

        A planet has no "first quarter": the names are the Moon's calendar, and
        Mars at 88% is simply gibbous. Which way the phase is going is only
        reported where it is quick to know and worth knowing.
        """
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
        """Extent over the *shorter* side of the frame.

        The short side is what decides whether the whole body fits, and for the
        Moon at 1200 mm on a 4/3" sensor it does not: the answer is a mosaic,
        and knowing that before pointing saves a session.
        """
        short = min(fov_arcmin)
        return self.extent_arcmin / short if short > 0 else float("inf")

    def disc_px(self, arcsec_per_px: float) -> float:
        """How many pixels across the disc is. The planetary question.

        Nothing about a planet is a framing problem — Jupiter covers two
        hundredths of a percent of a bin1 frame. What decides the session is
        whether the image scale resolves it at all, and that is this number.
        """
        return (self.diameter_arcmin * 60.0 / arcsec_per_px
                if arcsec_per_px > 0 else 0.0)

    def summary(self) -> str:
        where = (_("{alt:.0f}° up, az {az:.0f}°").format(alt=self.alt,
                                                         az=self.az)
                 if self.up else _("below the horizon"))
        size = (_("{arcmin:.1f}'").format(arcmin=self.diameter_arcmin)
                if self.diameter_arcmin >= 1.0 else
                _('{arcsec:.1f}"').format(arcsec=self.diameter_arcmin * 60))
        return _("{phase}, {pct:.0f}% lit · {size} · {where}").format(
            phase=self.phase_name(), pct=self.illum * 100, size=size,
            where=where)

    def as_target(self) -> Obj:
        """The body as a catalogue object, so push-to can aim at it.

        The whole pointing chain — the arrow in FRAME, the mark on the sky map —
        speaks `Obj`. Handing it one costs nothing and is the difference between
        "point at Jupiter" being a button and being a manual RA/Dec entry.
        """
        return Obj(name=self.name, kind="Moon" if self.is_moon else "Planet",
                   ra=self.ra, dec=self.dec,
                   major_arcmin=self.extent_arcmin,
                   minor_arcmin=self.extent_arcmin,
                   mag=self.info.mag, messier="", common="")


def body_at(body: str, latitude: float, longitude: float,
            when: datetime | None = None,
            elevation_m: float = 0.0) -> BodyState:
    """Position, phase and apparent size of one body at one instant.

    Topocentric on purpose, unlike the geocentric ephemeris a phase calculator
    would use: the Moon is close enough that parallax moves it by up to a degree
    — twice its own diameter — and a push-to arrow a degree off is an arrow that
    does not find it. The planets do not care, and are computed the same way for
    one code path instead of two.
    """
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_body
    from astropy.time import Time

    if body not in BODIES:
        raise KeyError(body)
    t = Time(when) if when is not None else Time.now()
    site = EarthLocation(lat=latitude * u.deg, lon=longitude * u.deg,
                         height=elevation_m * u.m)

    sun = get_body("sun", t, location=site)
    obj = get_body(body, t, location=site)
    altaz = obj.transform_to(AltAz(obstime=t, location=site))

    illum, dist_km = _phase(sun, obj)
    # Which way the phase is going, from the phase itself six hours on. The
    # ecliptic-longitude rule the Moon is usually done with does not carry over:
    # for an inner planet, leading the Sun means waning, not waxing.
    later = t + 6 * u.hour
    illum_later, _d = _phase(get_body("sun", later, location=site),
                             get_body(body, later, location=site))

    radius = BODIES[body].radius_km
    diameter = float(np.degrees(2 * np.arcsin(min(radius / dist_km, 1.0))) * 60)

    return BodyState(
        body=body, when=(t.to_datetime() if when is None else when),
        alt=float(altaz.alt.deg), az=float(altaz.az.deg),
        ra=float(obj.ra.deg), dec=float(obj.dec.deg),
        illum=illum, waxing=bool(illum_later > illum),
        diameter_arcmin=diameter, distance_km=dist_km,
    )


def moon_at(latitude: float, longitude: float, when: datetime | None = None,
            elevation_m: float = 0.0) -> BodyState:
    """The Moon, which is what most of the program still asks for by name."""
    return body_at("moon", latitude, longitude, when, elevation_m)


def _phase(sun, obj) -> tuple[float, float]:
    """(illuminated fraction, distance in km) from two observed positions.

    Through the phase angle at the body and not through the elongation seen from
    here. For the Moon the two agree — the Sun is 400 times further away, so the
    phase angle is the supplement of the elongation — but for Venus at inferior
    conjunction the elongation is 8° and the phase angle is 172°, which is a
    crescent read as very nearly full.
    """
    from astropy import units as u

    delta = float(obj.distance.to(u.km).value)      # observer to body
    r_sun = float(sun.distance.to(u.km).value)      # observer to Sun
    elong = float(sun.separation(obj).rad)
    # Law of cosines twice: the Sun-body distance from the triangle we can see,
    # then the angle of that triangle at the body.
    r = np.sqrt(max(r_sun ** 2 + delta ** 2
                    - 2 * r_sun * delta * np.cos(elong), 0.0))
    denom = 2 * r * delta
    cos_i = ((r ** 2 + delta ** 2 - r_sun ** 2) / denom) if denom > 0 else -1.0
    return float((1 + np.clip(cos_i, -1.0, 1.0)) / 2), delta


# --------------------------------------------------------------- the disc
#: Below this many pixels across, the window is sampled whole rather than every
#: third pixel: Mars is eight pixels wide at 1200 mm and a stride would leave
#: nine samples of it.
DENSE_BELOW_PX = 96

#: How much of its own bounding box something spread across the whole frame has
#: to fill before it is taken for a body. A disc fills 0.79 of its box; noise
#: over the same threshold covers every corner of the frame and fills a fifth.
#: Only ever applied to something that already spans the frame — a crescent
#: fills a third of its box and is a body wherever it is small enough to have
#: one.
FILL = 0.5

#: Smallest window, in luminance pixels. Below this the measurements inside it
#: are made of too few samples to mean anything, and the body is under-sampled
#: by the optics anyway.
MIN_SIDE = 16

#: How much bigger than the disc the measuring window is. Enough that the sky
#: around the body is in it — the pedestal and the "is there anything here at
#: all" test both need sky — and not so much that a planet is a rounding error
#: inside its own window.
MARGIN = 1.6


@dataclass(frozen=True)
class Box:
    """A window into a frame, in that frame's own pixels."""

    x: int
    y: int
    w: int
    h: int

    @property
    def centre(self) -> tuple[float, float]:
        return self.x + self.w / 2.0, self.y + self.h / 2.0

    def crop(self, a: np.ndarray) -> np.ndarray:
        return a[self.y:self.y + self.h, self.x:self.x + self.w]

    def scaled(self, k: float) -> Box:
        """The same window in a frame `k` times larger.

        Every origin here stays even, because the frame this maps onto is a
        Bayer mosaic: cropping it at an odd pixel shifts the pattern, and every
        colour in the crop is then the wrong one.
        """
        return Box(int(self.x * k) & ~1, int(self.y * k) & ~1,
                   int(self.w * k) & ~1, int(self.h * k) & ~1)

    def recentred(self, cx: float, cy: float, shape: tuple[int, int]) -> Box:
        """The same size, moved onto (cx, cy) and kept inside `shape`."""
        h, w = shape[0], shape[1]
        x = int(round(cx - self.w / 2.0))
        y = int(round(cy - self.h / 2.0))
        return Box(int(np.clip(x, 0, max(w - self.w, 0))),
                   int(np.clip(y, 0, max(h - self.h, 0))), self.w, self.h)


def window(lum: np.ndarray, side: int | None = None, threshold: float = 0.25,
           stride: int = 4) -> Box | None:
    """Where the body is in the frame, as a box around it. `None` if nothing is.

    Measured on the luminance and not on the mosaic: it is a quarter of the
    pixels, and one luminance pixel is one whole Bayer quad, so there is no
    phase to get wrong. `Box.scaled(2)` takes the answer back to the mosaic.

    Why a window at all: everything this mode measures is a statement about the
    body, and a full frame is mostly sky. `sharpness` divides by the mean level,
    so on a planet covering 0.02% of the frame it reports the contrast of
    the sky noise; `levels` counts the lit fraction, which for the same planet
    reads as an empty frame. Both are right about the frame and useless
    about the target.

    `side` fixes the size across frames. The size is the denominator of every
    measurement made inside it, so letting it breathe with the seeing would make
    the sharpness meter move when nothing about the focus did.
    """
    a = np.asarray(lum, dtype=np.float32)
    if a.ndim != 2 or a.size == 0:
        return None
    # Never so coarse that a small frame is a handful of samples: the stride is
    # there to keep the search cheap on an 11 MP frame, and a body eight pixels
    # across in a 256-pixel one would fall between its teeth.
    stride = max(1, min(stride, min(a.shape[0], a.shape[1]) // 128))
    s = a[::stride, ::stride]
    peak = float(s.max())
    sky = float(np.median(s))
    if peak <= sky:
        return None
    ys, xs = np.nonzero(s >= sky + threshold * (peak - sky))
    if xs.size < 4:
        return None
    # Percentiles and not the bounding box: a hot pixel or a star at the edge of
    # the frame is over the threshold too, and would drag the window off the
    # body it is supposed to be around.
    x0, x1 = np.percentile(xs, (1.0, 99.0))
    y0, y1 = np.percentile(ys, (1.0, 99.0))
    # Is any of this a body? Not a contrast test — an under-exposed planet has
    # little of it — but a shape one, and only where there is a doubt: what is
    # over the threshold on an empty frame is noise, which reaches every corner
    # and fills its own bounding box thinly, where a disc fills three quarters
    # of it. Anything that does not span the frame is a body by construction.
    spans = ((x1 - x0) > 0.8 * s.shape[1] and (y1 - y0) > 0.8 * s.shape[0])
    if spans and xs.size < FILL * max((x1 - x0 + 1) * (y1 - y0 + 1), 1.0):
        return None
    x0, x1, y0, y1 = x0 * stride, x1 * stride, y0 * stride, y1 * stride
    if side is None:
        size = max(int(round(max(x1 - x0, y1 - y0) * MARGIN)), MIN_SIDE)
    else:
        size = int(side)
    size = int(min(size, a.shape[0], a.shape[1]))
    return Box(0, 0, size, size).recentred((x0 + x1) / 2.0, (y0 + y1) / 2.0,
                                           a.shape)


def centroid(lum: np.ndarray, box: Box | None = None,
             threshold: float = 0.25, stride: int = 1) -> tuple[float, float]:
    """Brightness-weighted centre of the body, in `lum`'s pixels.

    Two things ask for it. Offline, it is what aligns a burst of a planet before
    anything sub-pixel happens: the body can wander a long way across the frame
    in half a minute, and a phase correlation only sees a shift it can still fit
    inside the window. Live, it is what the view follows so that a body being
    shaken by the wind stands still on screen.

    `stride` samples the window rather than reading all of it. A centroid is a
    ratio of two sums, so a regular sample of a smooth body moves it by less
    than it moves between two frames — and the live use runs at the display
    rate on a window that, for the Moon, is most of an 11.7 MP frame.
    """
    a = np.asarray(lum, dtype=np.float32)
    crop = box.crop(a) if box is not None else a
    if crop.size == 0:
        return (a.shape[1] / 2.0, a.shape[0] / 2.0)
    step = max(int(stride), 1)
    s = crop[::step, ::step]
    sky = float(np.median(s))
    lit = np.clip(s - (sky + threshold * (float(s.max()) - sky)), 0.0, None)
    total = float(lit.sum())
    if total <= 0.0:
        cx, cy = crop.shape[1] / 2.0, crop.shape[0] / 2.0
    else:
        ys, xs = np.indices(s.shape, dtype=np.float32)
        cx = float((lit * xs).sum() / total) * step
        cy = float((lit * ys).sum() / total) * step
    return (cx + (box.x if box else 0), cy + (box.y if box else 0))


def tracking_stride(box: Box) -> int:
    """Stride that keeps a live centroid to a few tens of thousands of samples.

    The window is the disc: a planet's is a few hundred pixels and is read
    whole, the Moon's is most of the frame and is not. Measured on a full-disc
    window of a bin1 luminance frame (1090 x 1090), reading it whole costs
    37 ms against 1.9 ms sampled, and the two centres differ by 0.23 px — a
    quarter of a pixel, against the tens the wind moves it by.
    """
    return max(1, min(box.w, box.h) // 128)


# ------------------------------------------------------------------- exposure
@dataclass
class Levels:
    """What the current exposure is doing to the disc."""

    #: Brightest of the sampled photosites, 0..1 of full scale.
    peak: float
    #: Fraction of the sampled area already at full scale — irrecoverable.
    clipped: float
    #: Fraction of the sampled area that is disc rather than sky.
    lit: float

    @property
    def factor(self) -> float:
        """Multiply the exposure by this to land the peak at `HEADROOM`.

        Once anything clips, `peak` reads 1.0 and this underestimates how much
        to come down by — the frame no longer records how far over it went. It
        converges in two or three steps, which is the honest behaviour: the
        alternative is inventing the missing signal.
        """
        return HEADROOM / max(self.peak, 1e-4)

    def verdict(self) -> str:
        if self.lit < 0.005:
            return _("nothing bright in the frame")
        if self.clipped > 0.001:
            return _("CLIPPING {pct:.2f}% — shorten the exposure to {factor:.2f}x"
                     ).format(pct=self.clipped * 100, factor=self.factor)
        if self.peak < HEADROOM * 0.5:
            return _("under-exposed at {pct:.0f}% — {factor:.1f}x would fill the "
                     "range").format(pct=self.peak * 100, factor=self.factor)
        return _("peak at {pct:.0f}% of scale — good").format(
            pct=self.peak * 100)


def levels(frame: np.ndarray, box: Box | None = None,
           sky_floor: float = 0.06) -> Levels:
    """Measure clipping and headroom on the calibrated mosaic.

    Measured on the mosaic and not on the debayered image because saturation
    happens per photosite: interpolating three colours together hides the one
    channel that went over.

    `box` is in mosaic coordinates — `window(lum).scaled(2)`. Without one the
    whole frame is measured, which is the right answer only when the body fills
    it.
    """
    a = np.asarray(frame, dtype=np.float32)
    if box is not None:
        a = box.crop(a)
    # Stride 3, and odd on purpose: a stride of 2 or 4 would land on a single
    # Bayer phase and measure one colour, which is the colour that clips last.
    # A small window is taken whole — there is no saving worth nine samples.
    step = 3 if min(a.shape[0], a.shape[1]) > DENSE_BELOW_PX else 1
    s = a[::step, ::step]
    if s.size == 0:
        return Levels(peak=0.0, clipped=0.0, lit=0.0)
    return Levels(
        peak=float(s.max()),
        clipped=float(np.count_nonzero(s >= 0.999) / s.size),
        lit=float(np.count_nonzero(s >= sky_floor) / s.size),
    )
