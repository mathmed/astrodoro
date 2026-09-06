"""Master calibration frames: how one is built, named and checked.

Three kinds, and what each one removes:

- **bias** — the offset pedestal plus the read pattern, at the shortest
  exposure the camera accepts (36 us on this one). It is what a flat has to
  have subtracted, and the only pedestal available when no dark of the right
  exposure exists.
- **dark** — the same pedestal *plus* the thermal signal collected during the
  exposure. A dark already contains the bias, which is why `calibration.
  calibrate` subtracts one or the other and never both.
- **flat** — the multiplicative response: vignetting, dust, and on a colour
  sensor the channel response difference.

Every master is a **median**, never a mean: the mean keeps a cosmic ray or a
satellite trail, and at these sizes the median of twenty frames costs nothing.

The file name carries everything the master has to match, because a master that
silently does not match the lights is worse than no master at all —
`dark_g250_o20_e5.00s_bin2_-10C.fits` cannot be mistaken for another session's.
`mismatch` is the readback of that promise: the header against the camera as it
is now.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

from ..i18n import gettext as _

#: In the order they are applied to a light frame.
KINDS = ("bias", "dark", "flat")


@dataclass(frozen=True)
class Setup:
    """The capture parameters a master has to match.

    `temperature` is None on a camera without a cooler, and only a dark cares
    about it: the thermal signal doubles every ~6 C, while the pedestal a bias
    measures barely moves.
    """

    gain: int
    offset: int
    exposure: float
    bin: int
    temperature: float | None = None
    bayer: str = ""
    full_scale: int = 65535

    @classmethod
    def from_camera(cls, cam) -> Setup:
        return cls(gain=cam.gain, offset=cam.offset, exposure=cam.exposure,
                   bin=cam.geometry.bin,
                   temperature=cam.temperature if cam.supports_cooler else None,
                   bayer=cam.bayer.fits_name, full_scale=cam.full_scale)


def combine(frames: list[np.ndarray]) -> np.ndarray:
    """Median of the stack, as float32."""
    return np.median(np.stack([np.asarray(f, dtype=np.float32)
                               for f in frames]), axis=0).astype(np.float32)


def name(kind: str, s: Setup) -> str:
    """File name for a master of `kind`, carrying what it has to match.

    A bias has no exposure in the name — it is defined by being the shortest the
    camera does — and a flat has neither exposure nor offset: it is a ratio, so
    the pedestal is subtracted out of it and what is left depends on the optics
    and the bin, not on how long the white sheet was exposed for.
    """
    if kind == "bias":
        return f"bias_g{s.gain}_o{s.offset}_bin{s.bin}.fits"
    if kind == "flat":
        return f"flat_g{s.gain}_bin{s.bin}.fits"
    return (f"dark_g{s.gain}_o{s.offset}_e{s.exposure:.2f}s_bin{s.bin}"
            + (f"_{s.temperature:+.0f}C" if s.temperature is not None else "")
            + ".fits")


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


def write(folder: str | Path, kind: str, data: np.ndarray, s: Setup,
          n_frames: int) -> Path:
    """Write the master and return its path. The folder is created on demand."""
    out = Path(folder).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    path = out / name(kind, s)
    fits.PrimaryHDU(np.asarray(data, dtype=np.float32),
                    header(kind, s, n_frames)).writeto(path, overwrite=True)
    return path


def mismatch(kind: str, hdr, s: Setup) -> list[str]:
    """What in this master's header does not match the setup, in words.

    Warnings, not refusals: the geometry is the only thing that makes a master
    unusable, and that is checked on the array's shape by the caller. A dark
    two degrees off is still worth using and the user may know why — but they
    have to be told, because the residual is a gradient that looks like sky.

    Which fields matter depends on the kind, and this is where the physics
    lives: the offset defines the pedestal a bias measures, the exposure and
    the temperature define the thermal signal a dark measures, and a flat is a
    ratio that cares about neither.
    """
    out: list[str] = []

    def num(key, default=None):
        try:
            return float(hdr[key])
        except (KeyError, TypeError, ValueError):
            return default

    gain = num("GAIN")
    if gain is not None and int(gain) != int(s.gain):
        out.append(_("{kind} at gain {have} against lights at gain {want}"
                     ).format(kind=kind, have=int(gain), want=int(s.gain)))

    if kind in ("bias", "dark"):
        offset = num("OFFSET")
        if offset is not None and int(offset) != int(s.offset):
            out.append(_(
                "{kind} at offset {have} against lights at offset {want} — "
                "the pedestal it removes is the wrong one").format(
                    kind=kind, have=int(offset), want=int(s.offset)))

    if kind == "dark":
        exp = num("EXPTIME")
        if exp is not None and abs(exp - s.exposure) > max(0.05 * s.exposure,
                                                           0.01):
            out.append(_(
                "dark of {have:.2f}s against lights of {want:.2f}s — the "
                "thermal signal does not cancel").format(have=exp,
                                                         want=s.exposure))
        temp = num("CCD-TEMP")
        if (temp is not None and s.temperature is not None
                and abs(temp - s.temperature) > 2.0):
            out.append(_(
                "dark taken at {have:+.1f} C, sensor now at {want:+.1f} C "
                "({delta:+.1f} C apart)").format(
                    have=temp, want=s.temperature, delta=s.temperature - temp))
    return out
