"""End-to-end proof of the stacker, with no camera.

Simulates a star field with drift and residual rotation (a poorly aligned
equatorial platform), Poisson noise, a satellite crossing, and a large field
jump halfway through — what happens when you reset the platform or knock the
tube of a Dobsonian with no goto.
"""
from __future__ import annotations

import numpy as np

from astrodoro.core.stacker import LiveStacker

H = W = 500
RNG = np.random.default_rng(7)
STARS = np.column_stack([RNG.uniform(20, W - 20, 130),
                         RNG.uniform(20, H - 20, 130)])
#: Fluxes over a wide range: the faint ones are what only the stack reveals.
FLUX = 10 ** RNG.uniform(1.5, 3.4, len(STARS))
SKY = 120.0
SIGMA_PSF = 1.9


def render(shift, rot_deg, satellite=False):
    th = np.deg2rad(rot_deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    c = np.array([W / 2, H / 2])
    xy = (STARS - c) @ R.T + c + np.asarray(shift, dtype=float)

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.full((H, W), SKY, dtype=np.float32)
    for (x, y), f in zip(xy, FLUX, strict=True):
        if -10 < x < W + 10 and -10 < y < H + 10:
            img += f * np.exp(-((xx - x) ** 2 + (yy - y) ** 2)
                              / (2 * SIGMA_PSF ** 2))
    if satellite:
        for u in np.linspace(0, 1, 900):
            px, py = int(30 + u * 440), int(470 - u * 430)
            img[max(py - 1, 0):py + 2, max(px - 1, 0):px + 2] += 900.0
    return RNG.poisson(np.clip(img, 0, None)).astype(np.float32)


def measure_snr(img, xy_true):
    """Median SNR of the faint stars: peak above background / background noise."""
    noise = float(img[5:60, 5:60].std())
    bg = float(np.median(img))
    snrs = []
    for i in np.argsort(FLUX)[:15]:
        x, y = xy_true[i]
        xi, yi = int(round(x)), int(round(y))
        if 6 < xi < W - 6 and 6 < yi < H - 6:
            peak = float(img[yi - 2:yi + 3, xi - 2:xi + 3].max())
            snrs.append((peak - bg) / max(noise, 1e-6))
    return float(np.median(snrs)), noise


def _sequence(n=24):
    """Slow drift plus residual rotation, and a large jump at frame 12."""
    frames = []
    for k in range(n):
        drift = np.array([k * 0.8, -k * 0.55])
        rot = k * 0.045
        if k >= 12:
            drift = drift + np.array([180.0, -140.0])   # platform reset
            rot += 1.3
        frames.append((render(drift, rot, satellite=(k == 7)), drift, rot))
    return frames


def test_stack_survives_a_platform_reset_and_gains_snr():
    frames = _sequence()
    snr1, _noise1 = measure_snr(frames[0][0], STARS)

    st = LiveStacker((H, W), channels=1, ref_refresh=6, min_matched=8,
                     max_rms=2.0, sigma_clip=3.0)

    for k, (img, _drift, _rot) in enumerate(frames):
        if k == 12:
            st.new_segment()
        # Mono: the luminance is the image itself, at 1:1 scale.
        st.add(img[:, :, None], img, exposure=5.0, lum_scale=1.0)

    stack = st.result()[:, :, 0]
    # Renormalise to the same scale as the single frame, to compare SNR.
    stack_adu = stack * (st.accum
                         / np.maximum(st.weight, 1e-6)[..., None]).max()
    snr_n, _noise_n = measure_snr(stack_adu, STARS)

    assert st.n_stacked >= len(frames) - 2, f"only stacked {st.n_stacked}"
    assert st.n_stacked > 12, "did not survive the platform reset"
    assert snr_n / snr1 > np.sqrt(st.n_stacked) * 0.55, \
        f"SNR gain {snr_n/snr1:.2f}x below expectation"
    # After a 180 px jump, no pixel is covered by 90% of the frames, so
    # `overlap_fraction` is legitimately 0 here — what has to hold is that the
    # weight map is populated where the frames did overlap.
    assert st.coverage().max() > 0.5


def test_preview_alignment_reports_the_offset_without_mutating_state():
    """Same trick as a manual recentring, but read-only: the arrow that guides
    the user has to reflect the true offset without ever touching accum,
    weight or the reference — that is the whole point of pausing first."""
    from astrodoro.core.stars import detect

    st = LiveStacker((H, W), channels=1, ref_refresh=6, min_matched=8,
                     max_rms=2.0)
    ref_img = render((0.0, 0.0), 0.0)
    outcome = st.add(ref_img[:, :, None], ref_img, exposure=5.0, lum_scale=1.0)
    assert outcome.accepted and st.started

    accum_before = st.accum.copy()
    weight_before = st.weight.copy()
    n_before = st.n_stacked
    best_fwhm_before = st.best_fwhm
    ref_stars_before = st.ref_stars

    shift = (35.0, -22.0)
    moved_stars = detect(render(shift, 0.0), scale=1.0)
    al = st.preview_alignment(moved_stars)

    assert al is not None and al.ok
    # `al.shift` is the translation taking the moved frame INTO the reference,
    # so it is the negative of the physical offset applied above.
    assert abs(al.shift[0] - (-shift[0])) < 1.0
    assert abs(al.shift[1] - (-shift[1])) < 1.0

    assert np.array_equal(st.accum, accum_before)
    assert np.array_equal(st.weight, weight_before)
    assert st.n_stacked == n_before
    assert st.best_fwhm == best_fwhm_before
    assert st.ref_stars is ref_stars_before


def test_preview_alignment_none_before_a_reference_exists():
    st = LiveStacker((H, W), channels=1)
    from astrodoro.core.stars import detect
    assert st.preview_alignment(detect(render((0.0, 0.0), 0.0), scale=1.0)) is None


def test_sigma_clipping_removes_the_satellite():
    frames = _sequence()
    st = LiveStacker((H, W), channels=1, ref_refresh=6, sigma_clip=3.0)
    for k, (img, _drift, _rot) in enumerate(frames):
        if k == 12:
            st.new_segment()
        st.add(img[:, :, None], img, exposure=5.0, lum_scale=1.0)

    stack = st.result()[:, :, 0]
    stack_adu = stack * (st.accum
                         / np.maximum(st.weight, 1e-6)[..., None]).max()
    diagonal = np.array([stack_adu[470 - int(u * 430), 30 + int(u * 440)]
                         for u in np.linspace(0.15, 0.85, 60)])
    bg = float(np.median(stack_adu))
    assert np.median(diagonal) < bg * 1.35, "the satellite trail is still visible"
