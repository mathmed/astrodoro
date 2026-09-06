"""Stacking a burst of the Moon or a planet, which the deep-sky stacker cannot do.

`LiveStacker` registers on asterisms: it matches patterns of stars between
frames. On a surface there are no point sources at all, so every frame falls at
`len(stars) < 3` and the whole burst is rejected — measured, not assumed. What a
surface needs instead is the other classical answer: pick the sharpest frames and
align them by cross-correlating the image with itself.

The four differences from the live path, and why each:

**It is offline.** Lucky imaging chooses frames by comparing them against each
other, which cannot be done while they arrive — the sharpest frame of the night
may be the last one. So this reads a finished burst.

**It ranks before it reads.** A bin1 burst is 23 MB a frame and can be tens of
gigabytes; nothing here may hold the set in memory. The first pass reads only
headers — `SHARPNS`, which the capture wrote — and the second reads only the
frames that survived the cut.

**It works on the body, not on the frame.** The Moon fills a third of a bin1
frame; Jupiter covers two hundredths of a percent of it. Correlating whole
frames of a planet correlates sky with sky, and averaging them averages mostly
sky — so a body smaller than half the frame is cropped to a window around it,
found on each frame by its own centroid before anything sub-pixel happens. The
Moon is left alone: there the frame *is* the body.

**Translation only.** Over the half minute of a burst neither the Moon nor a
planet rotates in the frame in any way a phase correlation would see, and the
residual rotation of an equatorial platform over 30 s is under a thousandth of a
degree. Fitting a rotation to that would be fitting noise.
"""
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

#: Fraction of a burst kept by default. The received wisdom of lucky imaging is
#: "the best 10 to 25 percent"; a quarter is the forgiving end of it, and on a
#: short burst under steady seeing throwing away more costs signal for nothing.
DEFAULT_BEST = 0.25

#: Alignment happens on the luminance, which `debayer.cfa_to_luminance` returns
#: at half resolution. Offsets measured there are in half-resolution pixels and
#: have to come back to full scale — the same `lum_scale` the live path uses,
#: and the same trap.
LUM_SCALE = 2.0

#: A body wider than this fraction of the frame's short side is not cropped to:
#: at that size the frame is the body, the sky around it is a rim, and cropping
#: would only throw away the limb the alignment needs. Below it — every planet,
#: at every focal length this program will see — the crop is the difference
#: between correlating a disc and correlating noise.
CROP_BELOW = 0.5

#: Percentile of the frame taken as sky when measuring the pedestal. The Moon
#: leaves most of the frame empty — 57% of it on a bin1 full disc — so a low
#: percentile is sky by construction, with no tiling and no mask that would have
#: to know where the disc is.
SKY_PERCENTILE = 5.0


@dataclass
class Frame:
    path: Path
    index: int
    sharpness: float
    exposure: float
    measured: bool = False          # sharpness read from the header, or computed


@dataclass
class Result:
    stack: np.ndarray               # float32 RGB in 0..1
    n_used: int
    n_total: int
    exposure_total: float
    #: Per-channel pedestal removed, or None when it was left in.
    background: np.ndarray | None = None
    sharpen: float = 0.0
    #: Per-frame (dx, dy) in full-resolution pixels, against the reference.
    shifts: list[tuple[float, float]] = field(default_factory=list)
    #: The window the stack was built in, in full-resolution pixels, or None
    #: when the whole frame was used.
    crop: lucky.Box | None = None
    reference: Path | None = None
    seconds: float = 0.0

    @property
    def max_shift(self) -> float:
        return max((abs(dx) + abs(dy) for dx, dy in self.shifts), default=0.0)


def subs(folder: str | Path) -> list[Path]:
    """The FITS of a session, whether or not they sit in a `subs/` directory."""
    root = Path(folder)
    inner = root / "subs"
    if inner.is_dir():
        root = inner
    out: list[Path] = []
    for pattern in ("*.fits", "*.fit", "*.fts"):
        out.extend(root.glob(pattern))
    return sorted(set(out))


