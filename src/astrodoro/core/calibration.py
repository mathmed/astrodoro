"""Frame calibration — the single place where the order of operations lives.

This used to be duplicated between the GUI worker and the CLI, and the order is
not optional: getting it wrong corrupts the stack quietly. One function, called
by both.
"""
from __future__ import annotations

import numpy as np

from .source import FrameMeta


def calibrate(raw: np.ndarray, meta: FrameMeta,
              dark: np.ndarray | None = None,
              flat: np.ndarray | None = None,
              hot: np.ndarray | None = None) -> np.ndarray:
    """Calibrate one raw frame to float32 in [0, 1].

    The mandatory order: `(raw - dark) / flat` -> hot pixel correction ->
    `/ meta.full_scale` -> clip [0, 1]. Demosaicing happens after this, on the
    caller's side, because the luminance path wants the mosaic and the display
    path wants RGB.
    """
    f = np.asarray(raw, dtype=np.float32)
    if f is raw:
        f = f.copy()
    if dark is not None and dark.shape == f.shape:
        f -= dark
        np.maximum(f, 0.0, out=f)
    if flat is not None and flat.shape == f.shape:
        f /= flat
    if hot is not None and hot.shape == f.shape:
        f = fix_hot_bayer(f, hot)
    f /= max(meta.full_scale, 1)
    np.clip(f, 0.0, 1.0, out=f)
    return f


def fix_hot_bayer(f: np.ndarray, hot: np.ndarray) -> np.ndarray:
    """Replace hot pixels with the median of SAME-COLOUR neighbours.

    Using the immediate neighbours would mix mosaic channels and create a colour
    artefact where the hot pixel was.
    """
    import cv2
    out = f.copy()
    for dy in (0, 1):
        for dx in (0, 1):
            phase_hot = hot[dy::2, dx::2]
            if not phase_hot.any():
                continue
            phase = np.ascontiguousarray(f[dy::2, dx::2])
            phase_med = cv2.medianBlur(phase, 3)
            sub = out[dy::2, dx::2]
            sub[phase_hot] = phase_med[phase_hot]
            out[dy::2, dx::2] = sub
    return out


def hot_pixel_map(master_dark: np.ndarray, sigma: float = 12.0) -> np.ndarray:
    """Boolean map of pixels persistently far above the dark's background."""
    med = float(np.median(master_dark))
    mad = float(np.median(np.abs(master_dark - med))) * 1.4826
    return master_dark > med + sigma * max(mad, 1.0)
