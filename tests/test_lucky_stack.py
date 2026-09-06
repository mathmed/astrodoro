"""Offline stacking of a burst of the Moon or a planet.

The three things worth pinning are the ones that fail silently: the direction of
the alignment shift — the same class of bug `test_register_warp.py` exists for —
the sharpness ranking, which decides what is thrown away and would still produce
a plausible-looking stack if it were inverted, and the crop, which is the
difference between stacking a planet and averaging the sky around one.
"""
from __future__ import annotations

import numpy as np
import pytest

from astrodoro.core import lucky_stack
from astrodoro.core.recorder import Burst, Recorder
from astrodoro.core.source import FrameMeta
from astrodoro.drivers.svbony.sdk import Bayer

SIZE = 256


def _moon(dx: int = 0, dy: int = 0, blur: float = 0.0,
          scale: int = 38000) -> np.ndarray:
    """A lit disc with crater-scale detail, optionally displaced and softened."""
    import cv2

    y, x = np.mgrid[0:SIZE, 0:SIZE]
    cx, cy = SIZE // 2 + dx, SIZE // 2 + dy
    disc = (((x - cx) ** 2 + (y - cy) ** 2) < 90 ** 2).astype(np.float32)
    detail = 0.25 * np.sin((x - cx) / 5.0) * np.cos((y - cy) / 5.0)
    img = np.clip(disc * (1.0 + detail), 0.0, 1.5)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    return (img / 1.5 * scale).astype(np.uint16)


def _burst(tmp_path, frames, with_sharpness: bool = True):
    """Write `frames` as a burst folder, as MOON would.

    `with_sharpness=False` produces frames from anywhere else: no `SHARPNS`, so
    ranking has to read the pixels.
    """
    from astrodoro.core import debayer
    from astrodoro.core.focus import sharpness as measure

    b = Burst(recorder=Recorder(root=tmp_path / "sessions", target="Moon",
                                compress=False))
    b.begin({"name": "synthetic", "pixel_um": 4.63}, {})
    for i, raw in enumerate(frames):
        value = None
        if with_sharpness:
            value = measure(debayer.cfa_to_luminance(
                raw.astype(np.float32) / 65532))
        b.write(raw, FrameMeta(index=i, timestamp=float(i), exposure=0.008,
                               gain=100, offset=20, bin=1, full_scale=65532,
                               bayer=Bayer.GR), sharpness=value)
    return b.session_dir


def _rgb(raw: np.ndarray) -> np.ndarray:
    """The same demosaic the stacker runs, so comparisons are like for like.

    A raw mosaic and a debayered frame are not comparable on any gradient
    measure: the CFA checkerboard is itself a gradient at every pixel.
    """
    from astrodoro.core import debayer

    f = raw.astype(np.float32) / 65532
    return debayer.to_rgb((f * 65535).astype(np.uint16), Bayer.GR,
                          quality="linear").astype(np.float32) / 65535.0


# ------------------------------------------------------------------- ranking
def test_the_sharpest_frame_comes_first(tmp_path):
    folder = _burst(tmp_path, [_moon(blur=b) for b in (4.0, 0.0, 2.0)])
    ranked = lucky_stack.rank(lucky_stack.subs(folder))
    # A burst renumbers from 1, so these are FRAMEIDX 1, 2, 3 in capture order.
    assert [f.index for f in ranked] == [2, 3, 1]
    assert not any(f.measured for f in ranked)      # SHARPNS was in the header


def test_a_burst_without_sharpness_is_measured(tmp_path):
    """Frames from anywhere else have no SHARPNS, and ranking still has to work."""
    folder = _burst(tmp_path, [_moon(blur=4.0), _moon()], with_sharpness=False)
    ranked = lucky_stack.rank(lucky_stack.subs(folder))
    assert all(f.measured for f in ranked)
    assert ranked[0].index == 2                     # the unblurred one
    assert ranked[0].sharpness > ranked[1].sharpness


