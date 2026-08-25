"""Self-test for the direction of the transform — the easiest mistake here.

`register.warp` takes M in the forward direction (src -> destination). Inverting
it is the classic bug of this stage, and it fails silently: frames still stack,
just onto the wrong place.
"""
from __future__ import annotations

import numpy as np

from astrodoro.core import register


def _synthetic(stars_xy, shape=(400, 400), sigma=2.0, amp=5000.0):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]].astype(np.float32)
    img = np.full(shape, 100.0, dtype=np.float32)
    for x, y in stars_xy:
        img += amp * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma ** 2))
    return img


def test_warp_brings_source_onto_reference():
    rng = np.random.default_rng(42)
    ref_xy = rng.uniform(60, 340, size=(25, 2))

    # Frame shifted 17,-9 px and rotated 1.5 degrees about the centre.
    th = np.deg2rad(1.5)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    c = np.array([200.0, 200.0])
    src_xy = (ref_xy - c) @ R.T + c + np.array([17.0, -9.0])

    al = register.estimate(src_xy, ref_xy)
    assert al.ok, al.reason
    assert al.n_matched >= 8
    assert al.rms < 0.5, al.rms
    assert abs(al.rotation_deg - (-1.5)) < 0.3, al.rotation_deg

    # Applying the matrix to the source coordinates must reproduce the reference.
    pred = (al.matrix[:, :2] @ src_xy.T).T + al.matrix[:, 2]
    d = np.linalg.norm(pred - ref_xy, axis=1)
    assert d.max() < 0.5, d.max()

    # And warping the source image must put the stars where the reference has them.
    aligned = register.warp(_synthetic(src_xy), al.matrix, (400, 400))
    for x, y in ref_xy:
        xi, yi = int(round(x)), int(round(y))
        patch = aligned[yi - 3:yi + 4, xi - 3:xi + 4]
        assert patch.max() > 1000.0, \
            f"star missing at ({x:.0f},{y:.0f}): {patch.max():.0f}"


def test_warp_keeps_the_channel_axis():
    """cv2 drops a channel axis of size 1, which breaks mono accumulators."""
    img = np.zeros((40, 40, 1), np.float32)
    M = np.array([[1.0, 0.0, 3.0], [0.0, 1.0, -2.0]])
    assert register.warp(img, M, (40, 40)).shape == (40, 40, 1)
