"""Frame-to-reference registration by asterism matching."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..i18n import gettext as _


@dataclass
class Alignment:
    matrix: np.ndarray | None    # (2, 3) similarity affine, src -> ref
    n_matched: int
    rms: float                   # residual error in pixels
    rotation_deg: float
    scale: float
    shift: tuple[float, float]
    reason: str = ""
    method: str = "asterisms"

    @property
    def ok(self) -> bool:
        return self.matrix is not None


def estimate(src_xy: np.ndarray, ref_xy: np.ndarray, min_matched: int = 8,
             max_rms: float = 2.0, vote_tolerance: float = 3.5) -> Alignment:
    """Estimate the similarity taking `src_xy` into `ref_xy`'s frame.

    Two layers, in this order:

    1. **Asterisms** (astroalign): matches triangles by side-ratio invariants,
       robust to arbitrary translation, rotation and scale. This is what
       survives the jump of hundreds of pixels when you push a Dobsonian.

    2. **Translation voting**, when the first fails. astroalign needs a few
       dozen stars to converge; with 8 to 14 it raises MaxIterError. And that
       is exactly the frame thin cloud, moonlight or a poor field produces,
       often with excellent FWHM. Throwing those away would lose a large part
       of the session.

       Voting walks every translation implied by a (frame star, reference star)
       pair, counts how many stars match under each, and takes the winner. It
       works with 3 or 4 stars. A similarity is then fitted over the matched
       pairs, which recovers the rotation too.
    """
    if len(src_xy) < 3 or len(ref_xy) < 3:
        return _fail(_("not enough stars"), len(src_xy))

    try:
        import astroalign
        _transform, (m_src, m_ref) = astroalign.find_transform(src_xy, ref_xy)
        al = _from_pairs(np.asarray(m_src), np.asarray(m_ref),
                         min_matched, max_rms, "asterisms")
        if al.ok:
            return al
        first = al.reason
    except Exception as e:
        first = f"astroalign: {type(e).__name__}"

    al = _vote_translation(src_xy, ref_xy, vote_tolerance, min_matched, max_rms)
    if al.ok:
        return al
    return _fail(_("{first}; voting: {second}").format(first=first,
                                                       second=al.reason),
                 al.n_matched)


def _vote_translation(src_xy, ref_xy, tol, min_matched, max_rms) -> Alignment:
    """Translation by voting, then a similarity over the matched pairs."""
    from scipy.spatial import cKDTree

    src = np.asarray(src_xy, dtype=np.float64)
    ref = np.asarray(ref_xy, dtype=np.float64)
    # Candidates: every (reference - frame) difference. With 15x35 that is 525,
    # and each one costs a nearest-neighbour query — cheap.
    cands = (ref[None, :, :] - src[:, None, :]).reshape(-1, 2)
    if len(cands) > 20000:
        cands = cands[:: len(cands) // 20000 + 1]

    tree = cKDTree(ref)
    best_n, best = 0, None
    for c in cands:
        dist, idx = tree.query(src + c, distance_upper_bound=tol)
        ok = np.isfinite(dist)
        n = int(ok.sum())
        if n > best_n:
            best_n, best = n, (c, idx, ok)
    if best is None or best_n < 3:
        return _fail(_("only {n} stars voted together").format(n=best_n), best_n)

    _c, idx, ok = best
    # Drop duplicate matches (two frame stars onto the same reference star).
    pairs_src, pairs_ref, seen = [], [], set()
    for i in np.flatnonzero(ok):
        j = int(idx[i])
        if j in seen:
            continue
        seen.add(j)
        pairs_src.append(src[i])
        pairs_ref.append(ref[j])
    if len(pairs_src) < 3:
        return _fail(_("not enough pairs after removing duplicates"),
                     len(pairs_src))
    return _from_pairs(np.asarray(pairs_src), np.asarray(pairs_ref),
                       min(min_matched, 3), max_rms, "vote")


def _from_pairs(m_src, m_ref, min_matched, max_rms, method) -> Alignment:
    n = len(m_src)
    if n < min_matched:
        return _fail(_("only {n} pairs matched").format(n=n), n)
    M, inliers = cv2.estimateAffinePartial2D(
        m_src.reshape(-1, 1, 2).astype(np.float64),
        m_ref.reshape(-1, 1, 2).astype(np.float64),
        method=cv2.RANSAC, ransacReprojThreshold=max_rms, maxIters=3000)
    if M is None:
        return _fail(_("similarity fit failed"), n)
    M = np.asarray(M, dtype=np.float64)
    keep = (inliers.ravel() > 0) if inliers is not None else np.ones(n, bool)
    if keep.sum() < max(min_matched, 3):
        return _fail(_("only {n} inliers").format(n=int(keep.sum())),
                     int(keep.sum()))

    pred = (M[:, :2] @ m_src[keep].T).T + M[:, 2]
    resid = np.linalg.norm(pred - m_ref[keep], axis=1)
    rms = float(np.sqrt(np.mean(resid ** 2)))
    if rms > max_rms:
        return _fail(_("rms {rms:.2f} px above the limit").format(rms=rms),
                     int(keep.sum()))
    rot = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
    sc = float(np.hypot(M[0, 0], M[1, 0]))
    return Alignment(M, int(keep.sum()), rms, rot, sc,
                     (float(M[0, 2]), float(M[1, 2])), method=method)


def _fail(reason: str, n: int) -> Alignment:
    return Alignment(None, n, float("nan"), 0.0, 1.0, (0.0, 0.0), reason, "none")


def warp(img: np.ndarray, M: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Apply the transform. `M` maps src -> destination (forward direction).

    cv2.warpAffine without WARP_INVERSE_MAP treats M as the forward mapping
    from source to destination, which is exactly what `estimate` returns.
    Inverting it by mistake is the classic bug of this stage, so there is a
    dedicated test in tests/test_register_warp.py.
    """
    h, w = shape
    out = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    # cv2 drops a channel axis of size 1: (h,w,1) -> (h,w). That breaks
    # broadcasting against the accumulators on mono cameras.
    if img.ndim == 3 and out.ndim == 2:
        out = out[:, :, None]
    return out


def coverage_mask(shape: tuple[int, int], M: np.ndarray) -> np.ndarray:
    """Mask of where the transformed frame actually contributes.

    Without it the stack gains a dark frame around the edges, where only some
    of the frames overlap.
    """
    h, w = shape
    ones = np.ones((h, w), dtype=np.float32)
    return warp(ones, M, shape)
