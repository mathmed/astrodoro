"""What is worth photographing right now, ranked.

The question this answers is the first one of the night and the one no
catalogue answers on its own: not "where is M8" but "of everything catalogued,
which objects are high enough, far enough from the Moon, and the right size for
this telescope, *at this hour*".

The ranking is a product of five factors, each in 0..1 and each kept on the
`Suggestion` so the interface can show why an object scored what it scored —
the same rule the frame acceptance follows: a verdict nobody can audit is a
verdict nobody trusts.

    altitude x window x moon x size x surface brightness x fame

The astronomy here is deliberately the simple kind. Positions come from the
hour angle formula against the local sidereal time rather than from a full
`AltAz` transform per object: the catalogue has ~14 thousand entries, the hour
slider recomputes the whole list on every change, and precession from J2000 is
~0.3 deg at this epoch — a third of a degree does not reorder a list whose
altitude factor changes by 1% per degree. The same formula gives the transit
and the remaining window for free, which a per-instant transform does not.

Astropy is still used for the three things that are not simple: sidereal time,
the Sun and the Moon.

The Moon and the planets are ranked into the same list, from the ephemeris
`core/lucky.py` computes rather than from a catalogue — no catalogue can hold a
target that moves. They are scored on the same six factors, three of which mean
something different for a body; `_rank_bodies` says which and why.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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

#: Sidereal rate: the sky turns 360.9856 deg per 24 h of civil time.
SIDEREAL_DEG_PER_HOUR = 15.04107

#: Atmospheric extinction at the zenith, in magnitudes per airmass. 0.25 is the
#: usual value for a decent low-altitude site; it is what makes an object at 30
#: deg worth ~0.8 of the same object overhead.
EXTINCTION_MAG = 0.25

#: Above this the Dobsonian on the platform is awkward and the azimuth becomes
#: unstable — `pointing/pushto.py` already warns about it, and a target that is
#: hard to push to is a target worth suggesting less.
ZENITH_DEG = 80.0

#: How long a useful EAA run on one object takes. An object that sets before
#: this is a target you will not finish.
FULL_SESSION_MIN = 45.0

#: Object families offered as a filter, and the OpenNGC types behind each. The
#: labels are translated at display time, so the source strings live here.
FAMILIES: dict[str, tuple[str, set[str] | None]] = {
    "all": (N_("everything"), None),
    "solar": (N_("Moon and planets"), set()),
    "galaxy": (N_("galaxies"), {"G", "GPair", "GTrpl", "GGroup"}),
    "nebula": (N_("nebulae"), {"Neb", "HII", "EmN", "RfN", "Cl+N", "SNR"}),
    "planetary": (N_("planetary nebulae"), {"PN"}),
    "globular": (N_("globular clusters"), {"GCl"}),
    "open": (N_("open clusters"), {"OCl"}),
}

#: The family that holds nothing from the catalogue: the bodies come from an
#: ephemeris, so "everything" and this one are the two that include them.
SOLAR = "solar"

#: How many pixels across a disc has to be before the image scale is doing it
#: justice. Nothing about a planet is a framing question — Jupiter covers two
#: hundredths of a percent of a bin1 frame — so what stands in for the size
#: factor is whether the optics resolve it at all: Neptune is 1.5 px at
#: 1.53"/px and no session saves that, Mars at opposition is 16, Jupiter 29.
BODY_PX = ([0.0, 4.0, 15.0, 60.0], [0.10, 0.35, 0.85, 1.00])

#: What an object with no name of its own keeps. A catalogue does not record
#: which objects are worth a night, and the closest thing to that fact in the
#: data is whether anyone ever bothered to name it: without this factor the
#: first fifteen suggestions on a real evening were fifteen anonymous open
#: clusters (NGC 6743, NGC 6755, NGC 6664…), scoring high because they are
#: small, bright and moon-proof, and none of them is why anyone goes outside.
FAME_MESSIER = 1.0
FAME_NAMED = 0.92
FAME_ANONYMOUS = 0.7

#: How much moonlight costs each family. Moonlight raises the sky background,
#: which destroys low-contrast extended objects and barely touches point
#: sources: a globular is still a globular under a gibbous Moon, a face-on
#: galaxy is not.
MOON_SENSITIVITY: dict[str, float] = {
    "G": 1.0, "GPair": 1.0, "GTrpl": 1.0, "GGroup": 1.0,
    "Neb": 0.9, "HII": 0.9, "EmN": 0.85, "RfN": 1.0, "Cl+N": 0.8, "SNR": 1.0,
    "PN": 0.5, "GCl": 0.35, "OCl": 0.25,
}


@dataclass
class Sky:
    """The state of the sky at one instant: what constrains every target."""
    when: datetime
    lst_deg: float
    sun_alt: float
    moon_alt: float
    moon_ra: float
    moon_dec: float
    #: Illuminated fraction of the Moon's disc, 0 at new and 1 at full.
    moon_illum: float

    @property
    def dark(self) -> bool:
        """True once the Sun is below the astronomical twilight limit."""
        return self.sun_alt <= -18.0

    @property
    def moon_up(self) -> bool:
        return self.moon_alt > 0.0

    def twilight_text(self) -> str:
        """How much light is still in the sky, or `""` once it is properly dark.

        The dark case says nothing on purpose: it is the normal state of an
        observing night, and a line reading "the sky is dark" from dusk to dawn
        is a line nobody reads — what keeps limiting targets after −18° is the
        Moon, which has its own text. Callers must cope with the empty string.
        """
        if self.sun_alt > 0:
            return _("the Sun is up ({alt:.0f}°) — nothing to image yet").format(
                alt=self.sun_alt)
        if self.sun_alt > -6:
            return _("civil twilight ({alt:.0f}°) — only the brightest "
                     "objects").format(alt=self.sun_alt)
        if self.sun_alt > -12:
            return _("nautical twilight ({alt:.0f}°) — the sky is still "
                     "bright").format(alt=self.sun_alt)
        if self.sun_alt > -18:
            return _("astronomical twilight ({alt:.0f}°) — nearly dark").format(
                alt=self.sun_alt)
        return ""

    def moon_text(self) -> str:
        pct = self.moon_illum * 100
        if not self.moon_up:
            return _("Moon below the horizon ({pct:.0f}% lit)").format(pct=pct)
        return _("Moon {alt:.0f}° up, {pct:.0f}% lit").format(
            alt=self.moon_alt, pct=pct)


@dataclass
class Suggestion:
    """One catalogue object, scored, with every factor kept for display."""
    obj: Obj
    score: float                  # 0..100
    alt: float                    # degrees
    az: float                     # degrees, from north through east
    #: Minutes until it crosses the meridian; negative means it already has.
    minutes_to_transit: float
    #: Minutes left above the requested minimum altitude (inf if circumpolar).
    minutes_left: float
    moon_sep: float               # degrees
    #: Fraction of the shorter side of the frame the object covers.
    field_fraction: float
    #: Surface brightness in mag/arcsec², NaN for objects with no measured size.
    surface_brightness: float
    factors: dict[str, float] = field(default_factory=dict)
    #: `lucky.BODIES` key when this row is the Moon or a planet, "" otherwise.
    #: The interface routes on it: a body is pointed at through its ephemeris,
    #: which keeps following it, and has no survey picture to preview.
    body: str = ""

    @property
    def rising(self) -> bool:
        return self.minutes_to_transit > 0

    def reasons(self) -> list[str]:
        """Short sentences explaining the score, best news first."""
        out = [_("{alt:.0f}° up, {dir}").format(alt=self.alt,
                                                dir=compass_point(self.az))]
        if np.isfinite(self.minutes_to_transit):
            if abs(self.minutes_to_transit) < 20:
                out.append(_("crossing the meridian now"))
            elif self.rising:
                out.append(_("culminates in {min:.0f} min").format(
                    min=self.minutes_to_transit))
            else:
                out.append(_("past the meridian {min:.0f} min ago").format(
                    min=-self.minutes_to_transit))
        if np.isinf(self.minutes_left):
            out.append(_("stays up all night"))
        elif self.minutes_left < FULL_SESSION_MIN:
            out.append(_("only {min:.0f} min left before it gets too low"
                         ).format(min=self.minutes_left))
        if np.isfinite(self.moon_sep):
            out.append(_("{deg:.0f}° from the Moon").format(deg=self.moon_sep))
        if np.isfinite(self.field_fraction) and self.field_fraction > 0:
            if self.field_fraction > 1.0:
                out.append(_("larger than the frame"))
            else:
                out.append(_("fills {pct:.0f}% of the frame").format(
                    pct=self.field_fraction * 100))
        if np.isfinite(self.surface_brightness):
            out.append(_("surface brightness {sb:.1f} mag/arcsec²").format(
                sb=self.surface_brightness))
        return out


# ------------------------------------------------------------------- the sky
def lst_at(longitude: float, when=None) -> float:
    """Apparent local sidereal time in degrees.

    On its own because it is the cheap half of `sky_at`: what anchors a
    position to the sky at an instant, without the Sun and Moon ephemerides.
    """
    from astropy import units as u
    from astropy.time import Time

    t = Time(when) if when is not None else Time.now()
    return float(t.sidereal_time("apparent", longitude=longitude * u.deg).deg)


def sky_at(latitude: float, longitude: float, when: datetime | None = None,
           elevation_m: float = 0.0) -> Sky:
    """Sidereal time, Sun and Moon for one instant at one site."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_body
    from astropy.time import Time

    t = Time(when) if when is not None else Time.now()
    site = EarthLocation(lat=latitude * u.deg, lon=longitude * u.deg,
                         height=elevation_m * u.m)
    frame = AltAz(obstime=t, location=site)
    lst = lst_at(longitude, t)

    sun = get_body("sun", t, location=site)
    moon = get_body("moon", t, location=site)
    # Illuminated fraction from the Sun-Moon elongation as seen from here. Good
    # to about 1% — the phase is used to weigh moonlight, not to draw a disc.
    elong = sun.separation(moon).rad
    illum = float((1 - np.cos(elong)) / 2)

    return Sky(
        when=(t.to_datetime() if when is None else when),
        lst_deg=lst,
        sun_alt=float(sun.transform_to(frame).alt.deg),
        moon_alt=float(moon.transform_to(frame).alt.deg),
        moon_ra=float(moon.ra.deg), moon_dec=float(moon.dec.deg),
        moon_illum=illum,
    )


