"""Bayer mosaic to RGB, and reduction to luminance for star detection."""
from __future__ import annotations

import cv2
import numpy as np

from ..drivers.svbony.sdk import Bayer

# Determined empirically on this camera (GRBG) by amplifying one channel at a
# time through WB_R/WB_B and checking which OpenCV code responded on the right
# channel. OpenCV's naming is shifted with respect to the sensor pattern.
_CV_CODE = {
    Bayer.RG: cv2.COLOR_BayerBG2RGB,
    Bayer.BG: cv2.COLOR_BayerRG2RGB,
    Bayer.GR: cv2.COLOR_BayerGB2RGB,   # <- SV405CC
    Bayer.GB: cv2.COLOR_BayerGR2RGB,
}

#: `cfa_to_luminance` SUMS the four positions of the Bayer quad, so the
#: luminance image reaches 4x the frame scale. Anything comparing levels there
#: — the detection saturation limit, for one — must scale by this.
LUM_SUM = 4.0


def to_rgb(cfa: np.ndarray, pattern: Bayer, quality: str = "vng") -> np.ndarray:
    """Demosaic. `cfa` is 2D uint16/uint8; returns (h, w, 3) in the same dtype.

    quality: "linear" (fast), "vng" (best on stars), "ea" (edge aware). VNG and
    EA reject uint16 in some OpenCV builds, so those fall back to linear.
    """
    code = _CV_CODE[pattern]
    if quality != "linear":
        suffix = {"vng": "_VNG", "ea": "_EA"}[quality]
        name = [k for k, v in vars(cv2).items()
                if isinstance(v, int) and v == code
                and k.startswith("COLOR_Bayer")]
        alt = getattr(cv2, name[0] + suffix, None) if name else None
        if alt is not None:
            try:
                return cv2.cvtColor(cfa, alt)
            except cv2.error:
                pass
    return cv2.cvtColor(cfa, code)


def cfa_to_luminance(cfa: np.ndarray) -> np.ndarray:
    """Sum each 2x2 quad of the mosaic -> luminance at half resolution.

    This is the right path for star detection on a colour sensor: it removes
    the CFA checkerboard (which inflates the estimated noise and produces false
    detections) without demosaicing. Each output pixel holds R+2G+B, and the
    centroid stays sub-pixel — about 0.5 px at full resolution, well under the
    seeing.

    Coordinates return to full resolution by multiplying by 2.
    """
    a = cfa.astype(np.float32)
    return a[0::2, 0::2] + a[0::2, 1::2] + a[1::2, 0::2] + a[1::2, 1::2]


def bin2_bayer_aware(cfa: np.ndarray) -> np.ndarray:
    """Bin 2x2 preserving the mosaic: sum same-colour pixels within each 4x4.

    Only needed to bin on the host instead of using the SDK's binning. The SDK
    already does this correctly (docs/hardware.md section 6), so this is kept
    for the case where host binning is preferable, and as a reference for what
    "Bayer-aware bin2" means.
    """
    a = cfa.astype(np.uint32)
    h, w = a.shape
    h, w = h - h % 4, w - w % 4
    a = a[:h, :w]
    out = np.empty((h // 2, w // 2), dtype=np.uint32)
    for dy in (0, 1):
        for dx in (0, 1):
            phase = a[dy::2, dx::2]
            out[dy::2, dx::2] = (phase[0::2, 0::2] + phase[0::2, 1::2]
                                 + phase[1::2, 0::2] + phase[1::2, 1::2])
    return np.clip(out, 0, 65535).astype(np.uint16)
