"""Autostretch para exibição — é isto que faz a nebulosa "aparecer" na tela.

Implementa a função de transferência de meios-tons (MTF) com estimativa
estatística de ponto preto, no espírito do STF do PixInsight. Em EAA este é o
momento decisivo: com 20 s de integração a imagem linear é praticamente preta,
e o stretch é o que revela o alvo.
"""
from __future__ import annotations

import numpy as np


def mtf(x: np.ndarray | float, m: float) -> np.ndarray:
    """Midtone transfer function. m=0.5 é a identidade; m<0.5 clareia."""
    x = np.asarray(x, dtype=np.float32)
    if abs(m - 0.5) < 1e-9:
        return np.clip(x, 0.0, 1.0)
    num = (m - 1.0) * x
    den = (2.0 * m - 1.0) * x - m
    with np.errstate(divide="ignore", invalid="ignore"):
        y = np.where(den == 0, 0.0, num / den)
    return np.clip(y, 0.0, 1.0).astype(np.float32)


def solve_midtone(x0: float, target: float) -> float:
    """m tal que mtf(x0, m) == target. Forma fechada.

    De ((m-1)x)/((2m-1)x - m) = t  segue  m = x(1-t) / (x - 2tx + t).
    """
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
    """Devolve (ponto_preto, midtone) para um canal em escala 0..1.

    O ponto preto sai da mediana menos 2,8 desvios robustos (MAD escalado), o
    que corta o fundo do céu sem comer a cauda de sinal fraco. O midtone é
    escolhido para levar a mediana a `target_bg`.
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


def autostretch(
    img: np.ndarray,
    target_bg: float = 0.25,
    shadows_clip: float = -2.8,
    linked: bool = False,
) -> np.ndarray:
    """Aplica autostretch a uma imagem float 0..1, mono ou (h, w, 3).

    linked=False trata cada canal em separado, o que neutraliza o fundo
    automaticamente — desejável sob poluição luminosa, onde o gradiente é
    diferente em cada canal. linked=True preserva as razões de cor originais.
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
            out[..., k] = mtf(np.clip((img[..., k] - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0), m)
    else:
        for k in range(img.shape[2]):
            c0, m = estimate_params(img[..., k], target_bg, shadows_clip)
            out[..., k] = mtf(np.clip((img[..., k] - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0), m)
    return out


def build_lut(c0: float, m: float, size: int = 65536) -> np.ndarray:
    """Tabela de 16 bits -> 8 bits que aplica o stretch de uma vez.

    Reaplicar o MTF a cada movimento de slider custa aritmética sobre milhões de
    pixels. Com a LUT, mudar o stretch é reconstruir 65536 entradas (µs) e fazer
    um gather sobre a imagem quantizada — dezenas de ms em vez de centenas.
    """
    x = np.linspace(0.0, 1.0, size, dtype=np.float32)
    return to_uint8(mtf(np.clip((x - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0), m))


def to_uint8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


# --------------------------------------------------------------------- arcsinh
def solve_strength(x0: float, target: float, lo: float = 0.1,
                   hi: float = 1e5) -> float:
    """`strength` tal que arcsinh leve x0 a `target`. Bissecção — a função é
    monotônica em strength, então converge sempre."""
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
    """Stretch arcsinh, alternativa ao MTF para alvos com núcleo brilhante.

    O MTF comprime as altas luzes com força: núcleo de M42, de globular ou de
    estrela brilhante estoura em branco e perde a cor. O arcsinh tem ganho alto
    no sinal fraco e vai ficando gentil no topo, o que preserva a cor.

    Com `preserve_color`, a curva é aplicada à **luminância** e os canais entram
    pela razão que têm com ela — as proporções de cor ficam exatamente
    intactas. É a construção de Lupton et al. usada nas imagens do SDSS.

    O ponto preto é estimado por canal antes disso, então o fundo fica neutro:
    sem esse passo, preservar as razões preservaria também o desvio de cor do
    fundo, e o céu sairia colorido.

    Erro que esta função já teve: subtrair um ponto preto derivado da luminância
    de cada canal. Nos canais abaixo da média o resultado ficava negativo, era
    multiplicado por um fator grande e grampeava em zero — imagem inteira
    vermelha.
    """
    a = np.asarray(img, dtype=np.float32)

    if a.ndim == 2:
        c0, _ = estimate_params(a, target_bg, shadows_clip, subsample)
        x = np.clip((a - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0)
        s = np.median(x[::subsample, ::subsample])
        st = solve_strength(max(float(s), 1e-6), target_bg)
        return np.clip(np.arcsinh(st * x) / np.arcsinh(st), 0, 1).astype(np.float32)

    # Ponto preto por canal, medido de forma robusta a sinal extenso.
    #
    # Usar a mediana global aqui não serve: uma nebulosa vermelha levanta a
    # mediana do R mais que a do B, o ponto preto sai enviesado de forma
    # diferente em cada canal, e o fundo herda um desvio de cor residual. A
    # amostragem por blocos com percentil baixo do módulo de gradiente mede o
    # piso do céu, que é o que o ponto preto deveria ser.
    from . import background as _bg
    b = np.empty_like(a)
    for k in range(a.shape[2]):
        ch = a[..., k]
        try:
            _, vals = _bg.sample_tiles(ch, grid=10)
            piso = float(np.median(vals)) if len(vals) >= 6 else None
        except Exception:
            piso = None
        if piso is None:
            c0, _ = estimate_params(ch, target_bg, shadows_clip, subsample)
        else:
            mad = float(np.median(np.abs(ch[::subsample, ::subsample] - piso))) * 1.4826
            c0 = float(np.clip(piso + shadows_clip * mad, 0.0, 1.0))
        b[..., k] = np.clip((ch - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0)

    lum = b.mean(axis=2)
    x0 = max(float(np.median(lum[::subsample, ::subsample])), 1e-6)
    st = solve_strength(x0, target_bg)

    if not preserve_color:
        return np.clip(np.arcsinh(st * b) / np.arcsinh(st), 0, 1).astype(np.float32)

    stretched = np.arcsinh(st * lum) / np.arcsinh(st)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(lum[..., None] > 1e-8, b / np.maximum(lum[..., None], 1e-8), 1.0)
    return np.clip(stretched[..., None] * ratio, 0.0, 1.0).astype(np.float32)


def saturate(img: np.ndarray, amount: float = 1.0) -> np.ndarray:
    """Escala a croma em torno da luminância. amount=1 não muda nada.

    Depois do stretch a nebulosa fica pálida porque esticar comprime a distância
    entre canais. Isto devolve a cor sem tocar o brilho.
    """
    a = np.asarray(img, dtype=np.float32)
    if a.ndim != 3 or abs(amount - 1.0) < 1e-6:
        return a
    lum = a.mean(axis=2, keepdims=True)
    return np.clip(lum + (a - lum) * amount, 0.0, 1.0).astype(np.float32)
