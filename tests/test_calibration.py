"""The calibration order, and the one rule that makes bias and dark different.

`calibrate` is deliberately one function called by both the GUI and the CLI,
because the order `(raw - dark|bias) / flat -> hot -> scale -> clip` is not
optional: any other order corrupts the stack quietly, and a wrong pedestal
looks exactly like sky.

The synthetic frames here are the whole point — a light of known signal on a
known pedestal with a known vignetting curve is the only way to assert that
what came out is what the arithmetic promises, and none of it needs a camera.
"""
from __future__ import annotations

import numpy as np
import pytest

from astrodoro.core import masters
from astrodoro.core.calibration import (
    calibrate,
    flat_master,
    hot_pixel_map,
    prepare_flat,
)
from astrodoro.core.source import FrameMeta

H, W = 32, 48
SCALE = 4095
PEDESTAL = 500.0


def _meta(full_scale: int = SCALE) -> FrameMeta:
    return FrameMeta(index=1, timestamp=0.0, exposure=1.0, gain=250, offset=20,
                     bin=2, full_scale=full_scale, bayer=None)


def _light(signal: float = 300.0, pedestal: float = PEDESTAL) -> np.ndarray:
    return np.full((H, W), pedestal + signal, dtype=np.uint16)


def _vignette(depth: float = 0.6) -> np.ndarray:
    """A radial falloff to `depth` in the corners, median-1 already."""
    y, x = np.mgrid[0:H, 0:W]
    r = np.hypot((y - H / 2) / (H / 2), (x - W / 2) / (W / 2)) / np.sqrt(2)
    return (1.0 - (1.0 - depth) * r**2).astype(np.float32)


# ------------------------------------------------------------------- the order
def test_dark_then_flat_in_that_order():
    """Dividing before subtracting would scale the pedestal by the vignetting
    and leave a residual gradient that no stretch can undo."""
    dark = np.full((H, W), PEDESTAL, dtype=np.float32)
    vig = _vignette()
    raw = (PEDESTAL + 300.0 * vig).astype(np.uint16)

    out = calibrate(raw, _meta(), dark=dark, flat=vig)
    assert out.min() > 0
    # 300 ADU of signal, flat-corrected back to uniform, on a 4095 scale.
    assert np.allclose(out, 300.0 / SCALE, atol=1.5 / SCALE)
    assert out.std() < 1.0 / SCALE

    # The wrong order, for contrast: the pedestal picks up the vignetting.
    wrong = np.asarray(raw, np.float32) / vig - PEDESTAL
    assert wrong.std() > 30.0


def test_scale_and_clip_come_last():
    raw = np.full((H, W), SCALE * 2, dtype=np.uint16)
    out = calibrate(raw, _meta())
    assert out.dtype == np.float32
    assert out.max() == pytest.approx(1.0)
    assert out.min() >= 0.0


def test_the_raw_frame_is_never_modified():
    raw = _light()
    before = raw.copy()
    calibrate(raw, _meta(), dark=np.full((H, W), PEDESTAL, np.float32))
    assert np.array_equal(raw, before)


# -------------------------------------------------------------- bias vs. dark
def test_bias_removes_the_pedestal_when_there_is_no_dark():
    bias = np.full((H, W), PEDESTAL, dtype=np.float32)
    out = calibrate(_light(signal=300.0), _meta(), bias=bias)
    assert np.allclose(out, 300.0 / SCALE, atol=1e-6)


def test_dark_and_bias_are_never_both_subtracted():
    """The invariant. A dark is taken at the lights' exposure with the sensor
    capped, so it already contains the pedestal a bias measures; subtracting
    both takes it off twice, the sky goes negative and the `maximum` clamp
    turns it into a black, signal-free floor."""
    thermal = 40.0
    dark = np.full((H, W), PEDESTAL + thermal, dtype=np.float32)
    bias = np.full((H, W), PEDESTAL, dtype=np.float32)
    raw = _light(signal=300.0 + thermal)

    with_dark = calibrate(raw, _meta(), dark=dark)
    with_both = calibrate(raw, _meta(), dark=dark, bias=bias)
    assert np.allclose(with_both, with_dark)
    assert np.allclose(with_both, 300.0 / SCALE, atol=1e-6)

    # What subtracting twice would have cost: the whole signal, and then some.
    twice = np.asarray(raw, np.float32) - dark - bias
    assert twice.max() < 0


