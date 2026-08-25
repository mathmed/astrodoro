"""Display autostretch — this is what makes the nebula appear on screen.

Implements the midtone transfer function (MTF) with a statistical black-point
estimate, in the spirit of PixInsight's STF. With 20 s of integration the
linear image is essentially black; the stretch is what reveals the target.
"""
from __future__ import annotations

import numpy as np


def mtf(x: np.ndarray | float, m: float) -> np.ndarray:
    """Midtone transfer function. m=0.5 is the identity; m<0.5 brightens."""
    x = np.asarray(x, dtype=np.float32)
    if abs(m - 0.5) < 1e-9:
        return np.clip(x, 0.0, 1.0)
    num = (m - 1.0) * x
    den = (2.0 * m - 1.0) * x - m
    with np.errstate(divide="ignore", invalid="ignore"):
        y = np.where(den == 0, 0.0, num / den)
    return np.clip(y, 0.0, 1.0).astype(np.float32)


def solve_midtone(x0: float, target: float) -> float:
    """m such that mtf(x0, m) == target, in closed form.

    From ((m-1)x)/((2m-1)x - m) = t follows m = x(1-t) / (x - 2tx + t).
    """
    den = x0 - 2.0 * target * x0 + target
    if den <= 0:
        return 0.5
    return float(np.clip(x0 * (1.0 - target) / den, 1e-6, 0.5))


def estimate_params(v: np.ndarray, target_bg: float = 0.25,
                    shadows_clip: float = -2.8,
                    subsample: int = 4) -> tuple[float, float]:
    """Returns (black_point, midtone) for one channel on a 0..1 scale.

    The black point is the median minus 2.8 robust deviations (scaled MAD),
    which cuts the sky background without eating the faint-signal tail. The
    midtone is chosen to bring the median to `target_bg`.
    """
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


def autostretch(img: np.ndarray, target_bg: float = 0.25,
                shadows_clip: float = -2.8,
                linked: bool = False) -> np.ndarray:
    """Autostretch a float 0..1 image, mono or (h, w, 3).

    linked=False treats each channel separately, which neutralises the
    background automatically — desirable under light pollution, where the
    gradient differs per channel. linked=True preserves the original colour
    ratios.
    """
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
                np.clip((img[..., k] - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0), m)
    else:
        for k in range(img.shape[2]):
            c0, m = estimate_params(img[..., k], target_bg, shadows_clip)
            out[..., k] = mtf(
                np.clip((img[..., k] - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0), m)
    return out


def build_lut(c0: float, m: float, c1: float = 1.0, gain: float = 1.0,
              size: int = 65536) -> np.ndarray:
    """A 16-bit -> 8-bit table that applies the whole stretch in one gather.

    Re-applying the MTF on every slider move costs arithmetic over millions of
    pixels. With the LUT, changing the stretch means rebuilding 65536 entries
    (microseconds) plus a gather over the quantised image: tens of milliseconds
    instead of hundreds.

    `c1` is the white point: the value that comes out as 255. Lowering it
    saturates the highlights deliberately, which is how faint nebulosity is
    brought up without waiting for the core to behave.

    `gain` multiplies the signal *before* the shadow clip, like a white
    balance: that is how per-channel gain enters through the LUT without
    touching the image.
    """
    x = np.linspace(0.0, 1.0, size, dtype=np.float32) * float(gain)
    span = max(float(c1) - float(c0), 1e-6)
    return to_uint8(mtf(np.clip((x - c0) / span, 0.0, 1.0), m))


def to_uint8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


# --------------------------------------------------------------------- arcsinh
def solve_strength(x0: float, target: float, lo: float = 0.1,
                   hi: float = 1e5) -> float:
    """`strength` such that arcsinh maps x0 to `target`.

    Bisection: the function is monotonic in strength, so it always converges.
    """
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


def auto_arcsinh(img: np.ndarray, target_bg: float = 0.25,
                 shadows_clip: float = -2.8, preserve_color: bool = True,
                 subsample: int = 4) -> np.ndarray:
    """Arcsinh stretch, an alternative to the MTF for bright-cored targets.

    The MTF compresses highlights hard: the core of M42, of a globular or of a
    bright star blows out to white and loses colour. Arcsinh has high gain on
    faint signal and grows gentle at the top, which preserves colour.

    With `preserve_color`, the curve is applied to the *luminance* and the
    channels follow by their ratio to it, leaving colour proportions exactly
    intact — the Lupton et al. construction used for the SDSS images.

    The black point is estimated per channel first, so the background stays
    neutral. Without that step, preserving ratios would also preserve the
    background's colour cast.
    """
    a = np.asarray(img, dtype=np.float32)

    if a.ndim == 2:
        c0, _m = estimate_params(a, target_bg, shadows_clip, subsample)
        x = np.clip((a - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0)
        s = np.median(x[::subsample, ::subsample])
        st = solve_strength(max(float(s), 1e-6), target_bg)
        return np.clip(np.arcsinh(st * x) / np.arcsinh(st), 0, 1).astype(np.float32)

    # Per-channel black point, measured robustly against extended signal.
    #
    # The global median will not do: a red nebula lifts the R median more than
    # the B one, the black point comes out biased differently per channel, and
    # the background inherits a residual colour cast. Tile sampling with a low
    # percentile measures the floor of the sky, which is what the black point
    # should be.
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
            mad = float(np.median(
                np.abs(ch[::subsample, ::subsample] - floor))) * 1.4826
            c0 = float(np.clip(floor + shadows_clip * mad, 0.0, 1.0))
        b[..., k] = np.clip((ch - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0)

    lum = b.mean(axis=2)
    x0 = max(float(np.median(lum[::subsample, ::subsample])), 1e-6)
    st = solve_strength(x0, target_bg)

    if not preserve_color:
        return np.clip(np.arcsinh(st * b) / np.arcsinh(st), 0, 1).astype(np.float32)

    stretched = np.arcsinh(st * lum) / np.arcsinh(st)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(lum[..., None] > 1e-8,
                         b / np.maximum(lum[..., None], 1e-8), 1.0)
    return np.clip(stretched[..., None] * ratio, 0.0, 1.0).astype(np.float32)


def saturate(img: np.ndarray, amount: float = 1.0) -> np.ndarray:
    """Scale chroma around the luminance. amount=1 changes nothing.

    After a stretch the nebula looks pale because stretching compresses the
    distance between channels. This gives the colour back without touching
    brightness.
    """
    a = np.asarray(img, dtype=np.float32)
    if a.ndim != 3 or abs(amount - 1.0) < 1e-6:
        return a
    lum = a.mean(axis=2, keepdims=True)
    return np.clip(lum + (a - lum) * amount, 0.0, 1.0).astype(np.float32)