def rank(paths: list[Path], measure_missing: bool = True) -> list[Frame]:
    """Sharpest first.

    `MOON` writes `SHARPNS` into every frame it records, so ranking a burst it
    produced costs one header read per file — kilobytes against gigabytes. A
    burst from anywhere else has no such key, and then there is no way around
    reading the pixels; `measure_missing=False` is for callers that would rather
    keep the capture order than pay for it.
    """
    out: list[Frame] = []
    for p in paths:
        value, exposure, index, measured = _header_quality(p)
        if value is None and measure_missing:
            raw, hdr = read_fits(p)
            lum = debayer.cfa_to_luminance(_to_float(raw, hdr))
            # On the body and not on the frame, the same as the capture does:
            # `sharpness` divides by the mean level, so measured over a frame
            # that is mostly sky it ranks the noise.
            value = sharpness(_focus_crop(lum))
            measured = True
        out.append(Frame(path=p, index=index,
                         sharpness=float(value) if value is not None else 0.0,
                         exposure=exposure, measured=measured))
    out.sort(key=lambda f: (-f.sharpness, f.index))
    return out


def background(img: np.ndarray,
               percentile: float = SKY_PERCENTILE) -> np.ndarray:
    """The additive pedestal under the whole frame, per channel.

    Two things sit under every pixel and neither is Moon: the sensor's offset,
    and light scattered in the sky and the tube. Both are additive, so until
    they come off no colour ratio in the frame means anything — measured on a
    real eclipse, removing 2% of full scale took the umbra from R/G 1.28 to
    1.36, a third of the eclipse's colour that was being diluted by a veil.

    Per channel, not one number: the offset is common but the scattered light
    and the channel responses are not, and it is exactly that difference the
    subtraction is meant to recover.

    A constant and not a fitted surface. Measured across the corners of the same
    frame the pedestal varied by 11% of itself, which is 0.2% of the disc — a
    plane fit would be modelling something two orders of magnitude below the
    signal. A flat is the right fix for that, before the fact.
    """
    a = np.asarray(img, np.float32)
    if a.ndim != 3:
        return np.float32(np.percentile(a, percentile))
    return np.array([np.percentile(a[..., k], percentile)
                     for k in range(a.shape[2])], np.float32)


def sharpen(img: np.ndarray, amount: float, radius: float = 2.0) -> np.ndarray:
    """Unsharp mask. Off by default, because it is a choice and not a correction.

    There is room for it: at 0.796"/px this camera oversamples any seeing the
    site delivers, so the detail is blurred rather than missing. Two places it
    lies, both visible before they are subtle — a clipped highlight sharpens
    into a dark ring around itself, and the umbral edge of an eclipse is
    genuinely diffuse, so pushing it manufactures an edge the sky does not have.
    """
    import cv2

    if amount <= 0:
        return img
    blur = cv2.GaussianBlur(img, (0, 0), radius)
    return np.clip(img + amount * (img - blur), 0.0, None)


