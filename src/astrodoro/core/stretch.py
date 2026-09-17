from __future__ import annotations

import numpy as np


def mtf(x: np.ndarray | float, m: float) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if abs(m - 0.5) < 1e-9:
        return np.clip(x, 0.0, 1.0)
    num = (m - 1.0) * x
    den = (2.0 * m - 1.0) * x - m
    with np.errstate(divide="ignore", invalid="ignore"):
        y = np.where(den == 0, 0.0, num / den)
    return np.clip(y, 0.0, 1.0).astype(np.float32)


def solve_midtone(x0: float, target: float) -> float:
    den = x0 - 2.0 * target * x0 + target
    if den <= 0:
        return 0.5
    return float(np.clip(x0 * (1.0 - target) / den, 1e-6, 0.5))


def estimate_params(
    v: np.ndarray,
    target_bg: float = 0.25,
    shadows_clip: float = -2.8,
    subsample: int = 4,
) -> tuple[float, float]:
    s = v[::subsample, ::subsample]
    s = s[np.isfinite(s)]
    if s.size == 0:
        return 0.0, 0.5
    med = float(np.median(s))
    mad = float(np.median(np.abs(s - med))) * 1.4826
    if mad <= 0:
        mad = float(s.std()) or 1e-6
    c0 = float(np.clip(med + shadows_clip * mad, 0.0, 1.0))
    x0 = max(med - c0, 1e-6)
    return c0, solve_midtone(x0, target_bg)


def autostretch(
    img: np.ndarray,
    target_bg: float = 0.25,
    shadows_clip: float = -2.8,
    linked: bool = False,
) -> np.ndarray:
    img = np.asarray(img, dtype=np.float32)
    if img.ndim == 2:
        c0, m = estimate_params(img, target_bg, shadows_clip)
        return mtf(np.clip((img - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0), m)

    out = np.empty_like(img)
    if linked:
        lum = img.mean(axis=2)
        c0, m = estimate_params(lum, target_bg, shadows_clip)
        for k in range(img.shape[2]):
            out[..., k] = mtf(
                np.clip((img[..., k] - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0), m
            )
    else:
        for k in range(img.shape[2]):
            c0, m = estimate_params(img[..., k], target_bg, shadows_clip)
            out[..., k] = mtf(
                np.clip((img[..., k] - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0), m
            )
    return out


def build_lut(
    c0: float, m: float, c1: float = 1.0, gain: float = 1.0, size: int = 65536
) -> np.ndarray:
    x = np.linspace(0.0, 1.0, size, dtype=np.float32) * float(gain)
    span = max(float(c1) - float(c0), 1e-6)
    return to_uint8(mtf(np.clip((x - c0) / span, 0.0, 1.0), m))


def to_uint8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def solve_strength(x0: float, target: float, lo: float = 0.1, hi: float = 1e5) -> float:
    if x0 <= 0:
        return 100.0

    def f(s):
        return float(np.arcsinh(s * x0) / np.arcsinh(s))

    if f(lo) > target:
        return lo
    for _ in range(60):
        mid = float(np.sqrt(lo * hi))
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
    return float(np.sqrt(lo * hi))


def auto_arcsinh(
    img: np.ndarray,
    target_bg: float = 0.25,
    shadows_clip: float = -2.8,
    preserve_color: bool = True,
    subsample: int = 4,
) -> np.ndarray:
    a = np.asarray(img, dtype=np.float32)

    if a.ndim == 2:
        c0, _m = estimate_params(a, target_bg, shadows_clip, subsample)
        x = np.clip((a - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0)
        s = np.median(x[::subsample, ::subsample])
        st = solve_strength(max(float(s), 1e-6), target_bg)
        return np.clip(np.arcsinh(st * x) / np.arcsinh(st), 0, 1).astype(np.float32)

    from . import background as _bg

    b = np.empty_like(a)
    for k in range(a.shape[2]):
        ch = a[..., k]
        try:
            _pts, vals = _bg.sample_tiles(ch, grid=10)
            floor = float(np.median(vals)) if len(vals) >= 6 else None
        except Exception:
            floor = None
        if floor is None:
            c0, _m = estimate_params(ch, target_bg, shadows_clip, subsample)
        else:
            mad = (
                float(np.median(np.abs(ch[::subsample, ::subsample] - floor))) * 1.4826
            )
            c0 = float(np.clip(floor + shadows_clip * mad, 0.0, 1.0))
        b[..., k] = np.clip((ch - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0)

    lum = b.mean(axis=2)
    x0 = max(float(np.median(lum[::subsample, ::subsample])), 1e-6)
    st = solve_strength(x0, target_bg)

    if not preserve_color:
        return np.clip(np.arcsinh(st * b) / np.arcsinh(st), 0, 1).astype(np.float32)

    stretched = np.arcsinh(st * lum) / np.arcsinh(st)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(
            lum[..., None] > 1e-8, b / np.maximum(lum[..., None], 1e-8), 1.0
        )
    return np.clip(stretched[..., None] * ratio, 0.0, 1.0).astype(np.float32)


def saturate(img: np.ndarray, amount: float = 1.0) -> np.ndarray:
    a = np.asarray(img, dtype=np.float32)
    if a.ndim != 3 or abs(amount - 1.0) < 1e-6:
        return a
    lum = a.mean(axis=2, keepdims=True)
    return np.clip(lum + (a - lum) * amount, 0.0, 1.0).astype(np.float32)


def saturate_u8(img: np.ndarray, amount: float = 1.0) -> np.ndarray:
    a = np.asarray(img)
    if a.ndim != 3 or abs(amount - 1.0) < 1e-6:
        return a
    x = a.astype(np.int32)
    lum = ((x[..., 0] + x[..., 1] + x[..., 2]) // 3)[..., None]
    k = int(round(amount * 256))
    return np.clip(lum + (((x - lum) * k) >> 8), 0, 255).astype(np.uint8)
