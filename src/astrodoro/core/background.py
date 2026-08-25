"""Background gradient extraction — the largest visual gain available.

Same idea as PixInsight's DBE and Siril's background extraction: fit a smooth
surface to samples that contain only sky and subtract it. What differs is the
scale of the problem — a fit in milliseconds over a live stack, with no
interaction.
"""
from __future__ import annotations

import numpy as np


def sample_tiles(img: np.ndarray, grid: int = 10, reject_sigma: float = 2.0,
                 mask: np.ndarray | None = None,
                 min_fraction: float = 0.25,
                 percentile: float = 25.0) -> tuple[np.ndarray, np.ndarray]:
    """Per-tile background estimate.

    Two defences against extended signal: a cut at
    `median + reject_sigma * MAD`, which removes stars; and then a *low
    percentile* of what remains rather than the median. The percentile matters
    because a 200 px nebula covers whole tiles, where the signal is not an
    outlier but a level shift that sigma clipping cannot see.

    Returns (points Nx2 in normalised coordinates, values N).
    """
    h, w = img.shape[:2]
    ys = np.linspace(0, h, grid + 1).astype(int)
    xs = np.linspace(0, w, grid + 1).astype(int)
    pts, vals = [], []
    for i in range(grid):
        for j in range(grid):
            tile = img[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
            if tile.size == 0:
                continue
            if mask is not None:
                m = mask[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
                tile = tile[m]
                if tile.size < 16:
                    continue
            t = tile.ravel()
            t = t[np.isfinite(t)]
            if t.size < 16:
                continue
            med = np.median(t)
            mad = np.median(np.abs(t - med)) * 1.4826
            keep = t <= med + reject_sigma * max(mad, 1e-9)
            if keep.mean() < min_fraction:
                # Tile dominated by signal (nebula core, large galaxy): not a
                # usable background sample.
                continue
            pts.append(((xs[j] + xs[j + 1]) / 2.0 / w,
                        (ys[i] + ys[i + 1]) / 2.0 / h))
            vals.append(float(np.percentile(t[keep], percentile)))
    return np.asarray(pts, dtype=np.float64), np.asarray(vals, dtype=np.float64)


def _design(x: np.ndarray, y: np.ndarray, degree: int) -> np.ndarray:
    cols = [np.ones_like(x)]
    for d in range(1, degree + 1):
        for k in range(d + 1):
            cols.append((x ** (d - k)) * (y ** k))
    return np.column_stack(cols)


def fit_surface(img: np.ndarray, degree: int = 1, grid: int = 10,
                mask: np.ndarray | None = None) -> np.ndarray | None:
    """Polynomial surface fitted to the background, same size as the input.

    Degree 1 corrects tilt (light pollution from one side). Degree 2 catches amp
    glow, which is a blob in a corner. Above 2 the fit starts eating extended
    nebulosity, which in EAA is worse than the gradient.
    """
    pts, vals = sample_tiles(img, grid=grid, mask=mask)
    n_terms = (degree + 1) * (degree + 2) // 2
    if len(vals) < n_terms + 3:
        return None
    A = _design(pts[:, 0], pts[:, 1], degree)

    # Iterative fit to the LOWER envelope of the samples: rejection is
    # one-sided, because anything above the model is signal (nebula, galaxy,
    # halo) and never sky. A plain least-squares fit is pulled upwards by that
    # signal, and the higher the degree the better it tracks the nebula — which
    # is why degree 2 used to make the result worse.
    keep = np.ones(len(vals), dtype=bool)
    coef = None
    for _ in range(5):
        c, *_rest = np.linalg.lstsq(A[keep], vals[keep], rcond=None)
        coef = c
        resid = vals - A @ c
        r = resid[keep]
        med_r = np.median(r)
        sigma = np.median(np.abs(r - med_r)) * 1.4826
        if sigma <= 0:
            break
        fresh = resid < med_r + 1.0 * sigma
        if fresh.sum() < n_terms + 3 or np.array_equal(fresh, keep):
            break
        keep = fresh
    if coef is None:
        return None

    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    xn = (xx + 0.5) / w
    yn = (yy + 0.5) / h
    out = np.zeros((h, w), dtype=np.float32)
    k = 0
    out += coef[k]
    k += 1
    for d in range(1, degree + 1):
        for j in range(d + 1):
            out += coef[k] * (xn ** (d - j)) * (yn ** j)
            k += 1
    return out


def remove(img: np.ndarray, degree: int = 1, grid: int = 10,
           mask: np.ndarray | None = None,
           keep_level: bool = True) -> tuple[np.ndarray, bool]:
    """Subtract the gradient from each channel. Returns (image, success).

    `keep_level` puts the model's mean level back, so the result is not centred
    on zero — the autostretch needs a background pedestal to estimate median
    and MAD from.
    """
    a = np.asarray(img, dtype=np.float32)
    if a.ndim == 2:
        s = fit_surface(a, degree, grid, mask)
        if s is None:
            return a, False
        out = a - s + (float(np.median(s)) if keep_level else 0.0)
        return np.clip(out, 0.0, None), True

    out = np.empty_like(a)
    ok = False
    for c in range(a.shape[2]):
        s = fit_surface(a[..., c], degree, grid, mask)
        if s is None:
            out[..., c] = a[..., c]
            continue
        ok = True
        out[..., c] = a[..., c] - s + (float(np.median(s)) if keep_level else 0.0)
    return np.clip(out, 0.0, None), ok