def test_measuring_can_be_declined(tmp_path):
    folder = _burst(tmp_path, [_moon(blur=4.0), _moon()], with_sharpness=False)
    ranked = lucky_stack.rank(lucky_stack.subs(folder), measure_missing=False)
    assert [f.sharpness for f in ranked] == [0.0, 0.0]


@pytest.mark.parametrize(("best", "kept"), [(1.0, 8), (0.25, 2), (0.01, 1)])
def test_the_cut_keeps_a_fraction_and_never_nothing(tmp_path, best, kept):
    folder = _burst(tmp_path, [_moon(blur=i * 0.5) for i in range(8)])
    assert lucky_stack.stack(folder, best=best).n_used == kept


# ----------------------------------------------------------------- alignment
@pytest.mark.parametrize(("dx", "dy"), [(6, 0), (0, -4), (-5, 3)])
def test_the_shift_is_applied_towards_the_reference(tmp_path, dx, dy):
    """The classic bug of this stage is the sign, and it stacks quietly blurred.

    A displaced frame must come back onto the reference, not move twice as far
    away — which is what an inverted sign produces, and it still writes a stack.
    """
    # The unblurred frame ranks first and is therefore the reference; without
    # forcing that, two identical frames tie and either may become it, which
    # flips the expected sign and makes the test say nothing.
    folder = _burst(tmp_path, [_moon(), _moon(dx=dx, dy=dy, blur=0.8)])
    result = lucky_stack.stack(folder, best=1.0)
    assert result.n_used == 2
    assert result.shifts[0] == (0.0, 0.0)
    assert result.shifts[1] == pytest.approx((-dx, -dy), abs=0.8)


def test_alignment_recovers_the_reference(tmp_path):
    """Aligned and averaged, a displaced burst is sharper than an unaligned one."""
    from astrodoro.core.focus import sharpness

    frames = [_moon(), _moon(dx=5, dy=-4), _moon(dx=-6, dy=3)]
    folder = _burst(tmp_path, frames)
    aligned = sharpness(lucky_stack.stack(folder, best=1.0).stack.mean(axis=2))
    naive = sharpness(np.mean([_rgb(f) for f in frames], axis=0).mean(axis=2))
    single = sharpness(_rgb(frames[0]).mean(axis=2))

    assert aligned > naive * 2          # averaging unaligned frames is a blur
    assert aligned > single * 0.9       # aligned, it keeps the frame's detail


def test_a_still_burst_needs_no_shifting(tmp_path):
    folder = _burst(tmp_path, [_moon(), _moon(), _moon()])
    result = lucky_stack.stack(folder, best=1.0)
    assert result.max_shift < 0.5


# -------------------------------------------------------------------- output
def test_the_stack_is_written_in_the_usual_layout(tmp_path):
    from astropy.io import fits

    folder = _burst(tmp_path, [_moon(blur=b) for b in (0.0, 1.0, 3.0, 5.0)])
    result = lucky_stack.stack(folder, best=0.5)
    path = lucky_stack.write(result, folder / "stack_moon.fits", target="Moon")

    data = fits.getdata(path)
    hdr = fits.getheader(path)
    assert data.shape == (3, SIZE, SIZE)          # channel first, like the rest
    assert hdr["NCOMBINE"] == 2 and hdr["NTOTAL"] == 4
    assert hdr["OBJECT"] == "Moon"
    assert hdr["EXPTOTAL"] == pytest.approx(0.016)


def test_a_flat_reaches_the_frames(tmp_path):
    """Calibration goes through the one function the whole program shares."""
    folder = _burst(tmp_path, [_moon(), _moon()])
    plain = lucky_stack.stack(folder, best=1.0).stack
    flat = np.full((SIZE, SIZE), 0.5, np.float32)
    halved = lucky_stack.stack(folder, best=1.0, flat=flat).stack
    assert halved.max() > plain.max()             # dividing by 0.5 brightens


