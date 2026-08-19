"""Mosaico Bayer -> RGB, e redução a luminância para detecção de estrelas."""
from __future__ import annotations

import cv2
import numpy as np

from svbony.sdk import Bayer

# Determinado empiricamente nesta câmera (GRBG): amplifiquei um canal por vez
# via WB_R/WB_B e verifiquei qual código do OpenCV respondia no canal certo.
# BayerGR2RGB troca R com B; BayerBG/BayerRG colapsam tudo em verde.
_CV_CODE = {
    Bayer.RG: cv2.COLOR_BayerBG2RGB,
    Bayer.BG: cv2.COLOR_BayerRG2RGB,
    Bayer.GR: cv2.COLOR_BayerGB2RGB,   # <- SV405CC
    Bayer.GB: cv2.COLOR_BayerGR2RGB,
}


def to_rgb(cfa: np.ndarray, pattern: Bayer, quality: str = "vng") -> np.ndarray:
    """Demosaico. `cfa` é 2D uint16/uint8; devolve (h, w, 3) no mesmo dtype.

    quality: "linear" (rápido), "vng" (melhor em estrelas), "ea" (bordas).
    VNG e EA não aceitam uint16 em algumas builds do OpenCV; caímos para
    linear nesses casos.
    """
    code = _CV_CODE[pattern]
    if quality != "linear":
        suffix = {"vng": "_VNG", "ea": "_EA"}[quality]
        name = [k for k, v in vars(cv2).items()
                if isinstance(v, int) and v == code and k.startswith("COLOR_Bayer")]
        alt = getattr(cv2, name[0] + suffix, None) if name else None
        if alt is not None:
            try:
                return cv2.cvtColor(cfa, alt)
            except cv2.error:
                pass
    return cv2.cvtColor(cfa, code)


# cfa_to_luminance SOMA as quatro posições da quadra Bayer, então a imagem de
# luminância vai até 4x a escala do frame. Quem for comparar níveis nela — o
# limite de saturação da detecção, por exemplo — precisa multiplicar por isto.
LUM_SUM = 4.0


def cfa_to_luminance(cfa: np.ndarray) -> np.ndarray:
    """Soma cada quadra 2x2 do mosaico -> luminância em meia resolução.

    Isto é o caminho certo para detecção de estrelas num sensor colorido:
    elimina o xadrez do CFA (que infla o ruído estimado e gera detecções
    falsas) sem precisar demosaicar. Cada pixel de saída contém R+2G+B, ou
    seja luminância real, e o centróide continua sendo sub-pixel — em
    resolução plena a precisão fica em ~0,5 px, muito abaixo do seeing.

    Coordenadas voltam para resolução plena multiplicando por 2.
    """
    a = cfa.astype(np.float32)
    return a[0::2, 0::2] + a[0::2, 1::2] + a[1::2, 0::2] + a[1::2, 1::2]


def bin2_bayer_aware(cfa: np.ndarray) -> np.ndarray:
    """Bin 2x2 preservando o mosaico: soma pixels de mesma cor dentro de 4x4.

    Só é necessário se quisermos binar por conta própria em vez de usar o bin
    da SDK. A SDK já faz isso corretamente (ver HARDWARE.md seção 6), então
    normalmente não usamos — fica aqui para quando o binning do host for
    preferível, e como referência do que "bin2 Bayer-aware" significa.
    """
    a = cfa.astype(np.uint32)
    h, w = a.shape
    h, w = h - h % 4, w - w % 4
    a = a[:h, :w]
    out = np.empty((h // 2, w // 2), dtype=np.uint32)
    for dy in (0, 1):
        for dx in (0, 1):
            # cada fase do CFA é somada dentro do seu próprio sub-grid
            ph = a[dy::2, dx::2]
            out[dy::2, dx::2] = (ph[0::2, 0::2] + ph[0::2, 1::2]
                                 + ph[1::2, 0::2] + ph[1::2, 1::2])
    return np.clip(out, 0, 65535).astype(np.uint16)