# ---------------------------------------------------------------- the ranking
def rank(objs: list[Obj], sky: Sky, latitude: float,
         fov_arcmin: tuple[float, float] = (55.0, 37.0),
         min_alt: float = 25.0, max_mag: float = 12.0,
         family: str = "all", fits_only: bool = False,
         limit: int = 60, bodies: Sequence = (),
         arcsec_per_px: float = 0.0) -> list[Suggestion]:
    """Score and order `objs` for the given instant, best first.

    `bodies` are `lucky.BodyState`s, ranked against the catalogue on the same
    six factors and sorted into the same list. They arrive already computed
    because they cost an ephemeris call each and this is called on every filter
    change, while the sky they are read from moves once a minute.
    """
    out = ([] if family == SOLAR
           else _rank_catalogue(objs, sky, latitude, fov_arcmin, min_alt,
                                max_mag, family, fits_only, limit))
    if family in ("all", SOLAR):
        out += _rank_bodies(bodies, sky, latitude, fov_arcmin, min_alt,
                            max_mag, fits_only, arcsec_per_px)
    out.sort(key=lambda s: -s.score)
    return out[:limit]


def _rank_catalogue(objs: list[Obj], sky: Sky, latitude: float,
                    fov_arcmin: tuple[float, float], min_alt: float,
                    max_mag: float, family: str, fits_only: bool,
                    limit: int) -> list[Suggestion]:
    """Everything is vectorised over the whole catalogue: the interface
    recomputes this on every filter change and on every drag of the hour, and
    one pass over 14 thousand rows in numpy is a few milliseconds.
    """
    if not objs:
        return []

    ra = np.array([o.ra for o in objs])
    dec = np.array([o.dec for o in objs])
    mag = np.array([o.mag for o in objs])
    major = np.array([o.major_arcmin for o in objs])
    minor = np.array([o.minor_arcmin for o in objs])
    kinds = [o.kind for o in objs]

    keep = np.ones(len(objs), dtype=bool)
    allowed = FAMILIES.get(family, FAMILIES["all"])[1]
    if allowed is not None:
        keep &= np.array([k in allowed for k in kinds])
    # An object with no measured magnitude is not necessarily faint, but it
    # cannot be ranked against one that has: it would sort by the default and
    # crowd out objects that were actually measured.
    keep &= np.isfinite(mag) & (mag <= max_mag)
    if not keep.any():
        return []

    alt, az, ha = horizon(ra, dec, latitude, sky.lst_deg)
    keep &= alt >= min_alt
    if not keep.any():
        return []

    idx = np.flatnonzero(keep)
    alt, az, ha = alt[idx], az[idx], ha[idx]
    ra, dec, mag = ra[idx], dec[idx], mag[idx]
    major, minor = major[idx], minor[idx]
    kinds = [kinds[i] for i in idx]

    # --- the five factors ---------------------------------------------------
    airmass = 1.0 / np.maximum(np.sin(np.radians(alt)), 0.05)
    f_alt = 10 ** (-0.4 * EXTINCTION_MAG * (airmass - 1.0))
    f_alt = np.where(alt > ZENITH_DEG, f_alt * 0.7, f_alt)

    to_transit = -ha / SIDEREAL_DEG_PER_HOUR * 60.0
    left = _minutes_left(dec, latitude, ha, min_alt)
    f_window = np.clip(left / FULL_SESSION_MIN, 0.0, 1.0)

    sep = _separation(ra, dec, sky.moon_ra, sky.moon_dec)
    f_moon = _moon_factor(sep, kinds, sky)

    short_side = min(fov_arcmin)
    frac = major / short_side
    f_size = _size_factor(frac)

    sb = _surface_brightness(mag, major, minor)
    # 20 mag/arcsec² and brighter needs no help; 24.5 is where a 5 s sub from a
    # suburban garden stops showing anything at all. M31 sits at 22.2 and lands
    # mid-scale, which is exactly how it behaves in EAA — bright object, faint
    # picture.
    f_bright = np.where(
        np.isfinite(sb),
        np.clip((24.5 - sb) / 4.5, 0.05, 1.0),
        # No measured size: judge by magnitude alone. Clusters and stellar
        # objects land here, and for them the integrated magnitude is the
        # honest criterion anyway.
        np.clip((13.0 - mag) / 5.0, 0.05, 1.0))

    f_fame = np.array([
        FAME_MESSIER if objs[i].messier
        else (FAME_NAMED if objs[i].common else FAME_ANONYMOUS)
        for i in idx])

    score = 100.0 * f_alt * f_window * f_moon * f_size * f_bright * f_fame

    if fits_only:
        score = np.where(frac <= 1.0, score, 0.0)

    order = np.argsort(-score)
    out: list[Suggestion] = []
    for j in order:
        if score[j] <= 0:
            break
        out.append(Suggestion(
            obj=objs[idx[j]], score=float(score[j]),
            alt=float(alt[j]), az=float(az[j]),
            minutes_to_transit=float(to_transit[j]),
            minutes_left=float(left[j]),
            moon_sep=float(sep[j]),
            field_fraction=float(frac[j]) if np.isfinite(frac[j]) else 0.0,
            surface_brightness=float(sb[j]),
            factors={"altitude": float(f_alt[j]), "window": float(f_window[j]),
                     "moon": float(f_moon[j]), "size": float(f_size[j]),
                     "brightness": float(f_bright[j]),
                     "fame": float(f_fame[j])},
        ))
        if len(out) >= limit:
            break
    return out


