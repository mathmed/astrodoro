"""The lucky path: ephemeris, the window on the body, exposure guard, contrast
focus and the burst.

Everything here runs with no camera and no network. The ephemeris is checked
against instants whose phase is a matter of record rather than against a
reference implementation — the point is to catch a sign flip or a unit mistake,
not to certify astropy.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from astrodoro.core import lucky
from astrodoro.core.focus import SharpnessMeter, sharpness
from astrodoro.core.recorder import Burst, Recorder, read_fits
from astrodoro.core.source import FrameMeta
from astrodoro.drivers.svbony.sdk import Bayer

SITE = (-6.7003, -36.9436)

#: Instants with a known phase. Full Moon of 25 Jan 2024 at 17:54 UTC, last
#: quarter approaching on 2 Feb, first quarter approaching on 16 Feb.
PHASES = [
    (datetime(2024, 1, 25, 17, 54, tzinfo=UTC), 0.99, 1.0, None),
    (datetime(2024, 2, 2, 0, 0, tzinfo=UTC), 0.50, 0.70, False),
    (datetime(2024, 2, 16, 0, 0, tzinfo=UTC), 0.30, 0.50, True),
]


@pytest.mark.parametrize(("when", "lo", "hi", "waxing"), PHASES)
def test_phase_and_direction(when, lo, hi, waxing):
    m = lucky.moon_at(*SITE, when=when)
    assert lo <= m.illum <= hi
    if waxing is not None:
        assert m.waxing is waxing
        # The direction is the half of the phase the illumination cannot give:
        # the same fraction happens twice a month.
        assert ("waxing" in m.phase_name()) is waxing


@pytest.mark.parametrize("when", [p[0] for p in PHASES])
def test_apparent_size_and_distance(when):
    m = lucky.moon_at(*SITE, when=when)
    assert 355_000 < m.distance_km < 407_000
    assert 29.0 < m.diameter_arcmin < 34.0


def test_frame_fraction_uses_the_short_side():
    m = lucky.moon_at(*SITE, when=PHASES[0][0])
    # A field wider than it is tall: the disc has to be measured against the
    # side that decides whether it fits, which is the short one.
    assert m.frame_fraction((60.0, 30.0)) == pytest.approx(
        m.diameter_arcmin / 30.0)


@pytest.mark.parametrize("body", list(lucky.BODIES))
def test_every_body_is_a_usable_target(body):
    o = lucky.body_at(body, *SITE, when=PHASES[0][0]).as_target()
    assert o.label and o.kind_label
    assert 0.0 <= o.ra < 360.0 and -90.0 <= o.dec <= 90.0


def test_the_sun_is_not_a_body():
    """Not an oversight. Nothing here can know whether there is a filter on the
    tube, and pointing at the Sun without one costs the sensor and the eye."""
    assert "sun" not in lucky.BODIES
    with pytest.raises(KeyError):
        lucky.body_at("sun", *SITE)


# ---------------------------------------------------------------- the planets
#: Venus, whose phase is the whole reason the illumination is computed through
#: the phase angle at the body: (date, illuminated fraction, arcsec across).
#: Greatest eastern elongation in Jan 2025 — the dichotomy, half lit — and
#: approaching superior conjunction a year later, where it is nearly full and
#: at its smallest. Reading the elongation from here as if it were the Moon's
#: gives 16% for the first and 0% for the second.
VENUS = [
    (datetime(2025, 1, 10, tzinfo=UTC), 0.45, 0.58, 20.0, 30.0),
    (datetime(2026, 1, 1, tzinfo=UTC), 0.95, 1.0, 9.0, 12.0),
]


@pytest.mark.parametrize(("when", "lo", "hi", "small", "big"), VENUS)
def test_venus_phase_and_size(when, lo, hi, small, big):
    v = lucky.body_at("venus", *SITE, when=when)
    assert lo <= v.illum <= hi
    assert small <= v.diameter_arcmin * 60 <= big
    # Closer means bigger and less lit, which is the one correlation that says
    # the geometry is right rather than the numbers merely plausible.
    assert (v.illum < 0.9) == (v.diameter_arcmin * 60 > 15.0)


def test_outer_planets_are_always_nearly_full():
    for body in ("jupiter", "saturn", "uranus", "neptune"):
        s = lucky.body_at(body, *SITE, when=VENUS[0][0])
        assert s.illum > 0.98
        assert s.phase_name() == "full disc"


def test_saturn_is_framed_by_its_rings():
    s = lucky.body_at("saturn", *SITE, when=VENUS[0][0])
    assert s.extent_arcmin == pytest.approx(s.diameter_arcmin * 2.27)
    assert s.as_target().major_arcmin == pytest.approx(s.extent_arcmin)


def test_disc_px_is_the_planetary_question():
    j = lucky.body_at("jupiter", *SITE, when=VENUS[0][0])
    # 0.8"/px, which is this program's own scale at bin1: a disc of tens of
    # pixels, not a fraction of the frame.
    assert 30 < j.disc_px(0.8) < 70
    # And a framing question that answers itself: 3% of the short side.
    assert j.frame_fraction((60.0, 30.0)) < 0.05


# ---------------------------------------------------------------- the exposure
def test_exposure_scales_with_surface_brightness():
    """A fainter surface wants a longer exposure, and by how much is the ratio
    of surface brightnesses — not of magnitudes, which is a body's total."""
    moon, missing = lucky.exposure_for("moon", 0.008)
    assert moon == pytest.approx(0.008)
    assert missing == pytest.approx(1.0)
    jupiter, _m = lucky.exposure_for("jupiter", 0.001)
    assert jupiter > 0.001 * 5