def test_an_empty_folder_says_so(tmp_path):
    with pytest.raises(RuntimeError, match="no FITS"):
        lucky_stack.stack(tmp_path)


# -------------------------------------------------------------- background
def test_the_pedestal_is_measured_and_removed(tmp_path):
    """An additive veil makes every colour ratio in the frame a lie."""
    pedestal = 1300                                   # ADU, offset plus skyglow
    folder = _burst(tmp_path, [_moon() + pedestal, _moon() + pedestal])
    result = lucky_stack.stack(folder, best=1.0)

    assert result.background is not None
    assert result.background.mean() == pytest.approx(pedestal / 65532, rel=0.1)
    # Sky comes back to zero; the disc keeps its signal.
    lum = result.stack.mean(axis=2)
    assert np.percentile(lum, 5) == pytest.approx(0.0, abs=1e-3)
    assert lum.max() > 0.3


def test_removing_the_pedestal_deepens_the_colour(tmp_path):
    """The measured point of doing it: a veil dilutes the eclipse's red."""
    red = _moon().astype(np.float32)
    frame = np.stack([red * 1.4, red, red * 0.7], axis=-1)
    veiled = np.clip(frame + 1300, 0, 65532).astype(np.uint16)
    # A colour frame is not what a burst holds, so mosaic it back down: take
    # each channel where its own Bayer phase sits in GRBG.
    cfa = np.zeros(red.shape, np.uint16)
    cfa[0::2, 1::2] = veiled[0::2, 1::2, 0]           # R
    cfa[0::2, 0::2] = veiled[0::2, 0::2, 1]           # G
    cfa[1::2, 1::2] = veiled[1::2, 1::2, 1]           # G
    cfa[1::2, 0::2] = veiled[1::2, 0::2, 2]           # B

    folder = _burst(tmp_path, [cfa, cfa])
    with_veil = lucky_stack.stack(folder, best=1.0, subtract_background=False)
    without = lucky_stack.stack(folder, best=1.0)

    def ratio(img):
        disc = img.mean(axis=2) > np.percentile(img.mean(axis=2), 70)
        return (float(np.median(img[..., 0][disc]))
                / float(np.median(img[..., 1][disc])))

    assert ratio(without.stack) > ratio(with_veil.stack)


def test_the_pedestal_can_be_left_in(tmp_path):
    folder = _burst(tmp_path, [_moon() + 1300])
    assert lucky_stack.stack(folder, best=1.0,
                             subtract_background=False).background is None


# ---------------------------------------------------------------- sharpen
def test_sharpening_is_off_unless_asked(tmp_path):
    folder = _burst(tmp_path, [_moon(blur=2.0)])
    plain = lucky_stack.stack(folder, best=1.0)
    assert plain.sharpen == 0.0

    from astrodoro.core.focus import sharpness
    sharpened = lucky_stack.stack(folder, best=1.0, sharpen_amount=0.8)
    assert sharpened.sharpen == 0.8
    assert (sharpness(sharpened.stack.mean(axis=2))
            > sharpness(plain.stack.mean(axis=2)))


def test_sharpening_never_goes_negative(tmp_path):
    """An unsharp mask undershoots at an edge, and a negative flux is not one."""
    folder = _burst(tmp_path, [_moon()])
    result = lucky_stack.stack(folder, best=1.0, sharpen_amount=1.5)
    assert result.stack.min() >= 0.0


def test_the_header_records_what_was_done(tmp_path):
    from astropy.io import fits

    folder = _burst(tmp_path, [_moon() + 1300, _moon() + 1300])
    result = lucky_stack.stack(folder, best=1.0, sharpen_amount=0.6)
    path = lucky_stack.write(result, folder / "stack_moon.fits")
    hdr = fits.getheader(path)
    assert hdr["SHARPEN"] == pytest.approx(0.6)
    assert hdr["BKGSUB_G"] > 0.0


