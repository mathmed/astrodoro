from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..i18n import gettext as _
from . import debayer, lucky
from .calibration import calibrate
from .focus import sharpness
from .recorder import read_fits
from .source import FrameMeta

DEFAULT_BEST = 0.25

LUM_SCALE = 2.0

CROP_BELOW = 0.5

SKY_PERCENTILE = 5.0


@dataclass
class Frame:
    path: Path
    index: int
    sharpness: float
    exposure: float
    measured: bool = False


@dataclass
class Result:
    stack: np.ndarray
    n_used: int
    n_total: int
    exposure_total: float
    background: np.ndarray | np.floating | None = None
    sharpen: float = 0.0
    shifts: list[tuple[float, float]] = field(default_factory=list)
    crop: lucky.Box | None = None
    reference: Path | None = None
    seconds: float = 0.0

    @property
    def max_shift(self) -> float:
        return max((abs(dx) + abs(dy) for dx, dy in self.shifts), default=0.0)


def subs(folder: str | Path) -> list[Path]:
    root = Path(folder)
    inner = root / "subs"
    if inner.is_dir():
        root = inner
    out: list[Path] = []
    for pattern in ("*.fits", "*.fit", "*.fts"):
        out.extend(root.glob(pattern))
    return sorted(set(out))


def rank(paths: list[Path], measure_missing: bool = True) -> list[Frame]:
    out: list[Frame] = []
    for p in paths:
        value, exposure, index, measured = _header_quality(p)
        if value is None and measure_missing:
            raw, hdr = read_fits(p)
            lum = debayer.cfa_to_luminance(_to_float(raw, hdr))
            value = sharpness(_focus_crop(lum))
            measured = True
        out.append(
            Frame(
                path=p,
                index=index,
                sharpness=float(value) if value is not None else 0.0,
                exposure=exposure,
                measured=measured,
            )
        )
    out.sort(key=lambda f: (-f.sharpness, f.index))
    return out


def background(
    img: np.ndarray, percentile: float = SKY_PERCENTILE
) -> np.ndarray | np.floating:
    a = np.asarray(img, np.float32)
    if a.ndim != 3:
        return np.float32(np.percentile(a, percentile))
    return np.array(
        [np.percentile(a[..., k], percentile) for k in range(a.shape[2])], np.float32
    )


def sharpen(img: np.ndarray, amount: float, radius: float = 2.0) -> np.ndarray:
    import cv2

    if amount <= 0:
        return img
    blur = cv2.GaussianBlur(img, (0, 0), radius)
    return np.clip(img + amount * (img - blur), 0.0, None)


def stack(
    folder: str | Path,
    best: float = DEFAULT_BEST,
    dark: np.ndarray | None = None,
    flat: np.ndarray | None = None,
    subtract_background: bool = True,
    sharpen_amount: float = 0.0,
    crop: str | int | None = "auto",
    progress=None,
) -> Result:
    t0 = time.perf_counter()
    paths = subs(folder)
    if not paths:
        raise RuntimeError(_("no FITS files in {path}").format(path=folder))
    ranked = rank(paths)
    keep = _cut(ranked, best)

    acc: np.ndarray | None = None
    ref: tuple[np.ndarray, np.ndarray] | None = None
    box: lucky.Box | None = None
    shifts: list[tuple[float, float]] = []
    exposure_total = 0.0
    used = 0

    for i, frame in enumerate(keep):
        raw, hdr = read_fits(frame.path)
        f = calibrate(raw, _meta(hdr, frame), dark=dark, flat=flat)
        lum = debayer.cfa_to_luminance(f)
        rgb = debayer.to_rgb(
            (f * 65535).astype(np.uint16), _bayer(hdr), quality="linear"
        ).astype(np.float32)
        rgb /= 65535.0

        if ref is None:
            box = _window(lum, crop)
            ref = _prepare(box.crop(lum) if box else lum)
            view = box.scaled(LUM_SCALE).crop(rgb) if box else rgb
            acc = np.zeros_like(view)
            dx = dy = 0.0
        elif box is not None:
            here = box.recentred(*lucky.centroid(lum), lum.shape)
            ddx, ddy = _offset(*ref, here.crop(lum))
            view = _shift(here.scaled(LUM_SCALE).crop(rgb), ddx, ddy)
            dx = (box.x - here.x) * LUM_SCALE + ddx
            dy = (box.y - here.y) * LUM_SCALE + ddy
        else:
            dx, dy = _offset(*ref, lum)
            view = _shift(rgb, dx, dy)

        if progress is not None:
            progress(i + 1, len(keep), frame.path.name)
        if acc is None or view.shape != acc.shape:
            continue

        acc += view
        used += 1
        shifts.append((dx, dy))
        exposure_total += frame.exposure

    if acc is None:
        raise ValueError(_("no frames to stack"))
    acc /= max(used, 1)
    pedestal = None
    if subtract_background:
        pedestal = background(acc)
        acc = np.clip(acc - pedestal, 0.0, None)
    if sharpen_amount > 0:
        acc = sharpen(acc, sharpen_amount)
    return Result(
        stack=acc,
        n_used=used,
        n_total=len(paths),
        exposure_total=exposure_total,
        shifts=shifts,
        reference=keep[0].path if keep else None,
        background=pedestal,
        sharpen=sharpen_amount,
        crop=box.scaled(LUM_SCALE) if box else None,
        seconds=time.perf_counter() - t0,
    )


