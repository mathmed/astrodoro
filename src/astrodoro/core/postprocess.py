"""Offline post-processing of a linear Astrodoro stack.

Everything here runs once, by hand, after a session — never per frame and never
under `LiveStacker`. The order is not negotiable and follows what the field
actually does: gradient removal and colour calibration are additive/multiplicative
corrections that only mean what they say on LINEAR data, atmospheric dispersion
and the PSF are optical facts that exist before any of that, deconvolution stays
linear too, and the stretch is the one non-linear transition — chroma denoise and
saturation come after it. Doing gradient or colour on stretched data is what
produces the blotchy, off-colour look everyone tries to fix afterwards instead of
in order. See `docs/design-notes.md` for the measurements behind each step.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from astropy.io import fits
from scipy.ndimage import shift as nd_shift
from scipy.signal import fftconvolve

from . import stars as stars_mod
from . import stretch

Logger = Callable[[str], None]


def load_linear(path: str | Path) -> np.ndarray:
    """A stack FITS as (h, w, 3) float32 — `Recorder.write_stack`'s own layout."""
    a = np.ascontiguousarray(fits.getdata(path), dtype=np.float32)
    return np.moveaxis(a, 0, -1) if a.ndim == 3 and a.shape[0] == 3 else a


def write_linear(rgb: np.ndarray, path: str | Path) -> None:
    fits.writeto(path, np.moveaxis(rgb, -1, 0).astype(np.float32), overwrite=True)


# ------------------------------------------------------------------- gradient
def star_mask(lum: np.ndarray, grow: int = 9) -> np.ndarray:
    """True where there is NO star — the pixels a background fit may sample.

    Sigma clipping alone is enough for a sparse field, but a dense one (an open
    cluster, a crowded Milky Way field) has whole tiles that are more star than
    sky, and the clipped survivors still sit on stellar haloes. An explicit mask
    measured from detection is what keeps the model on the sky.
    """
    med = float(np.median(lum))
    mad = 1.4826 * float(np.median(np.abs(lum - med)))
    hot = (lum > med + 2.5 * mad).astype(np.uint8)
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow))
    return cv2.dilate(hot, ker) == 0


def coverage_crop(lum: np.ndarray, step: int = 20, thresh: float = 1.8,
                  margin: int = 40) -> tuple[int, int, int, int]:
    """(top, bottom, left, right) pixels to drop — the partial-coverage border.

    On a Dobsonian on an equatorial platform the field drifts and rotates
    slightly over a long integration, so the edges of the stack are built from
    fewer frames than the centre. Vignetting-corrected, that shows up purely as
    noise, not signal — a strip several times noisier than the interior — which
    is what everyone crops by hand. This measures where.
    """
    h, w = lum.shape

    def strip_noise(a: np.ndarray) -> float:
        a = a[a < np.percentile(a, 85)]
        return float(1.4826 * np.median(np.abs(a - np.median(a))))

    rows = np.array([strip_noise(lum[y:y + step, :].ravel())
                     for y in range(0, h - step, step)])
    cols = np.array([strip_noise(lum[:, x:x + step].ravel())
                     for x in range(0, w - step, step)])

    def edges(v: np.ndarray) -> tuple[int, int]:
        ref = float(np.percentile(v, 30))
        lo = 0
        while lo < len(v) and v[lo] > thresh * ref:
            lo += 1
        hi = len(v) - 1
        while hi > lo and v[hi] > thresh * ref:
            hi -= 1
        return lo * step, (len(v) - 1 - hi) * step

    top, bottom = edges(rows)
    left, right = edges(cols)
    return top + margin, bottom + margin, left + margin, right + margin


