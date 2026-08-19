"""Registro frame-a-referência por casamento de asterismos."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Alignment:
    matrix: np.ndarray | None    # (2, 3) afim de similaridade, src -> ref
    n_matched: int
    rms: float                   # erro residual em px
    rotation_deg: float
    scale: float
    shift: tuple[float, float]
    reason: str = ""
    method: str = "asterismos"

    @property
    def ok(self) -> bool:
        return self.matrix is not None


def estimate(
    src_xy: np.ndarray,
    ref_xy: np.ndarray,
    min_matched: int = 8,
    max_rms: float = 2.0,
    vote_tolerance: float = 3.5,
) -> Alignment:
    """Estima a similaridade que leva `src_xy` ao referencial de `ref_xy`.

    Duas camadas, nesta ordem:

    1. **Asterismos** (astroalign): casa triângulos por invariantes de razão de
       lados, robusto a translação, rotação e escala arbitrárias. É o que
       sobrevive ao salto de centenas de pixels quando você empurra o tubo de um
       dobsoniano sem goto.

    2. **Votação de translação**, quando o primeiro falha. O astroalign precisa de
       algumas dezenas de estrelas para convergir; com 8 a 14 ele estoura em
       MaxIterError. E é exatamente esse o frame que nuvem fina, luar ou campo
       pobre produzem — muitas vezes com FWHM ótimo. Jogar fora seria perder
       parte grande da sessão.

       A votação percorre todas as translações implicadas por pares (estrela do
       frame, estrela da referência), conta quantas estrelas casam sob cada uma, e
       toma a de mais votos. Funciona com 3 ou 4 estrelas. Depois ajusta uma
       similaridade sobre os pares casados, o que recupera também a rotação.
    """
    if len(src_xy) < 3 or len(ref_xy) < 3:
        return _fail("estrelas insuficientes", len(src_xy))

    try:
        import astroalign
        transform, (m_src, m_ref) = astroalign.find_transform(src_xy, ref_xy)
        al = _from_pairs(np.asarray(m_src), np.asarray(m_ref),
                        min_matched, max_rms, "asterismos")
        if al.ok:
            return al
        primeiro = al.reason
    except Exception as e:
        primeiro = f"astroalign: {type(e).__name__}"

    al = _vote_translation(src_xy, ref_xy, vote_tolerance, min_matched, max_rms)
    if al.ok:
        return al
    return _fail(f"{primeiro}; votação: {al.reason}", al.n_matched)


def _vote_translation(src_xy, ref_xy, tol, min_matched, max_rms) -> Alignment:
    """Translação por votação, depois similaridade sobre os pares casados."""
    from scipy.spatial import cKDTree

    src = np.asarray(src_xy, dtype=np.float64)
    ref = np.asarray(ref_xy, dtype=np.float64)
    # candidatas: toda diferença (referência - frame). Com 15x35 são 525, e cada
    # uma custa uma consulta de vizinho mais próximo — barato.
    cands = (ref[None, :, :] - src[:, None, :]).reshape(-1, 2)
    if len(cands) > 20000:
        cands = cands[:: len(cands) // 20000 + 1]

    tree = cKDTree(ref)
    melhor_n, melhor = 0, None
    for c in cands:
        dist, idx = tree.query(src + c, distance_upper_bound=tol)
        ok = np.isfinite(dist)
        n = int(ok.sum())
        if n > melhor_n:
            melhor_n, melhor = n, (c, idx, ok)
    if melhor is None or melhor_n < 3:
        return _fail(f"apenas {melhor_n} estrelas votaram junto", melhor_n)

    c, idx, ok = melhor
    # remove casamentos duplicados (duas do frame na mesma da referência)
    pares_src, pares_ref, vistos = [], [], set()
    for i in np.flatnonzero(ok):
        j = int(idx[i])
        if j in vistos:
            continue
        vistos.add(j)
        pares_src.append(src[i])
        pares_ref.append(ref[j])
    if len(pares_src) < 3:
        return _fail("pares insuficientes após remover duplicatas", len(pares_src))
    return _from_pairs(np.asarray(pares_src), np.asarray(pares_ref),
                       min(min_matched, 3), max_rms, "votação")


def _from_pairs(m_src, m_ref, min_matched, max_rms, method) -> Alignment:
    n = len(m_src)
    if n < min_matched:
        return _fail(f"apenas {n} pares casados", n)
    M, inliers = cv2.estimateAffinePartial2D(
        m_src.reshape(-1, 1, 2).astype(np.float64),
        m_ref.reshape(-1, 1, 2).astype(np.float64),
        method=cv2.RANSAC, ransacReprojThreshold=max_rms, maxIters=3000)
    if M is None:
        return _fail("ajuste de similaridade falhou", n)
    M = np.asarray(M, dtype=np.float64)
    keep = (inliers.ravel() > 0) if inliers is not None else np.ones(n, bool)
    if keep.sum() < max(min_matched, 3):
        return _fail(f"apenas {int(keep.sum())} inliers", int(keep.sum()))

    pred = (M[:, :2] @ m_src[keep].T).T + M[:, 2]
    resid = np.linalg.norm(pred - m_ref[keep], axis=1)
    rms = float(np.sqrt(np.mean(resid ** 2)))
    if rms > max_rms:
        return _fail(f"rms {rms:.2f} px acima do limite", int(keep.sum()))
    rot = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
    sc = float(np.hypot(M[0, 0], M[1, 0]))
    return Alignment(M, int(keep.sum()), rms, rot, sc,
                     (float(M[0, 2]), float(M[1, 2])), method=method)


def _fail(reason: str, n: int) -> Alignment:
    return Alignment(None, n, float("nan"), 0.0, 1.0, (0.0, 0.0), reason, "nenhum")


def warp(img: np.ndarray, M: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Aplica a transformada. `M` mapeia src -> destino (sentido direto).

    cv2.warpAffine, sem WARP_INVERSE_MAP, trata M como mapeamento direto do
    source para o destino — que é exatamente o que `estimate` devolve. Inverter
    isso por engano é o bug clássico desta etapa, então há um autoteste em
    tests/test_warp_direction.py.
    """
    h, w = shape
    out = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    # cv2 descarta o eixo de canal quando ele tem tamanho 1: (h,w,1) -> (h,w).
    # Isso quebra o broadcast contra os acumuladores em cameras mono.
    if img.ndim == 3 and out.ndim == 2:
        out = out[:, :, None]
    return out


def coverage_mask(shape: tuple[int, int], M: np.ndarray) -> np.ndarray:
    """Máscara de onde o frame transformado realmente contribui.

    Sem isso o stack ganha uma moldura escura nas bordas, onde só parte dos
    frames se sobrepõe.
    """
    h, w = shape
    ones = np.ones((h, w), dtype=np.float32)
    return warp(ones, M, shape)
