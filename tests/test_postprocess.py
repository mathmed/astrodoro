"""Offline post-processing pipeline.

The pipeline is a chain of independently-testable steps; what is worth pinning
here is that `process()` runs end to end without blowing up the clip range or
inverting the crop, since each step both feeds and depends on the linear domain
the one before it produced.
"""
from __future__ import annotations

import numpy as np

from astrodoro.core import postprocess

SIZE = 240


def _field(dispersion: tuple[float, float] = (0.0, 0.0),
          tint: tuple[float, float, float] = (1.0, 1.0, 1.0)) -> np.ndarray:
    """A star field over a dome-shaped gradient, linear RGB in [0, 1].

    `dispersion` shifts R and B from G by that many pixels, the same shape of
    error `align_channels` exists to remove. `tint` is a flat per-channel gain
    on the stars, the shape `calibrate_color` exists to remove.
    """
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    dome = 0.05 + 0.02 * np.exp(-((x - SIZE * 0.6) ** 2 + (y - SIZE * 0.3) ** 2)
                                / (2 * (SIZE * 0.4) ** 2))
    rgb = np.repeat(dome[:, :, None], 3, axis=2).astype(np.float32)

    xs = rng.uniform(20, SIZE - 20, 60)
    ys = rng.uniform(20, SIZE - 20, 60)
    peaks = rng.uniform(0.2, 0.9, 60)
    for cx, cy, peak in zip(xs, ys, peaks, strict=False):
        for k, (dx, dy, gain) in enumerate((
            (dispersion[0], dispersion[1], tint[0]),
            (0.0, 0.0, tint[1]),
            (-dispersion[0], -dispersion[1], tint[2]),
        )):
            star = peak * gain * np.exp(-(((x - cx - dx) ** 2 + (y - cy - dy) ** 2)
                                          / (2 * 1.8 ** 2)))
            rgb[..., k] += star
    return np.clip(rgb, 0.0, 1.0).astype(np.float32)


def test_process_end_to_end_stays_in_range():
    rgb = _field(dispersion=(0.4, -0.3), tint=(1.15, 1.0, 0.85))
    img = postprocess.process(rgb, crop=0, tiles=8, target_bg=0.10)
    assert img.shape == rgb.shape
    assert img.dtype == np.float32
    assert np.isfinite(img).all()
    assert img.min() >= 0.0 and img.max() <= 1.0


def test_process_reports_each_stage():
    rgb = _field(dispersion=(0.4, -0.3), tint=(1.15, 1.0, 0.85))
    messages: list[str] = []
    postprocess.process(rgb, crop=0, tiles=8, target_bg=0.10, log=messages.append)
    joined = " | ".join(messages)
    assert "channel alignment" in joined
    assert "colour calibration" in joined


def test_calibrate_color_neutralises_a_flat_tint():
    rgb = _field(tint=(1.2, 1.0, 0.8))
    out = postprocess.calibrate_color(rgb)
    lum = np.ascontiguousarray(out.sum(axis=2), dtype=np.float32)
    from astrodoro.core import stars as stars_mod
    sf = stars_mod.detect(lum, scale=1.0, max_stars=200, central=0.9)
    ys, xs = sf.xy[:, 1].astype(int), sf.xy[:, 0].astype(int)
    med = np.array([float(np.median(out[..., k])) for k in range(3)])
    flux = np.array([float(np.sum(out[ys, xs, k] - med[k])) for k in range(3)])
    ratio_r, ratio_b = flux[0] / flux[1], flux[2] / flux[1]
    assert 0.85 < ratio_r < 1.15
    assert 0.85 < ratio_b < 1.15


def test_coverage_crop_finds_a_noisy_border():
    rng = np.random.default_rng(1)
    lum = rng.normal(0.05, 0.002, (SIZE, SIZE)).astype(np.float32)
    lum[:30, :] += rng.normal(0.0, 0.02, (30, SIZE)).astype(np.float32)
    top, bottom, left, right = postprocess.coverage_crop(lum, margin=0)
    assert top > 10
    assert bottom < 10
    assert left < 10
    assert right < 10


def test_load_linear_round_trips_write_linear(tmp_path):
    rgb = _field()
    path = tmp_path / "stack.fits"
    postprocess.write_linear(rgb, path)
    back = postprocess.load_linear(path)
    np.testing.assert_allclose(back, rgb, atol=1e-6)