def stack(folder: str | Path, best: float = DEFAULT_BEST,
          dark: np.ndarray | None = None, flat: np.ndarray | None = None,
          subtract_background: bool = True, sharpen_amount: float = 0.0,
          crop: str | int | None = "auto", progress=None) -> Result:
    """Align and average the sharpest `best` fraction of a burst.

    The background comes off the finished stack rather than off each frame: it
    is the same constant either way, and measuring it once on an image with a
    tenth of the noise is the more stable of the two.

    `crop` is "auto" — a window around the body, unless the body is most of the
    frame — or "full" for the whole frame, or a width in full-resolution pixels.
    The window's *size* is fixed by the reference frame and only its centre
    moves afterwards: every frame has to contribute the same pixels to the same
    accumulator, and a window that breathed with the seeing would resample the
    stack instead of aligning it.

    `progress(done, total, message)` is called once per frame, for a caller that
    wants to print a line; it is not required.
    """
    t0 = time.perf_counter()
    paths = subs(folder)
    if not paths:
        raise RuntimeError(_("no FITS files in {path}").format(path=folder))
    ranked = rank(paths)
    keep = _cut(ranked, best)

    acc: np.ndarray | None = None
    reference: np.ndarray | None = None
    hann: np.ndarray | None = None
    box: lucky.Box | None = None
    shifts: list[tuple[float, float]] = []
    exposure_total = 0.0
    used = 0

    for i, frame in enumerate(keep):
        raw, hdr = read_fits(frame.path)
        f = calibrate(raw, _meta(hdr, frame), dark=dark, flat=flat)
        lum = debayer.cfa_to_luminance(f)
        rgb = debayer.to_rgb((f * 65535).astype(np.uint16),
                             _bayer(hdr), quality="linear").astype(np.float32)
        rgb /= 65535.0

        if reference is None:
            # The sharpest frame is the reference, which is also why ranking
            # comes first: aligning everything to a soft frame would spend the
            # burst's best detail correcting for the worst frame's blur. It is
            # also what the window's size is taken from, for the same reason.
            box = _window(lum, crop)
            reference, hann = _prepare(box.crop(lum) if box else lum)
            view = box.scaled(LUM_SCALE).crop(rgb) if box else rgb
            acc = np.zeros_like(view)
            dx = dy = 0.0
        elif box is not None:
            # Coarse by centroid, fine by correlation. The body drifts across
            # the frame over a burst — on an equatorial platform by a few
            # hundred pixels — and a phase correlation only sees a shift small
            # against the window it is measured in.
            here = box.recentred(*lucky.centroid(lum), lum.shape)
            ddx, ddy = _offset(reference, hann, here.crop(lum))
            view = _shift(here.scaled(LUM_SCALE).crop(rgb), ddx, ddy)
            dx = (box.x - here.x) * LUM_SCALE + ddx
            dy = (box.y - here.y) * LUM_SCALE + ddy
        else:
            dx, dy = _offset(reference, hann, lum)
            view = _shift(rgb, dx, dy)

        if progress is not None:
            progress(i + 1, len(keep), frame.path.name)
        # A frame recorded at another binning, or one whose body sits so close
        # to the edge that the window no longer fits: it cannot be added to
        # this accumulator, and dropping it is the only honest answer.
        if view.shape != acc.shape:
            continue

        acc += view
        used += 1
        shifts.append((dx, dy))
        exposure_total += frame.exposure

    acc /= max(used, 1)
    pedestal = None
    if subtract_background:
        pedestal = background(acc)
        acc = np.clip(acc - pedestal, 0.0, None)
    if sharpen_amount > 0:
        acc = sharpen(acc, sharpen_amount)
    return Result(stack=acc, n_used=used, n_total=len(paths),
                  exposure_total=exposure_total, shifts=shifts,
                  reference=keep[0].path if keep else None,
                  background=pedestal, sharpen=sharpen_amount,
                  crop=box.scaled(LUM_SCALE) if box else None,
                  seconds=time.perf_counter() - t0)


def write(result: Result, path: str | Path, target: str = "") -> Path:
    """The stack as float32 RGB FITS, in the layout the rest of the program uses."""
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
            hdr[name] = (round(float(np.atleast_1d(result.background)[k]), 6),
                         "pedestal removed, fraction of full scale")
    if result.sharpen:
        hdr["SHARPEN"] = (result.sharpen, "unsharp mask amount")
    if result.crop is not None:
        c = result.crop
        hdr["CROPX"] = (c.x, "px, window origin in the recorded frame")
        hdr["CROPY"] = (c.y, "px, window origin in the recorded frame")
    if target:
        hdr["OBJECT"] = target
    fits.PrimaryHDU(result.stack.transpose(2, 0, 1).astype(np.float32),
                    hdr).writeto(p, overwrite=True)
    return p