def test_exposure_stops_where_it_stops_freezing_the_seeing():
    """Past the cap the shortfall is reported instead of being taken in time:
    a 200 ms frame averages two atmospheres, and lucky imaging then has nothing
    sharp to pick."""
    exposure, missing = lucky.exposure_for("saturn", 0.008)
    assert exposure == pytest.approx(lucky.FREEZE_S)
    assert missing > 5.0


# ------------------------------------------------------------------- the window
def _planet(shape=(400, 500), centre=(320, 150), radius=12,
            peak=0.8) -> np.ndarray:
    rng = np.random.default_rng(0)
    img = rng.normal(0.01, 0.001, shape).astype(np.float32)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    img[(xx - centre[0]) ** 2 + (yy - centre[1]) ** 2 < radius ** 2] = peak
    return img


def test_window_finds_a_small_body_in_a_big_frame():
    box = lucky.window(_planet())
    cx, cy = box.centre
    assert cx == pytest.approx(320, abs=4) and cy == pytest.approx(150, abs=4)
    assert 24 <= box.w <= 60          # the disc, with room around it


def test_window_size_is_fixed_when_it_is_given():
    """The size is the denominator of everything measured inside it. If it
    breathed with the seeing, the sharpness readout would move while the
    focuser stood still."""
    box = lucky.window(_planet(), side=64)
    assert box.w == 64 and box.h == 64
    moved = lucky.window(_planet(centre=(120, 300)), side=64)
    assert moved.w == 64
    assert moved.centre[0] == pytest.approx(120, abs=4)


def test_window_is_none_when_nothing_is_bright():
    rng = np.random.default_rng(1)
    noise = rng.normal(0.01, 0.001, (256, 256)).astype(np.float32)
    assert lucky.window(noise) is None
    assert lucky.window(np.zeros((64, 64), np.float32)) is None


def test_a_crescent_is_still_a_body():
    """Venus at 5% lit is an arc, not a disc: it fills a third of its own
    bounding box, and a shape test strict enough to reject noise would reject
    it too if it were applied to everything."""
    lum = _planet(radius=14, peak=0.8)
    yy, xx = np.mgrid[0:400, 0:500]
    lum[(xx - 312) ** 2 + (yy - 150) ** 2 < 13 ** 2] = 0.01
    box = lucky.window(lum)
    assert box is not None
    assert box.centre[0] == pytest.approx(322, abs=6)


def test_window_origin_stays_even_on_the_mosaic():
    """The frame it maps onto is a Bayer mosaic: an odd origin shifts the
    pattern, and then every colour in the crop is the wrong one."""
    box = lucky.window(_planet()).scaled(2.0)
    assert box.x % 2 == 0 and box.y % 2 == 0
    assert box.w % 2 == 0 and box.h % 2 == 0


def test_centroid_follows_the_body_not_the_frame():
    cx, cy = lucky.centroid(_planet(centre=(120, 300)))
    assert cx == pytest.approx(120, abs=1.5)
    assert cy == pytest.approx(300, abs=1.5)


def test_levels_needs_the_window_to_see_a_planet():
    """The same frame, measured two ways. Over the whole frame a planet is a
    fraction of a percent of the pixels and reads as an empty frame; inside the
    window it
    is a disc with headroom."""
    lum = _planet()
    frame = np.repeat(np.repeat(lum, 2, axis=0), 2, axis=1)
    whole = lucky.levels(frame)
    assert whole.lit < 0.005
    assert "nothing bright" in whole.verdict()

    windowed = lucky.levels(frame, lucky.window(lum).scaled(2.0))
    assert windowed.lit > 0.1
    assert windowed.peak == pytest.approx(0.8, abs=0.01)
    assert "good" in windowed.verdict()


def test_a_small_window_is_sampled_whole():
    """Mars is eight pixels across at 1200 mm; every third pixel of that is
    nine samples, and the brightest one is not reliably among them."""
    frame = np.zeros((40, 40), np.float32)
    frame[19:21, 19:21] = 1.0
    lv = lucky.levels(frame, lucky.Box(10, 10, 20, 20))
    assert lv.peak == pytest.approx(1.0)
    assert lv.clipped > 0.0


# --------------------------------------------------------------------- levels
def test_levels_finds_clipping():
    frame = np.full((64, 64), 0.4, np.float32)
    frame[:16] = 1.0
    lv = lucky.levels(frame)
    assert lv.peak == pytest.approx(1.0)
    assert lv.clipped == pytest.approx(0.25, abs=0.05)
    assert lv.lit == pytest.approx(1.0)
    assert "CLIPPING" in lv.verdict()