def test_the_bias_serves_when_the_dark_is_refused_for_its_shape():
    """A dark of another bin is not applied — and the bias, which does match,
    still has to be. Otherwise picking the wrong dark silently costs the
    pedestal correction too."""
    dark = np.full((H // 2, W), PEDESTAL, dtype=np.float32)
    bias = np.full((H, W), PEDESTAL, dtype=np.float32)
    out = calibrate(_light(signal=300.0), _meta(), dark=dark, bias=bias)
    assert np.allclose(out, 300.0 / SCALE, atol=1e-6)


def test_a_master_of_the_wrong_shape_is_ignored_not_broadcast():
    raw = _light()
    for kw in ({"dark": np.zeros((H + 2, W), np.float32)},
               {"bias": np.zeros((H, W + 2), np.float32)},
               {"flat": np.ones((H // 2, W // 2), np.float32)}):
        out = calibrate(raw, _meta(), **kw)
        assert out.shape == (H, W)
        assert np.allclose(out, (PEDESTAL + 300.0) / SCALE, atol=1e-6)


# ---------------------------------------------------------------------- flats
def test_prepare_flat_normalises_to_a_median_of_one():
    flat = prepare_flat(_vignette() * 12345.0)
    assert float(np.median(flat)) == pytest.approx(1.0, abs=1e-4)
    assert flat.dtype == np.float32


def test_prepare_flat_clamps_the_divisor():
    """A flat pixel at 1% of the median would multiply the noise there by a
    hundred; the clamp is what stops a dust shadow from becoming a hot spot."""
    raw = np.full((H, W), 1000.0, dtype=np.float32)
    raw[0, 0] = 1.0
    raw[0, 1] = 100000.0
    flat = prepare_flat(raw)
    assert flat[0, 0] == pytest.approx(0.15)
    assert flat[0, 1] == pytest.approx(4.0)


def test_prepare_flat_refuses_a_flat_with_no_signal():
    assert prepare_flat(np.zeros((H, W), np.float32)) is None


def test_flat_master_subtracts_the_pedestal_before_normalising():
    """With the pedestal left in, a multiplicative correction is computed from
    an additive offset: the vignetting curve comes out shallow and the light it
    corrects keeps part of its falloff.

    The number `docs/design-notes.md` quotes: on a 500 ADU pedestal and a flat
    median of 20000 ADU, 2.4% of the falloff survives the correction. It is
    small and always in the same direction, which makes it a gradient in the
    stack rather than noise.
    """
    vig = _vignette(depth=0.5)
    frames = [(PEDESTAL + 20000.0 * vig).astype(np.float32) for _ in range(5)]
    bias = np.full((H, W), PEDESTAL, dtype=np.float32)

    corrected = prepare_flat(flat_master(frames, bias))
    truth = prepare_flat(vig)
    assert np.allclose(corrected, truth, atol=1e-3)

    baked = prepare_flat(flat_master(frames, None))
    assert baked.min() > corrected.min(), (
        "the pedestal made the vignetting curve shallow")

    light = 300.0 * vig
    residual = light / baked
    assert 1.0 - residual.min() / residual.max() == pytest.approx(0.024,
                                                                  abs=0.004)
    kept = light / corrected
    assert 1.0 - kept.min() / kept.max() < 1e-3


def test_flat_master_is_a_divisor_so_it_never_reaches_zero():
    frames = [np.full((H, W), 100.0, np.float32) for _ in range(3)]
    master = flat_master(frames, np.full((H, W), 100.0, np.float32))
    assert master.min() >= 1.0


# -------------------------------------------------------------- hot pixels
def test_hot_pixels_are_replaced_from_the_same_bayer_phase():
    """Using the immediate neighbours would mix mosaic channels and leave a
    coloured dot where the hot pixel was."""
    raw = np.zeros((H, W), dtype=np.uint16)
    # One value per Bayer phase, so a wrong-phase median is detectable.
    for dy in (0, 1):
        for dx in (0, 1):
            raw[dy::2, dx::2] = 100 + 100 * (dy * 2 + dx)
    raw[4, 4] = 4000
    hot = np.zeros((H, W), dtype=bool)
    hot[4, 4] = True

    out = calibrate(raw, _meta(), hot=hot)
    assert out[4, 4] * SCALE == pytest.approx(100.0, abs=1.0)


def test_hot_pixel_map_finds_the_outliers_of_a_dark():
    dark = np.random.default_rng(7).normal(500, 3, (H, W)).astype(np.float32)
    dark[2, 3] = 5000.0
    hot = hot_pixel_map(dark)
    assert hot[2, 3]
    assert hot.sum() == 1


# -------------------------------------------------------------------- masters
def test_a_master_is_a_median_so_one_bad_frame_does_not_survive():
    """A mean would keep a satellite trail or a cosmic ray at 1/N of its
    brightness, and a master is exactly the frame that must not have any."""
    frames = [np.full((H, W), 500.0, np.float32) for _ in range(9)]
    frames[4] = frames[4].copy()
    frames[4][8, 8] = 60000.0
    master = masters.combine(frames)
    assert master[8, 8] == pytest.approx(500.0)
    assert master.dtype == np.float32


def test_the_name_carries_what_the_master_has_to_match():
    s = masters.Setup(gain=250, offset=20, exposure=5.0, bin=2,
                      temperature=-10.0, bayer="GRBG", full_scale=4095)
    assert masters.name("dark", s) == "dark_g250_o20_e5.00s_bin2_-10C.fits"
    # A bias is defined by being the shortest exposure the camera does, so the
    # exposure is not part of its identity; a flat is a ratio, so neither the
    # exposure nor the offset are part of its.
    assert masters.name("bias", s) == "bias_g250_o20_bin2.fits"
    assert masters.name("flat", s) == "flat_g250_bin2.fits"


def test_a_dark_without_a_cooler_is_named_without_a_temperature():
    s = masters.Setup(gain=100, offset=8, exposure=0.5, bin=1)
    assert masters.name("dark", s) == "dark_g100_o8_e0.50s_bin1.fits"


def test_write_round_trips_through_the_header(tmp_path):
    from astrodoro.core.recorder import read_fits

    s = masters.Setup(gain=250, offset=20, exposure=5.0, bin=2,
                      temperature=-10.0, bayer="GRBG", full_scale=4095)
    data = np.full((H, W), 501.0, np.float32)
    path = masters.write(tmp_path / "darks", "dark", data, s, n_frames=17)

    assert path.parent.is_dir()
    back, hdr = read_fits(path)
    assert np.allclose(back, data)
    assert hdr["IMAGETYP"] == "DARK"
    assert hdr["EXPTIME"] == pytest.approx(5.0)
    assert hdr["GAIN"] == 250 and hdr["OFFSET"] == 20
    assert hdr["XBINNING"] == 2 and hdr["NCOMBINE"] == 17
    assert hdr["CCD-TEMP"] == pytest.approx(-10.0)
    assert hdr["BAYERPAT"] == "GRBG"
    assert masters.mismatch("dark", hdr, s) == []


# ------------------------------------------------------------------- mismatch
def _hdr(kind, **kw):
    s = masters.Setup(gain=kw.pop("gain", 250), offset=kw.pop("offset", 20),
                      exposure=kw.pop("exposure", 5.0), bin=2,
                      temperature=kw.pop("temperature", None))
    return masters.header(kind, s, 20)


def test_the_offset_matters_for_a_bias_and_not_for_a_flat():
    """The offset *is* the pedestal a bias measures — at another offset it
    removes the wrong constant. A flat is a ratio and does not care."""
    want = masters.Setup(gain=250, offset=20, exposure=5.0, bin=2)
    assert any("offset" in m for m in
               masters.mismatch("bias", _hdr("bias", offset=8), want))
    assert masters.mismatch("flat", _hdr("flat", offset=8), want) == []


def test_gain_is_checked_for_every_kind():
    want = masters.Setup(gain=250, offset=20, exposure=5.0, bin=2)
    for kind in masters.KINDS:
        assert masters.mismatch(kind, _hdr(kind, gain=100), want), kind


def test_exposure_and_temperature_are_a_darks_business_only():
    want = masters.Setup(gain=250, offset=20, exposure=5.0, bin=2,
                         temperature=-10.0)
    assert masters.mismatch("dark", _hdr("dark", exposure=1.0), want)
    assert masters.mismatch("dark", _hdr("dark", exposure=5.1), want) == [], \
        "2% off is within the tolerance; a dark a hair off is still a dark"
    assert masters.mismatch(
        "dark", _hdr("dark", exposure=5.0, temperature=-3.0), want)
    # A bias is milliseconds by construction: its EXPTIME never matches the
    # lights and saying so on every session would be noise.
    assert masters.mismatch("bias", _hdr("bias", exposure=0.000036), want) == []


def test_mismatch_survives_a_header_that_says_nothing():
    from astropy.io import fits

    want = masters.Setup(gain=250, offset=20, exposure=5.0, bin=2)
    assert masters.mismatch("dark", fits.Header(), want) == []
