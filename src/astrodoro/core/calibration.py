"""Frame calibration — the single place where the order of operations lives.

This used to be duplicated between the GUI worker and the CLI, and the order is
not optional: getting it wrong corrupts the stack quietly. One function, called
by both.

`core/masters.py` builds the masters this applies.
"""
from __future__ import annotations

import numpy as np

from .source import FrameMeta


def calibrate(raw: np.ndarray, meta: FrameMeta,
              dark: np.ndarray | None = None,
              flat: np.ndarray | None = None,
              hot: np.ndarray | None = None,
              bias: np.ndarray | None = None) -> np.ndarray:
    """Calibrate one raw frame to float32 in [0, 1].

    The mandatory order: `(raw - dark) / flat` -> hot pixel correction ->
    `/ meta.full_scale` -> clip [0, 1]. Demosaicing happens after this, on the
    caller's side, because the luminance path wants the mosaic and the display
    path wants RGB.

    **The dark and the bias are alternatives, never a sum.** A dark is taken at
    the exposure of the lights with the sensor capped, so it already contains
    the offset pedestal a bias measures; subtracting both removes it twice and
    the sky goes negative, which `np.maximum` then flattens into a black,
    signal-free floor. The bias is what serves when there is no dark of the
    right exposure — it removes the pedestal, not the thermal signal.
    """
    f = np.asarray(raw, dtype=np.float32)
    if f is raw:
        f = f.copy()
    pedestal = dark if dark is not None and dark.shape == f.shape else None
    if pedestal is None and bias is not None and bias.shape == f.shape:
        pedestal = bias
    if pedestal is not None:
        f -= pedestal
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


def prepare_flat(raw: np.ndarray, low: float = 0.15,
                 high: float = 4.0) -> np.ndarray | None:
    """A master flat as `calibrate` wants it: normalised to a median of 1.

    None when the flat carries no signal (median <= 0) — dividing by it would
    destroy the frame rather than correct it.

    The divisor is clamped: a flat pixel at 1% of the median would multiply the
    noise there by a hundred, and a dust shadow deep enough to do that is
    better left visible than amplified.
    """
    d = np.asarray(raw, dtype=np.float32)
    median = float(np.median(d))
    if median <= 0:
        return None
    return np.clip(d / median, low, high).astype(np.float32)


def flat_master(frames: list[np.ndarray],
                pedestal: np.ndarray | None = None) -> np.ndarray:
    """Median of the flat frames with the pedestal removed, floored at 1.

    Without the subtraction the sensor's offset enters a *multiplicative*
    correction: a pedestal of 500 ADU on a flat whose median is 20000 flattens
    the vignetting curve by 2.5% everywhere, and the corners come out
    over-corrected. Floored at 1 rather than 0 because the result is a divisor.
    """
    from .masters import combine
    master = combine(frames)
    if pedestal is not None and pedestal.shape == master.shape:
        master = np.maximum(master - pedestal, 1.0)
    return master.astype(np.float32)