def test_levels_sees_all_four_bayer_phases():
    """The sample must not land on one colour: red clips last, and a stride of
    2 or 4 on the mosaic would only ever look at one phase."""
    for dy in (0, 1):
        for dx in (0, 1):
            frame = np.zeros((64, 64), np.float32)
            frame[dy::2, dx::2] = 1.0
            assert lucky.levels(frame).peak == pytest.approx(1.0)
            assert lucky.levels(frame).clipped > 0.0


def test_exposure_factor_targets_the_headroom():
    lv = lucky.levels(np.full((32, 32), 0.2, np.float32))
    assert lv.factor == pytest.approx(lucky.HEADROOM / 0.2, rel=1e-3)
    assert "under-exposed" in lv.verdict()


def test_empty_frame_is_not_a_disc():
    assert lucky.levels(np.zeros((32, 32), np.float32)).lit == 0.0
    assert "nothing bright" in lucky.levels(np.zeros((32, 32),
                                                     np.float32)).verdict()


# ------------------------------------------------------------------ sharpness
def _disc(blur: int = 0) -> np.ndarray:
    import cv2
    y, x = np.mgrid[0:120, 0:120]
    img = ((x - 60) ** 2 + (y - 60) ** 2 < 40 ** 2).astype(np.float32)
    img += 0.15 * np.sin(x / 3.0) * img          # crater-scale detail
    return cv2.GaussianBlur(img, (0, 0), blur) if blur else img


def test_sharpness_falls_with_blur():
    assert sharpness(_disc()) > sharpness(_disc(blur=2)) > sharpness(_disc(blur=6))


def test_sharpness_ignores_gain():
    """Turning up the gain must not read as better focus."""
    a = _disc(blur=2)
    assert sharpness(a * 3.0) == pytest.approx(sharpness(a), rel=1e-4)


def test_sharpness_is_measured_on_the_body_not_on_the_frame():
    """Normalised by the mean level, so a frame that is mostly sky reports the
    contrast of its noise. The window is what makes the number the body's."""
    body = _disc(blur=1)
    frame = np.zeros((480, 480), np.float32)
    frame[100:220, 100:220] = body
    rng = np.random.default_rng(2)
    frame += rng.normal(0.0, 0.002, frame.shape).astype(np.float32)
    box = lucky.window(frame)
    assert sharpness(box.crop(frame)) < sharpness(frame) / 5


def test_sharpness_survives_a_black_frame():
    assert sharpness(np.zeros((10, 10), np.float32)) == 0.0


def test_sharpness_meter_ratio_matches_the_focus_meter():
    """1.0 means "at the session best" in both meters — the loupe and the beep
    read the number without knowing which one produced it."""
    m = SharpnessMeter()
    m.add(10.0)
    assert m.ratio_to_best() == pytest.approx(1.0)
    m.add(5.0)
    assert m.ratio_to_best() == pytest.approx(2.0)
    assert "below the sharpest" in m.verdict()
    m.reset_best()
    m.add(1.0)
    assert m.ratio_to_best() == pytest.approx(1.0)


# ---------------------------------------------------------------------- burst
def _meta(index: int) -> FrameMeta:
    return FrameMeta(index=index, timestamp=0.0, exposure=0.008, gain=100,
                     offset=20, bin=1, full_scale=65535, bayer=Bayer.RG)


def _burst(tmp_path, **kw) -> Burst:
    b = Burst(recorder=Recorder(root=tmp_path, target="Moon", compress=False),
              **kw)
    b.begin({"name": "test", "pixel_um": 4.63}, {})
    return b


def test_burst_stops_at_the_frame_limit(tmp_path):
    b = _burst(tmp_path, max_frames=3)
    raw = np.zeros((8, 8), np.uint16)
    for i in range(3):
        assert not b.done
        b.write(raw, _meta(500 + i), sharpness=1.5)
    assert b.done
    assert b.progress == 1.0
    assert b.end()["frames"] == 3


def test_burst_renumbers_from_one(tmp_path):
    """The camera's index is in the thousands by the third burst of the night,
    and a folder whose first file is sub_02841.fits reads as a folder with
    2840 files missing."""
    b = _burst(tmp_path, max_frames=2)
    b.write(np.zeros((8, 8), np.uint16), _meta(2841))
    names = sorted(p.name for p in (b.session_dir / "subs").iterdir())
    assert names == ["sub_00001.fits"]


def test_burst_records_the_sharpness_in_the_header(tmp_path):
    b = _burst(tmp_path, max_frames=1)
    path = b.write(np.zeros((8, 8), np.uint16), _meta(1), sharpness=12.3456)
    _data, hdr = read_fits(path)
    assert hdr["SHARPNS"] == pytest.approx(12.3456, abs=1e-3)
    assert hdr["OBJECT"] == "Moon"


def test_burst_without_limits_never_finishes_on_its_own(tmp_path):
    b = _burst(tmp_path)
    b.write(np.zeros((8, 8), np.uint16), _meta(1))
    assert not b.done
    assert b.progress == 0.0