def _rank_bodies(bodies: Sequence, sky: Sky, latitude: float,
                 fov_arcmin: tuple[float, float], min_alt: float,
                 max_mag: float, fits_only: bool,
                 arcsec_per_px: float) -> list[Suggestion]:
    """The Moon and the planets, on the same six factors as the catalogue.

    Three of the six are the same measurement and mean the same thing: how high
    it is, how long it stays, and how bright a square arcsecond of it is — the
    published surface magnitude walks straight into the catalogue's formula and
    comes out saturated, which is the honest answer for a body that is fifteen
    magnitudes above the sky.

    The other three do not survive the crossing. Moonlight does not touch a
    body of this brightness, and the Moon does not shine on itself; every one
    of them is a target somebody drove out for, so there is no anonymity to
    discount; and size is not about the frame — it is `BODY_PX`, whether the
    image scale resolves the disc at all.
    """
    out: list[Suggestion] = []
    for st in bodies:
        if st.alt < min_alt or not np.isfinite(st.info.mag):
            continue
        if st.info.mag > max_mag:
            continue
        _alt, _az, ha = horizon(np.array([st.ra]), np.array([st.dec]),
                                latitude, sky.lst_deg)
        left = _minutes_left(np.array([st.dec]), latitude, ha, min_alt)
        airmass = 1.0 / max(np.sin(np.radians(st.alt)), 0.05)
        f_alt = 10 ** (-0.4 * EXTINCTION_MAG * (airmass - 1.0))
        if st.alt > ZENITH_DEG:
            f_alt *= 0.7
        f_window = float(np.clip(left[0] / FULL_SESSION_MIN, 0.0, 1.0))
        px = (st.extent_arcmin * 60.0 / arcsec_per_px
              if arcsec_per_px > 0 else float("inf"))
        f_size = (1.0 if not np.isfinite(px)
                  else float(np.interp(px, *BODY_PX)))
        sb = st.info.surface_mag
        f_bright = float(np.clip((24.5 - sb) / 4.5, 0.05, 1.0))
        frac = st.frame_fraction(fov_arcmin)
        score = 100.0 * f_alt * f_window * f_size * f_bright
        if fits_only and frac > 1.0:
            continue
        if score <= 0:
            continue
        out.append(Suggestion(
            obj=st.as_target(), score=score, alt=st.alt, az=st.az,
            minutes_to_transit=float(-ha[0] / SIDEREAL_DEG_PER_HOUR * 60.0),
            minutes_left=float(left[0]),
            # Not 0° for the Moon on itself, and not a warning for a planet:
            # the number would be true and the factor it stands for is 1.
            moon_sep=np.nan,
            field_fraction=frac, surface_brightness=sb,
            factors={"altitude": float(f_alt), "window": f_window,
                     "moon": 1.0, "size": f_size, "brightness": f_bright,
                     "fame": 1.0},
            body=st.body))
    return out