def background_model(ch: np.ndarray, ok: np.ndarray, tiles: int = 24,
                     smooth: float = 2.0) -> np.ndarray:
    """Smooth sky model, DBE-style: low percentile per tile, then interpolate.

    A polynomial will not do here in general: light pollution plus an
    uncorrected Newtonian's vignetting can peak off-centre and fall unevenly to
    the corners, an asymmetric shape a low-degree polynomial leaves a visible
    residual veil behind. A tile grid follows whatever shape the sky actually
    has.
    """
    h, w = ch.shape
    ys = np.linspace(0, h, tiles + 1).astype(int)
    xs = np.linspace(0, w, tiles + 1).astype(int)
    coarse = np.zeros((tiles, tiles), dtype=np.float32)
    valid = np.zeros((tiles, tiles), dtype=bool)
    for i in range(tiles):
        for j in range(tiles):
            t = ch[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
            m = ok[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
            t = t[m]
            if t.size < 64:
                continue
            # 30th percentile of the star-free pixels: the median still rides
            # on unresolved stellar background in a dense field.
            coarse[i, j] = np.percentile(t, 30.0)
            valid[i, j] = True

    if valid.sum() < 16:
        raise ValueError("not enough sky tiles for a background model")

    # Fill tiles that were all stars from their neighbours, then smooth: a hole
    # left at zero would pull the interpolation into a dark pit.
    filled = coarse.copy()
    filled[~valid] = np.nan
    for _ in range(6):
        if not np.isnan(filled).any():
            break
        blur = cv2.blur(np.nan_to_num(filled), (3, 3))
        cnt = cv2.blur((~np.isnan(filled)).astype(np.float32), (3, 3))
        with np.errstate(invalid="ignore", divide="ignore"):
            filled = np.where(np.isnan(filled), blur / np.maximum(cnt, 1e-6), filled)
    filled = cv2.GaussianBlur(np.nan_to_num(filled), (0, 0), smooth)
    return cv2.resize(filled, (w, h), interpolation=cv2.INTER_CUBIC)


def remove_gradient(rgb: np.ndarray, tiles: int = 40,
                    mode: str = "divide") -> np.ndarray:
    """Flat-field / gradient correction with no flat frame.

    Vignetting is MULTIPLICATIVE (it scales the signal, stars included) while
    light pollution is additive. `mode="divide"` corrects the dominant term of
    the two by dividing by the normalised sky model; `mode="subtract"` is the
    purely additive alternative, useful for comparison when vignetting is
    already flat-corrected upstream.
    """
    ok = star_mask(rgb.mean(axis=2))
    out = rgb.copy()
    for k in range(3):
        model = background_model(out[..., k], ok, tiles=tiles)
        if mode == "divide":
            norm = np.maximum(model / float(np.median(model)), 1e-6)
            out[..., k] = out[..., k] / norm
        else:
            out[..., k] = out[..., k] - model + float(np.median(model))
    return np.clip(out, 0.0, None)


# ------------------------------------------------------------- optics, colour
def align_channels(rgb: np.ndarray, log: Logger | None = None) -> np.ndarray:
    """Register R and B onto G, subpixel — atmospheric dispersion.

    The atmosphere is a prism: it spreads a star into a tiny spectrum along the
    direction of the horizon, longer the lower the target. That is what puts a
    red fringe on one side of every star and a blue one on the other, and in a
    dense field the eye integrates the mixture as a colour cast on the sky. It
    is optics, not processing — it is there in the raw linear stack, and an ADC
    corrects it in hardware. In software the fix is the same one everybody
    applies: shift the channels back on top of each other.
    """
    med = np.array([np.median(rgb[..., k]) for k in range(3)], dtype=np.float32)
    lum = np.ascontiguousarray(rgb.sum(axis=2), dtype=np.float32)
    sf = stars_mod.detect(lum, scale=1.0, max_stars=400, central=0.85)
    r = 7
    h, w, _ = rgb.shape
    sel = [(x, y) for (x, y), pk in zip(sf.xy, sf.peak, strict=False)
           if 0.05 < pk < 0.5 and r < x < w - r and r < y < h - r]
    if len(sel) < 20:
        if log:
            log("channel alignment        skipped, too few isolated stars")
        return rgb

    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    cen = np.zeros((3, 2))
    for x, y in sel:
        xi, yi = int(round(x)), int(round(y))
        cut = rgb[yi - r:yi + r + 1, xi - r:xi + r + 1] - med
        for k in range(3):
            c = np.clip(cut[..., k], 0, None)
            tot = c.sum()
            if tot > 0:
                cen[k] += [(xx * c).sum() / tot, (yy * c).sum() / tot]
    cen /= len(sel)

    out = rgb.copy()
    for k in (0, 2):
        dx, dy = cen[k] - cen[1]
        out[..., k] = nd_shift(rgb[..., k], (-dy, -dx), order=3, mode="nearest")
        if log:
            log(f"channel alignment        {'RGB'[k]} shifted "
                f"{-dx:+.3f}, {-dy:+.3f} px onto G ({len(sel)} stars)")
    return out


def match_psf(rgb: np.ndarray, log: Logger | None = None) -> np.ndarray:
    """Blur the sharper channels until all three share one PSF width.

    Aligning the channels fixes the dispersion FRINGE but not a colour HALO:
    a centroid shift is asymmetric and averages out around a star, while a
    difference in PSF width is symmetric and does not — chromatic focus, the
    system sharpest in one channel (usually green, with twice the photosites).
    No colour calibration reaches this, because it is a difference in shape,
    not in level.

    Matching means convolving the sharp channels up to the widest one, never
    deconvolving the wide one down: Gaussian quadrature is exact and costs
    nothing, deconvolution invents detail that was not there.
    """
    fw = []
    med = [float(np.median(rgb[..., k])) for k in range(3)]
    for k in range(3):
        sf = stars_mod.detect(np.ascontiguousarray(rgb[..., k] - med[k],
                                                   dtype=np.float32),
                              scale=1.0, max_stars=800, central=0.85)
        fw.append(float(sf.median_fwhm))
    target = max(fw)
    out = rgb.copy()
    for k in range(3):
        # Gaussians add in quadrature: the kernel that takes fw[k] to target.
        extra = np.sqrt(max(target ** 2 - fw[k] ** 2, 0.0)) / 2.355
        if extra > 0.05:
            out[..., k] = cv2.GaussianBlur(rgb[..., k], (0, 0), extra)
        if log:
            log(f"psf match                {'RGB'[k]} FWHM {fw[k]:.3f} px"
                f" -> {target:.3f} (blur sigma {extra:.3f})")
    return out


def calibrate_color(rgb: np.ndarray, aperture: int = 3, method: str = "median",
                    log: Logger | None = None) -> np.ndarray:
    """Neutralise the background AND the stars' colour, on linear data.

    Light pollution is an additive offset on the background; the sensor's
    unequal channel response is a multiplicative gain on the signal.
    Neutralising the sky alone leaves every star tinted, so this does both: the
    median subtracts the offset, the star flux ratios give the gain.

    The aperture decides which part of a star is made neutral: a small one
    neutralises the cores and can leave chromatic-focus haloes coloured; a wide
    one neutralises total flux, the photometrically honest choice.

    The gain is measured on integrated flux in a small aperture, not on the
    peak pixel — a peak sample is one pixel of a several-px FWHM profile and
    lands wherever the seeing put it that frame. `method="median"` takes the
    median of the per-star R/G and B/G ratios, which a handful of bright stars
    cannot drag off; `method="flux"` sums total flux first, an average
    weighted by brightness that a few hot, blue members can bias.
    """
    med = np.array([np.median(rgb[..., k]) for k in range(3)], dtype=np.float32)
    rgb = rgb - med

    lum = np.ascontiguousarray(rgb.sum(axis=2), dtype=np.float32)
    sf = stars_mod.detect(lum, scale=1.0, max_stars=400, central=0.9)
    r = aperture
    h, w, _ = rgb.shape
    keep = ((sf.xy[:, 0] > r) & (sf.xy[:, 0] < w - r - 1) &
            (sf.xy[:, 1] > r) & (sf.xy[:, 1] < h - r - 1) & (sf.peak < 0.7))
    ys = sf.xy[keep, 1].astype(int)
    xs = sf.xy[keep, 0].astype(int)
    per_star = np.zeros((len(ys), 3))
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            per_star += rgb[ys + dy, xs + dx, :]
    flux = per_star.sum(axis=0)

    if method == "median":
        valid = per_star[:, 1] > 0
        ratio_r = float(np.median(per_star[valid, 0] / per_star[valid, 1]))
        ratio_b = float(np.median(per_star[valid, 2] / per_star[valid, 1]))
        gain = np.array([1.0 / max(ratio_r, 1e-9), 1.0, 1.0 / max(ratio_b, 1e-9)])
        flux_gain = float(flux.mean()) / np.maximum(flux, 1e-9)
        gain *= float(np.mean(flux_gain)) / gain.mean()
    else:
        gain = float(flux.mean()) / np.maximum(flux, 1e-9)
    if log:
        log(f"colour calibration       {keep.sum()} stars ({method}), gains "
            f"R{gain[0]:.3f} G{gain[1]:.3f} B{gain[2]:.3f}")

    # Blend the gain down to 1 wherever there is no real signal. A gain this
    # size (measured on stars) is compensating the sensor's channel response,
    # not the sky — applying it flat also multiplies whichever channel is
    # noisiest by the same factor, which is invisible on a star and reads as
    # blotchy colour speckle on the faint corners where SNR is already worst.
    lum = rgb.mean(axis=2)
    mad = 1.4826 * float(np.median(np.abs(lum - np.median(lum))))
    weight = np.clip(lum / max(4.0 * mad, 1e-9), 0.0, 1.0)[..., None]
    eff_gain = 1.0 + (gain.astype(np.float32) - 1.0) * weight
    return np.clip(rgb * eff_gain + med.mean(), 0.0, 1.0)


# ---------------------------------------------------------------- deconvolve
def deconvolve(rgb: np.ndarray, iters: int, log: Logger | None = None) -> np.ndarray:
    """Gentle Richardson-Lucy on the luminance, reapplied to channels as a ratio.

    The PSF is empirical, the median of the frame's own brightest unsaturated
    stars — a Newtonian on a platform has coma and tracking asymmetry no
    analytic profile carries. The correction is masked by SNR so it only acts
    where there is signal to sharpen, blended back to the original elsewhere;
    RL amplifies noise hardest exactly where there is none.
    """
    lum = np.ascontiguousarray(rgb.mean(axis=2), dtype=np.float32)
    sf = stars_mod.detect(lum, scale=1.0, max_stars=40, central=0.9, saturation=0.85)
    size, half = 21, 10
    h, w = lum.shape
    stamps = []
    for (x, y), peak in zip(sf.xy, sf.peak, strict=False):
        xi, yi = int(round(x)), int(round(y))
        if (peak >= 0.85 or xi - half < 0 or yi - half < 0
                or xi + half + 1 > w or yi + half + 1 > h):
            continue
        cut = lum[yi - half:yi + half + 1, xi - half:xi + half + 1] - sf.background
        cut = nd_shift(cut, (yi - y, xi - x), order=3, mode="nearest")
        if cut.sum() > 0:
            stamps.append(cut / cut.sum())
    if len(stamps) < 5:
        if log:
            log("deconvolution            skipped, PSF unreliable")
        return rgb

    psf = np.clip(np.median(np.stack(stamps), axis=0), 0.0, None)
    yy, xx = np.mgrid[:size, :size] - half
    rr = np.hypot(xx, yy) / half
    # Apodise to a circular window, or the square stamp support leaves a
    # visible square frame around every bright star.
    psf *= np.clip(0.5 * (1 + np.cos(np.pi * np.clip((rr - 0.6) / 0.4, 0, 1))), 0, 1)
    psf /= psf.sum()

    est = np.maximum(lum, 1e-6)
    flip = psf[::-1, ::-1]
    for _ in range(iters):
        blur = fftconvolve(est, psf, mode="same")
        est = np.clip(est * fftconvolve(lum / np.maximum(blur, 1e-9), flip,
                                        mode="same"), 0.0, None)

    bg = float(np.median(lum))
    noise = 1.4826 * float(np.median(np.abs(lum - bg)))
    mask = np.clip((lum - bg) / max(10.0 * noise, 1e-9), 0.0, 1.0) ** 0.5
    mask = cv2.GaussianBlur(mask, (0, 0), 2.0)
    out_lum = lum * (1 - mask) + est * mask
    if log:
        sf2 = stars_mod.detect(np.ascontiguousarray(out_lum, dtype=np.float32),
                               scale=1.0, central=0.9)
        log(f"deconvolution RL {iters:<2d}      FWHM {sf.median_fwhm:.2f} -> "
            f"{sf2.median_fwhm:.2f} px")
    ratio = out_lum / np.maximum(lum, 1e-9)
    return np.clip(rgb * ratio[..., None], 0.0, 1.0)


# -------------------------------------------------------------------- stretch
def arcsinh_neutral(rgb: np.ndarray, target_bg: float) -> np.ndarray:
    """Arcsinh stretch with ONE black point shared by the three channels.

    `stretch.auto_arcsinh` estimates the black point per channel, a fixed
    number of sigmas below that channel's own floor — and sigma is not the
    same in the three, because a Bayer sensor has twice as many green
    photosites and G averages two raw pixels per output pixel. The quieter
    channel gets the higher black point, sits lower after subtraction, and a
    background that was exactly neutral in linear data comes out of that
    stretch with a magenta cast — an artefact of the per-channel estimator,
    not of the sky.

    `calibrate_color` already neutralised the background on linear data, where
    the correction belongs, so the black point here is the SAME number for the
    three channels and neutrality survives the stretch.
    """
    lum = rgb.mean(axis=2)
    from . import background as _bg
    _pts, vals = _bg.sample_tiles(np.ascontiguousarray(lum, dtype=np.float32),
                                  grid=10)
    floor = float(np.median(vals)) if len(vals) >= 6 else float(np.median(lum))
    mad = 1.4826 * float(np.median(np.abs(lum[::4, ::4] - floor)))
    c0 = float(np.clip(floor - 2.8 * mad, 0.0, 1.0))

    b = np.clip((rgb - c0) / max(1.0 - c0, 1e-6), 0.0, 1.0)
    bl = b.mean(axis=2)
    st = stretch.solve_strength(max(float(np.median(bl[::4, ::4])), 1e-6), target_bg)
    out = np.arcsinh(st * bl) / np.arcsinh(st)

    # b/bl is a ratio of two small, noisy numbers wherever bl sits near the
    # noise floor, and it blows up long before `1e-8` — that is what turns
    # background noise into colour speckle in the faintest corners of a stack.
    # Blending the ratio down to 1 (neutral) below a few sigma of noise leaves
    # real, above-noise colour (stars, nebula) untouched and lets the
    # background stay the neutral grey it should be.
    noise = max(mad / max(1.0 - c0, 1e-6), 1e-9)
    weight = np.clip(bl / (6.0 * noise), 0.0, 1.0)[..., None]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(bl[..., None] > 1e-8, b / np.maximum(bl[..., None], 1e-8), 1.0)
    ratio = 1.0 + (ratio - 1.0) * weight
    return np.clip(out[..., None] * ratio, 0.0, 1.0).astype(np.float32)


def denoise_chroma(img: np.ndarray, strength: float) -> np.ndarray:
    """Blur only a/b in Lab — the stack's noise is mostly luminance noise.

    Per-channel gains and saturation turn any residual colour noise into
    speckle over the sky. Detail lives in L and is left untouched; bilateral
    rather than Gaussian so a red giant does not bleed its colour into the sky
    around it.
    """
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2Lab)
    for k in (1, 2):
        lab[..., k] = cv2.bilateralFilter(lab[..., k], 0, 12.0, strength)
    return np.clip(cv2.cvtColor(lab, cv2.COLOR_Lab2RGB), 0.0, 1.0)


def denoise_luminance(img: np.ndarray, strength: float,
                      log: Logger | None = None) -> np.ndarray:
    """Non-local-means denoise, masked to the sky only.

    Stars keep every photon of detail they have; only the empty sky, where all
    the noise lives and no detail does, gets smoothed. Applied to the whole
    frame this is what makes a stack look like plastic.
    """
    lum = img.mean(axis=2)
    bg = float(np.median(lum))
    mad = 1.4826 * float(np.median(np.abs(lum - bg)))
    sky = 1.0 - np.clip((lum - bg) / max(4.0 * mad, 1e-6), 0.0, 1.0)
    sky = cv2.GaussianBlur(sky, (0, 0), 2.0)[..., None]
    u8 = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    smooth = cv2.fastNlMeansDenoisingColored(u8, None, float(strength),
                                             float(strength), 7, 21)
    out = np.clip(img * (1 - sky) + (smooth.astype(np.float32) / 255.0) * sky, 0.0, 1.0)
    if log:
        log(f"luminance denoise        strength {strength:.1f} on "
            f"{100 * (sky > 0.5).mean():.1f}% of pixels (sky only)")
    return out


def saturate_masked(img: np.ndarray, amount: float,
                    log: Logger | None = None) -> np.ndarray:
    """Boost chroma where there is signal, leaving the sky's residual colour alone.

    Applied flat, saturation multiplies the sky's residual colour by the same
    factor as the stars', and the background goes brown. Masking by luminance
    keeps the correction on the target.
    """
    lum = img.mean(axis=2)
    bg = float(np.median(lum))
    mad = 1.4826 * float(np.median(np.abs(lum - bg)))
    m = np.clip((lum - bg) / max(6.0 * mad, 1e-6), 0.0, 1.0)[..., None]
    factor = 1.0 + (amount - 1.0) * m
    lum3 = img.mean(axis=2, keepdims=True)
    out = np.clip(lum3 + (img - lum3) * factor, 0.0, 1.0)
    if log:
        log(f"saturation               x{amount:.2f} on "
            f"{100 * (m > 0.5).mean():.1f}% of pixels (signal only)")
    return out


# --------------------------------------------------------------------- crop
def auto_crop(rgb: np.ndarray, tiles: int = 40) -> tuple[int, int, int, int]:
    """Measure the partial-coverage border on flat-corrected luminance.

    The vignetting has to be out of the way first, or its dimming masquerades
    as coverage loss.
    """
    probe = rgb.mean(axis=2)
    m = background_model(probe, star_mask(probe), tiles=tiles)
    return coverage_crop(probe / np.maximum(m / float(np.median(m)), 1e-6))


# ---------------------------------------------------------------- orchestrator
def process(rgb: np.ndarray, *, crop: int = -1, mode: str = "divide",
           tiles: int = 40, align: bool = True, match_psf_widths: bool = False,
           color_method: str = "median", color_aperture: int = 3,
           deconv_iters: int = 0, target_bg: float = 0.25,
           chroma_denoise: float = 2.0, denoise: float = 0.0,
           saturation: float = 1.6, log: Logger | None = None) -> np.ndarray:
    """The full pipeline, linear domain first, in the one order that matters.

    Returns a non-linear RGB float32 image in [0, 1], ready to encode.
    """
    def emit(msg: str) -> None:
        if log:
            log(msg)

    if crop < 0:
        t, b, le, ri = auto_crop(rgb, tiles=tiles)
        emit(f"coverage crop            top {t}  bottom {b}  left {le}  right {ri} px")
        rgb = rgb[t:rgb.shape[0] - b, le:rgb.shape[1] - ri]
    elif crop:
        rgb = rgb[crop:-crop, crop:-crop]

    rgb = remove_gradient(rgb, tiles=tiles, mode=mode)

    # Atmospheric dispersion, before anything measures colour: every star
    # carries a red fringe on one side and a blue one on the other until the
    # channels sit on top of each other.
    if align:
        rgb = align_channels(rgb, log=log)
    if match_psf_widths:
        rgb = match_psf(rgb, log=log)

    rgb = calibrate_color(rgb, aperture=color_aperture, method=color_method, log=log)

    if deconv_iters:
        rgb = deconvolve(rgb, deconv_iters, log=log)

    img = arcsinh_neutral(rgb, target_bg)

    if chroma_denoise > 0:
        img = denoise_chroma(img, chroma_denoise)
    if denoise > 0:
        img = denoise_luminance(img, denoise, log=log)
    if saturation != 1.0:
        img = saturate_masked(img, saturation, log=log)

    return img
