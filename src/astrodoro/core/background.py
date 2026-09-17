from __future__ import annotations

import numpy as np


def sample_tiles(
    img: np.ndarray,
    grid: int = 10,
    reject_sigma: float = 2.0,
    mask: np.ndarray | None = None,
    min_fraction: float = 0.25,
    percentile: float = 25.0,
) -> tuple[np.ndarray, np.ndarray]:
    h, w = img.shape[:2]
    ys = np.linspace(0, h, grid + 1).astype(int)
    xs = np.linspace(0, w, grid + 1).astype(int)
    pts, vals = [], []
    for i in range(grid):
        for j in range(grid):
            tile = img[ys[i] : ys[i + 1], xs[j] : xs[j + 1]]
            if tile.size == 0:
                continue
            if mask is not None:
                m = mask[ys[i] : ys[i + 1], xs[j] : xs[j + 1]]
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
                continue
            pts.append(((xs[j] + xs[j + 1]) / 2.0 / w, (ys[i] + ys[i + 1]) / 2.0 / h))
            vals.append(float(np.percentile(t[keep], percentile)))
    return np.asarray(pts, dtype=np.float64), np.asarray(vals, dtype=np.float64)