# ------------------------------------------------------------------- the crop
def _planet(dx: int = 0, dy: int = 0, blur: float = 0.0,
            radius: int = 24, scale: int = 38000) -> np.ndarray:
    """A small disc in a big frame — a planet, which is what the Moon is not."""
    import cv2

    y, x = np.mgrid[0:SIZE, 0:SIZE]
    cx, cy = SIZE // 2 + dx, SIZE // 2 + dy
    disc = (((x - cx) ** 2 + (y - cy) ** 2) < radius ** 2).astype(np.float32)
    detail = 0.3 * np.sin((x - cx) / 3.0) * np.cos((y - cy) / 3.0)
    img = np.clip(disc * (1.0 + detail), 0.0, 1.5)
    if blur:
        img = cv2.GaussianBlur(img, (0, 0), blur)
    return (img / 1.5 * scale).astype(np.uint16)


def test_a_planet_is_stacked_in_a_window_on_it(tmp_path):
    folder = _burst(tmp_path, [_planet(), _planet(blur=0.8)])
    result = lucky_stack.stack(folder, best=1.0)
    assert result.crop is not None
    assert result.stack.shape[0] == result.crop.h < SIZE
    # The window is on the body, not on a corner of the frame.
    cx = result.crop.x + result.crop.w / 2
    assert cx == pytest.approx(SIZE / 2, abs=8)


def test_the_moon_is_not_cropped(tmp_path):
    """At this size the frame *is* the body: cropping would throw away the limb
    the alignment reads, for a window barely smaller than what it came from."""
    folder = _burst(tmp_path, [_moon(), _moon(blur=0.8)])
    result = lucky_stack.stack(folder, best=1.0)
    assert result.crop is None
    assert result.stack.shape[0] == SIZE


def test_the_crop_can_be_refused_or_given_a_size(tmp_path):
    folder = _burst(tmp_path, [_planet(), _planet(blur=0.8)])
    assert lucky_stack.stack(folder, best=1.0, crop="full").crop is None
    fixed = lucky_stack.stack(folder, best=1.0, crop=96)
    assert fixed.crop.w == 96 and fixed.stack.shape[0] == 96


@pytest.mark.parametrize(("dx", "dy"), [(30, 0), (0, -25), (-40, 22)])
def test_a_planet_that_wandered_is_still_aligned(tmp_path, dx, dy):
    """The body drifts a long way across a frame over half a minute, and a
    phase correlation only sees a shift small against its own window. The
    centroid is what carries the frame back before the sub-pixel work starts."""
    folder = _burst(tmp_path, [_planet(), _planet(dx=dx, dy=dy, blur=0.8)])
    result = lucky_stack.stack(folder, best=1.0)
    assert result.n_used == 2
    assert result.shifts[1] == pytest.approx((-dx, -dy), abs=1.5)


def test_a_wandering_planet_is_stacked_and_not_smeared(tmp_path):
    """The point of following the body: over a burst it crosses the frame, and
    a window that stayed where it was would average three planets side by side.
    That still writes a stack — a wider, fainter one — so what is checked is the
    peak, which alignment keeps and a smear spends."""
    frames = [_planet(), _planet(dx=26, dy=-18, blur=0.4),
              _planet(dx=-31, dy=20, blur=0.6)]
    folder = _burst(tmp_path, frames)
    result = lucky_stack.stack(folder, best=1.0)
    one = result.crop.crop(_rgb(frames[0]))
    still = np.mean([result.crop.crop(_rgb(f)) for f in frames], axis=0)
    assert result.stack.max() == pytest.approx(one.max(), rel=0.05)
    assert still.max() < 0.7 * one.max()


def test_the_window_is_recorded_in_the_header(tmp_path):
    from astropy.io import fits

    folder = _burst(tmp_path, [_planet(), _planet(blur=0.8)])
    result = lucky_stack.stack(folder, best=1.0)
    path = lucky_stack.write(result, folder / "stack_lucky.fits")
    hdr = fits.getheader(path)
    assert hdr["CROPX"] == result.crop.x and hdr["CROPY"] == result.crop.y
