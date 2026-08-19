"""Extração de gradiente de fundo — o maior ganho visual disponível.

O IMX294 tem amp glow, e mesmo com master dark sobra resíduo, porque o dark
nunca escala perfeitamente com temperatura e exposição. Some qualquer poluição
luminosa e você tem um gradiente. O autostretch amplifica exatamente isso: é o
que ele faz de melhor.

A ideia é a mesma do DBE do PixInsight e da extração de fundo do Siril — ajustar
uma superfície suave a amostras que contêm só céu e subtraí-la. O que muda aqui é
a escala do problema: ajuste em segundos sobre um stack ao vivo, sem interação.
"""
from __future__ import annotations

import numpy as np


def sample_tiles(img: np.ndarray, grid: int = 10, reject_sigma: float = 2.0,
                 mask: np.ndarray | None = None,
                 min_fraction: float = 0.25,
                 percentile: float = 25.0) -> tuple[np.ndarray, np.ndarray]:
    """Estimativa de fundo por bloco.

    Duas defesas contra sinal extenso:

    * corte em `median + reject_sigma * MAD`, que remove estrelas;
    * e depois um **percentil baixo** do que sobrou, não a mediana. Isto importa
      porque uma nebulosa de 200 px cobre blocos inteiros: dentro dela o sinal não
      é outlier, é um deslocamento de nível, e o sigma clip não vê. O percentil
      baixo aproxima o piso do bloco, que é o que o céu vale ali.

    Devolve (pontos Nx2 em coordenadas normalizadas, valores N).
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
                # bloco dominado por sinal (núcleo de nebulosa, galáxia grande):
                # não serve como amostra de fundo
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
    """Superfície polinomial ajustada ao fundo. Devolve imagem do mesmo tamanho.

    Grau 1 corrige inclinação (poluição luminosa de um lado). Grau 2 pega o amp
    glow, que é uma mancha num canto. Grau acima de 2 começa a comer nebulosa
    extensa, e em EAA isso é pior que o gradiente.
    """
    pts, vals = sample_tiles(img, grid=grid, mask=mask)
    n_terms = (degree + 1) * (degree + 2) // 2
    if len(vals) < n_terms + 3:
        return None
    A = _design(pts[:, 0], pts[:, 1], degree)

    # Ajuste iterativo à envoltória INFERIOR das amostras. Rejeição só de um
    # lado: o que fica acima do modelo é sinal (nebulosa, galáxia, halo), nunca
    # céu. Um ajuste de mínimos quadrados simples é puxado para cima por esse
    # sinal, e quanto maior o grau mais ele consegue seguir a nebulosa — foi por
    # isso que grau 2 chegava a piorar o resultado.
    keep = np.ones(len(vals), dtype=bool)
    coef = None
    for _ in range(5):
        c, *_ = np.linalg.lstsq(A[keep], vals[keep], rcond=None)
        coef = c
        resid = vals - A @ c
        r = resid[keep]
        med_r = np.median(r)
        sigma = np.median(np.abs(r - med_r)) * 1.4826
        if sigma <= 0:
            break
        novo = resid < med_r + 1.0 * sigma
        if novo.sum() < n_terms + 3 or np.array_equal(novo, keep):
            break
        keep = novo
    if coef is None:
        return None

    h, w = img.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    xn = (xx + 0.5) / w
    yn = (yy + 0.5) / h
    out = np.zeros((h, w), dtype=np.float32)
    k = 0
    out += coef[k]; k += 1
    for d in range(1, degree + 1):
        for j in range(d + 1):
            out += coef[k] * (xn ** (d - j)) * (yn ** j)
            k += 1
    return out


def remove(img: np.ndarray, degree: int = 1, grid: int = 10,
           mask: np.ndarray | None = None,
           keep_level: bool = True) -> tuple[np.ndarray, bool]:
    """Subtrai o gradiente de cada canal. Devolve (imagem, sucesso).

    `keep_level` recoloca o nível médio do modelo, para o resultado não ficar
    centrado em zero — o autostretch precisa de um pedestal de fundo para
    estimar mediana e MAD.
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