# ------------------------------------------------------------------- internals
def _window(lum: np.ndarray, crop: str | int | None) -> lucky.Box | None:
    """The window this stack will be built in, or None for the whole frame.

    Centred on the centroid rather than on the bright extent `lucky.window`
    returns: every later frame is centred that way too, and a reference centred
    by a different rule would hand every one of them the same constant offset to
    correlate away.
    """
    if crop in (None, False, "full", "none"):
        return None
    side = None
    if not isinstance(crop, str):
        # Given in full-resolution pixels, measured in luminance ones.
        side = max(int(crop) // int(LUM_SCALE), 8)
    b = lucky.window(lum, side=side)
    if b is None:
        return None
    if side is None and b.w >= CROP_BELOW * min(lum.shape[0], lum.shape[1]):
        return None
    return b.recentred(*lucky.centroid(lum, b), lum.shape)


def _focus_crop(lum: np.ndarray) -> np.ndarray:
    """The body alone, for a sharpness that means the body's sharpness."""
    b = lucky.window(lum)
    return b.crop(lum) if b is not None else lum


def _cut(ranked: list[Frame], best: float) -> list[Frame]:
    """The sharpest slice, never empty and never a single frame by accident."""
    fraction = float(np.clip(best, 0.01, 1.0))
    return ranked[:max(int(round(len(ranked) * fraction)), 1)]


def _header_quality(path: Path):
    """(sharpness, exposure, index, measured) from the header alone."""
    from astropy.io import fits

    with fits.open(path, memmap=False) as hdul:
        for hdu in hdul:
            h = hdu.header
            if "SHARPNS" in h or "EXPTIME" in h:
                return (float(h["SHARPNS"]) if "SHARPNS" in h else None,
                        float(h.get("EXPTIME", 0.0)),
                        int(h.get("FRAMEIDX", 0)), False)
    return None, 0.0, 0, False


def _to_float(raw: np.ndarray, hdr) -> np.ndarray:
    scale = float(hdr.get("FULLSCAL", 65532)) or 65532.0
    return np.clip(np.asarray(raw, np.float32) / scale, 0.0, 1.0)


def _bayer(hdr):
    from ..drivers.svbony.sdk import Bayer
    return {"RGGB": Bayer.RG, "BGGR": Bayer.BG,
            "GRBG": Bayer.GR, "GBRG": Bayer.GB}.get(
                str(hdr.get("BAYERPAT", "GRBG")).strip().upper(), Bayer.GR)


def _meta(hdr, frame: Frame) -> FrameMeta:
    return FrameMeta(index=frame.index, timestamp=0.0,
                     exposure=frame.exposure, gain=int(hdr.get("GAIN", 0)),
                     offset=int(hdr.get("OFFSET", 0)),
                     bin=int(hdr.get("XBINNING", 1)),
                     full_scale=int(hdr.get("FULLSCAL", 65532)),
                     bayer=_bayer(hdr), origin="file", path=str(frame.path))


def _prepare(lum: np.ndarray):
    """The reference and its window, both float32 and both computed once.

    The window is a Hann taper. Without it the correlation sees the frame's
    straight edges as the strongest feature in the image and locks onto them,
    which on a disc against black sky means locking onto nothing.
    """
    import cv2

    ref = np.ascontiguousarray(lum, dtype=np.float32)
    window = cv2.createHanningWindow((ref.shape[1], ref.shape[0]), cv2.CV_32F)
    return ref, window


def _offset(reference: np.ndarray, window: np.ndarray,
            lum: np.ndarray) -> tuple[float, float]:
    """Sub-pixel shift of `lum` against the reference, in full-resolution px."""
    import cv2

    if lum.shape != reference.shape:
        return 0.0, 0.0
    (dx, dy), _response = cv2.phaseCorrelate(
        reference, np.ascontiguousarray(lum, dtype=np.float32), window)
    # Measured on the half-resolution luminance; the image it will move is at
    # full resolution.
    return -dx * LUM_SCALE, -dy * LUM_SCALE


def _shift(rgb: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Move the frame onto the reference, interpolating between pixels.

    Sub-pixel on purpose: rounding to whole pixels throws away most of what
    stacking buys, because the residual half-pixel is exactly the blur the
    average is supposed to be free of.
    """
    import cv2

    m = np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], np.float32)
    return cv2.warpAffine(rgb, m, (rgb.shape[1], rgb.shape[0]),
                          flags=cv2.INTER_LANCZOS4,
                          borderMode=cv2.BORDER_REPLICATE)
