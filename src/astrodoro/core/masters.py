from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

from ..i18n import gettext as _

KINDS = ("bias", "dark", "flat")


@dataclass(frozen=True)
class Setup:
    gain: int
    offset: int
    exposure: float
    bin: int
    temperature: float | None = None
    bayer: str = ""
    full_scale: int = 65535

    @classmethod
    def from_camera(cls, cam) -> Setup:
        return cls(
            gain=cam.gain,
            offset=cam.offset,
            exposure=cam.exposure,
            bin=cam.geometry.bin,
            temperature=cam.temperature if cam.supports_cooler else None,
            bayer=cam.bayer.fits_name,
            full_scale=cam.full_scale,
        )


def combine(frames: list[np.ndarray]) -> np.ndarray:
    return np.median(
        np.stack([np.asarray(f, dtype=np.float32) for f in frames]), axis=0
    ).astype(np.float32)


def name(kind: str, s: Setup) -> str:
    if kind == "bias":
        return f"bias_g{s.gain}_o{s.offset}_bin{s.bin}.fits"
    if kind == "flat":
        return f"flat_g{s.gain}_bin{s.bin}.fits"
    return (
        f"dark_g{s.gain}_o{s.offset}_e{s.exposure:.2f}s_bin{s.bin}"
        + (f"_{s.temperature:+.0f}C" if s.temperature is not None else "")
        + ".fits"
    )


def header(kind: str, s: Setup, n_frames: int) -> fits.Header:
    hdr = fits.Header()
    hdr["IMAGETYP"] = kind.upper()
    hdr["EXPTIME"] = float(s.exposure)
    hdr["GAIN"] = int(s.gain)
    hdr["OFFSET"] = int(s.offset)
    hdr["XBINNING"] = int(s.bin)
    hdr["NCOMBINE"] = int(n_frames)
    hdr["FULLSCAL"] = int(s.full_scale)
    if s.bayer:
        hdr["BAYERPAT"] = s.bayer
    if s.temperature is not None:
        hdr["CCD-TEMP"] = round(float(s.temperature), 2)
    return hdr


def write(
    folder: str | Path, kind: str, data: np.ndarray, s: Setup, n_frames: int
) -> Path:
    out = Path(folder).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    path = out / name(kind, s)
    fits.PrimaryHDU(
        np.asarray(data, dtype=np.float32), header(kind, s, n_frames)
    ).writeto(path, overwrite=True)
    return path


def mismatch(kind: str, hdr, s: Setup) -> list[str]:
    out: list[str] = []

    def num(key, default=None):
        try:
            return float(hdr[key])
        except (KeyError, TypeError, ValueError):
            return default

    gain = num("GAIN")
    if gain is not None and int(gain) != int(s.gain):
        out.append(
            _("{kind} at gain {have} against lights at gain {want}").format(
                kind=kind, have=int(gain), want=int(s.gain)
            )
        )

    if kind in ("bias", "dark"):
        offset = num("OFFSET")
        if offset is not None and int(offset) != int(s.offset):
            out.append(
                _(
                    "{kind} at offset {have} against lights at offset {want} — "
                    "the pedestal it removes is the wrong one"
                ).format(kind=kind, have=int(offset), want=int(s.offset))
            )

    if kind == "dark":
        exp = num("EXPTIME")
        if exp is not None and abs(exp - s.exposure) > max(0.05 * s.exposure, 0.01):
            out.append(
                _(
                    "dark of {have:.2f}s against lights of {want:.2f}s — the "
                    "thermal signal does not cancel"
                ).format(have=exp, want=s.exposure)
            )
        temp = num("CCD-TEMP")
        if (
            temp is not None
            and s.temperature is not None
            and abs(temp - s.temperature) > 2.0
        ):
            out.append(
                _(
                    "dark taken at {have:+.1f} C, sensor now at {want:+.1f} C "
                    "({delta:+.1f} C apart)"
                ).format(have=temp, want=s.temperature, delta=s.temperature - temp)
            )
    return out
