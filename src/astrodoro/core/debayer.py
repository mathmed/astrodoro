from __future__ import annotations

import cv2
import numpy as np

from ..drivers import Bayer

_CV_CODE = {
    Bayer.RG: cv2.COLOR_BayerBG2RGB,
    Bayer.BG: cv2.COLOR_BayerRG2RGB,
    Bayer.GR: cv2.COLOR_BayerGB2RGB,
    Bayer.GB: cv2.COLOR_BayerGR2RGB,
}

LUM_SUM = 4.0


def to_rgb(cfa: np.ndarray, pattern: Bayer, quality: str = "vng") -> np.ndarray:
    code = _CV_CODE[pattern]
    if quality != "linear":
        suffix = {"vng": "_VNG", "ea": "_EA"}[quality]
        name = [
            k
            for k, v in vars(cv2).items()
            if isinstance(v, int) and v == code and k.startswith("COLOR_Bayer")
        ]
        alt = getattr(cv2, name[0] + suffix, None) if name else None
        if alt is not None:
            try:
                return cv2.cvtColor(cfa, alt)
            except cv2.error:
                pass
    return cv2.cvtColor(cfa, code)


def cfa_to_luminance(cfa: np.ndarray) -> np.ndarray:
    a = cfa.astype(np.float32)
    return a[0::2, 0::2] + a[0::2, 1::2] + a[1::2, 0::2] + a[1::2, 1::2]
