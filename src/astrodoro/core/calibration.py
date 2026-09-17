from __future__ import annotations

from typing import Protocol

import numpy as np


class Scaled(Protocol):
    """All `calibrate` needs of a frame's metadata."""

    full_scale: int


def calibrate(
    raw: np.ndarray,
    meta: Scaled,
    dark: np.ndarray | None = None,
    flat: np.ndarray | None = None,
    hot: np.ndarray | None = None,
    bias: np.ndarray | None = None,
) -> np.ndarray:
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
    med = float(np.median(master_dark))
    mad = float(np.median(np.abs(master_dark - med))) * 1.4826
    return master_dark > med + sigma * max(mad, 1.0)


def prepare_flat(
    raw: np.ndarray, low: float = 0.15, high: float = 4.0
) -> np.ndarray | None:
    d = np.asarray(raw, dtype=np.float32)
    median = float(np.median(d))
    if median <= 0:
        return None
    return np.clip(d / median, low, high).astype(np.float32)


def flat_master(
    frames: list[np.ndarray], pedestal: np.ndarray | None = None
) -> np.ndarray:
    from .masters import combine

    master = combine(frames)
    if pedestal is not None and pedestal.shape == master.shape:
        master = np.maximum(master - pedestal, 1.0)
    return master.astype(np.float32)