def write(result: Result, path: str | Path, target: str = "") -> Path:
    from astropy.io import fits

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    hdr = fits.Header()
    hdr["NCOMBINE"] = (result.n_used, "frames averaged")
    hdr["NTOTAL"] = (result.n_total, "frames in the burst")
    hdr["EXPTOTAL"] = (round(result.exposure_total, 4), "s of exposure summed")
    hdr["ALIGNBY"] = ("phase correlation", "translation only")
    hdr["MAXSHIFT"] = (round(result.max_shift, 2), "px, worst frame")
    if result.background is not None:
        for k, name in enumerate(("BKGSUB_R", "BKGSUB_G", "BKGSUB_B")):
            hdr[name] = (
                round(float(np.atleast_1d(result.background)[k]), 6),
                "pedestal removed, fraction of full scale",
            )
    if result.sharpen:
        hdr["SHARPEN"] = (result.sharpen, "unsharp mask amount")
    if result.crop is not None:
        c = result.crop
        hdr["CROPX"] = (c.x, "px, window origin in the recorded frame")
        hdr["CROPY"] = (c.y, "px, window origin in the recorded frame")
    if target:
        hdr["OBJECT"] = target
    fits.PrimaryHDU(result.stack.transpose(2, 0, 1).astype(np.float32), hdr).writeto(
        p, overwrite=True
    )
    return p


def _window(lum: np.ndarray, crop: str | int | None) -> lucky.Box | None:
    if crop in (None, False, "full", "none"):
        return None
    side = None
    if not isinstance(crop, str):
        side = max(int(crop) // int(LUM_SCALE), 8)
    b = lucky.window(lum, side=side)
    if b is None:
        return None
    if side is None and b.w >= CROP_BELOW * min(lum.shape[0], lum.shape[1]):
        return None
    return b.recentred(*lucky.centroid(lum, b), lum.shape)


def _focus_crop(lum: np.ndarray) -> np.ndarray:
    b = lucky.window(lum)
    return b.crop(lum) if b is not None else lum


def _cut(ranked: list[Frame], best: float) -> list[Frame]:
    fraction = float(np.clip(best, 0.01, 1.0))
    return ranked[: max(int(round(len(ranked) * fraction)), 1)]


def _header_quality(path: Path):
    from astropy.io import fits

    with fits.open(path, memmap=False) as hdul:
        for hdu in hdul:
            h = hdu.header
            if "SHARPNS" in h or "EXPTIME" in h:
                return (
                    float(h["SHARPNS"]) if "SHARPNS" in h else None,
                    float(h.get("EXPTIME", 0.0)),
                    int(h.get("FRAMEIDX", 0)),
                    False,
                )
    return None, 0.0, 0, False


def _to_float(raw: np.ndarray, hdr) -> np.ndarray:
    scale = float(hdr.get("FULLSCAL", 65532)) or 65532.0
    return np.clip(np.asarray(raw, np.float32) / scale, 0.0, 1.0)


def _bayer(hdr):
    from ..drivers import Bayer

    return {"RGGB": Bayer.RG, "BGGR": Bayer.BG, "GRBG": Bayer.GR, "GBRG": Bayer.GB}.get(
        str(hdr.get("BAYERPAT", "GRBG")).strip().upper(), Bayer.GR
    )


def _meta(hdr, frame: Frame) -> FrameMeta:
    return FrameMeta(
        index=frame.index,
        timestamp=0.0,
        exposure=frame.exposure,
        gain=int(hdr.get("GAIN", 0)),
        offset=int(hdr.get("OFFSET", 0)),
        bin=int(hdr.get("XBINNING", 1)),
        full_scale=int(hdr.get("FULLSCAL", 65532)),
        bayer=_bayer(hdr),
        origin="file",
        path=str(frame.path),
    )


def _prepare(lum: np.ndarray):
    import cv2

    ref = np.ascontiguousarray(lum, dtype=np.float32)
    window = cv2.createHanningWindow((ref.shape[1], ref.shape[0]), cv2.CV_32F)
    return ref, window


def _offset(
    reference: np.ndarray, window: np.ndarray, lum: np.ndarray
) -> tuple[float, float]:
    import cv2

    if lum.shape != reference.shape:
        return 0.0, 0.0
    (dx, dy), _response = cv2.phaseCorrelate(
        reference, np.ascontiguousarray(lum, dtype=np.float32), window
    )
    return -dx * LUM_SCALE, -dy * LUM_SCALE


def _shift(rgb: np.ndarray, dx: float, dy: float) -> np.ndarray:
    import cv2

    m = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], np.float32)
    return cv2.warpAffine(
        rgb,
        m,
        (rgb.shape[1], rgb.shape[0]),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REPLICATE,
    )