# ------------------------------------------------------------------ the maths
def horizon(ra: np.ndarray, dec: np.ndarray, lat: float,
             lst_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Altitude, azimuth and hour angle, all in degrees.

    The hour angle is returned wrapped to (-180, 180]: negative means the object
    has yet to cross the meridian, which is what the ranking calls "rising".
    """
    ha = np.radians(((lst_deg - ra + 180.0) % 360.0) - 180.0)
    d, p = np.radians(dec), np.radians(lat)
    sin_alt = np.sin(d) * np.sin(p) + np.cos(d) * np.cos(p) * np.cos(ha)
    alt = np.degrees(np.arcsin(np.clip(sin_alt, -1, 1)))
    az = np.degrees(np.arctan2(
        -np.sin(ha) * np.cos(d),
        np.sin(d) * np.cos(p) - np.cos(d) * np.sin(p) * np.cos(ha))) % 360.0
    return alt, az, np.degrees(ha)


def _minutes_left(dec: np.ndarray, lat: float, ha_deg: np.ndarray,
                  min_alt: float) -> np.ndarray:
    """Minutes until the object drops below `min_alt`, inf if it never does."""
    d, p, h = np.radians(dec), np.radians(lat), np.radians(min_alt)
    denom = np.cos(d) * np.cos(p)
    with np.errstate(divide="ignore", invalid="ignore"):
        cos_ha = (np.sin(h) - np.sin(d) * np.sin(p)) / denom
    # |cos| > 1 means the circle never crosses that altitude: either always
    # above it (circumpolar at this limit) or always below.
    always_up = (cos_ha < -1) | ((np.abs(denom) < 1e-12) & (dec * lat > 0))
    ha_set = np.degrees(np.arccos(np.clip(cos_ha, -1, 1)))
    minutes = (ha_set - ha_deg) / SIDEREAL_DEG_PER_HOUR * 60.0
    minutes = np.where(minutes < 0, 0.0, minutes)
    return np.where(always_up, np.inf, minutes)


def _separation(ra: np.ndarray, dec: np.ndarray, ra2: float,
                dec2: float) -> np.ndarray:
    a, d = np.radians(ra), np.radians(dec)
    b, e = np.radians(ra2), np.radians(dec2)
    cos = (np.sin(d) * np.sin(e)
           + np.cos(d) * np.cos(e) * np.cos(a - b))
    return np.degrees(np.arccos(np.clip(cos, -1, 1)))


def _moon_factor(sep: np.ndarray, kinds: list[str], sky: Sky) -> np.ndarray:
    """How much of the object survives the moonlight.

    Three things multiply: how lit the Moon is, how close it is, and how high —
    a Moon on the horizon scatters much less into the field, and one below it
    costs nothing at all.
    """
    if not sky.moon_up or sky.moon_illum < 0.02:
        return np.ones(len(sep))
    sens = np.array([MOON_SENSITIVITY.get(k, 0.8) for k in kinds])
    # Beyond 100 deg the glare is no longer what limits the frame.
    near = np.clip(1.0 - sep / 100.0, 0.0, 1.0)
    high = float(np.clip(np.sin(np.radians(sky.moon_alt)) * 1.5, 0.15, 1.0))
    return np.clip(1.0 - sens * sky.moon_illum * near * high, 0.05, 1.0)


def _size_factor(frac: np.ndarray) -> np.ndarray:
    """How well the object suits the frame, `frac` being its size over the
    frame's shorter side.

    The comfortable band is 15%..70%: below it the object is a handful of pixels
    and no amount of integration makes it a picture, above it the frame starts
    cutting it. Larger than the frame is not worthless — a piece of the Veil is
    still worth imaging — so it keeps 0.4 rather than being dropped.
    """
    f = np.full(len(frac), 0.8)          # no measured size: neutral
    ok = np.isfinite(frac) & (frac > 0)
    x = frac[ok]
    v = np.interp(x, [0.0, 0.03, 0.15, 0.70, 1.00, 2.00],
                  [0.25, 0.45, 1.00, 1.00, 0.70, 0.40])
    f[ok] = np.where(x > 2.0, 0.4, v)
    return f


def _surface_brightness(mag: np.ndarray, major: np.ndarray,
                        minor: np.ndarray) -> np.ndarray:
    """Mean surface brightness in mag/arcsec², NaN when there is no size.

    This, not the integrated magnitude, is what decides whether an object shows
    up in a short sub: M31 is magnitude 3.4 and still a faint smudge because
    that light is spread over half a degree.
    """
    b = np.where(np.isfinite(minor) & (minor > 0), minor, major)
    with np.errstate(divide="ignore", invalid="ignore"):
        area = np.pi / 4.0 * major * b * 3600.0        # arcsec²
        sb = mag + 2.5 * np.log10(area)
    return np.where(np.isfinite(area) & (area > 0), sb, np.nan)


#: The 16 compass points. Translated because they differ by language. They live
#: here rather than in the interface because the ranking itself needs them: a
#: suggestion's reason is "58° up, SE", and that sentence is built next to the
#: azimuth that produced it.
COMPASS = (N_("N"), N_("NNE"), N_("NE"), N_("ENE"),
           N_("E"), N_("ESE"), N_("SE"), N_("SSE"),
           N_("S"), N_("SSW"), N_("SW"), N_("WSW"),
           N_("W"), N_("WNW"), N_("NW"), N_("NNW"))


def compass_point(az_deg: float) -> str:
    """Azimuth as a compass point. In the dark, "SE" arrives before "134°"."""
    return _(COMPASS[int((az_deg % 360.0) / 22.5 + 0.5) % 16])
