from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
from astropy.io import fits

from ..i18n import gettext as _
from .source import FrameMeta


def read_fits(path: str | Path) -> tuple[np.ndarray, fits.Header]:
    with fits.open(path, memmap=False) as hdul:
        for hdu in hdul:
            if hdu.data is not None:
                return np.asarray(hdu.data), hdu.header
    raise ValueError(f"{path}: no HDU with data")


@dataclass
class Recorder:
    root: Path | str
    target: str = ""
    save_subs: bool = True
    compress: bool = True
    every: int = 1
    sub_suffix: str = ".fits"
    session_dir: Path | None = None
    n_written: int = 0
    n_skipped: int = 0
    bytes_written: int = 0
    _t0: float = 0.0
    _info: dict = field(default_factory=dict)

    def begin(self, info: dict, cfg: dict) -> Path:
        self._t0 = time.time()
        self._info = dict(info)
        day = time.strftime("%Y-%m-%d")
        stamp = time.strftime("%H%M")
        slug = _slug(self.target) or "session"
        d = Path(self.root) / day / f"{stamp}_{slug}"
        n = 1
        while d.exists():
            n += 1
            d = Path(self.root) / day / f"{stamp}_{slug}_{n}"
        (d / "subs").mkdir(parents=True, exist_ok=True)
        self.session_dir = d
        self._write_json(
            {
                "info": info,
                "config": cfg,
                "target": self.target,
                "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
        return d

    def rename_target(self, target: str) -> Path | None:
        self.target = target
        if not self.session_dir or not self.session_dir.exists():
            return None
        subs = self.session_dir / "subs"
        if subs.exists() and any(subs.iterdir()):
            return self.session_dir
        stamp = self.session_dir.name.split("_")[0]
        fresh = self.session_dir.parent / f"{stamp}_{_slug(target) or 'session'}"
        n = 1
        while fresh.exists() and fresh != self.session_dir:
            n += 1
            fresh = self.session_dir.parent / f"{stamp}_{_slug(target)}_{n}"
        if fresh != self.session_dir:
            self.session_dir.rename(fresh)
            self.session_dir = fresh
        self._write_json({"target": target})
        return self.session_dir

    def _write_json(self, extra: dict) -> None:
        if not self.session_dir:
            return
        p = self.session_dir / "session.json"
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text())
            except Exception:
                data = {}
        data.update(extra)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def write_sub(
        self, raw: np.ndarray, meta: FrameMeta, extra: dict | None = None
    ) -> Path | None:
        if not self.save_subs or not self.session_dir:
            return None
        if self.every > 1 and (meta.index - 1) % self.every:
            self.n_skipped += 1
            return None

        hdr = fits.Header()
        hdr["INSTRUME"] = self._info.get("name", "?")
        hdr["IMAGETYP"] = "LIGHT"
        hdr["EXPTIME"] = (meta.exposure, "s")
        hdr["GAIN"] = meta.gain
        hdr["OFFSET"] = meta.offset
        hdr["XBINNING"] = meta.bin
        hdr["YBINNING"] = meta.bin
        px = self._info.get("pixel_um", 4.63) * meta.bin
        hdr["XPIXSZ"] = (px, "um, effective")
        hdr["YPIXSZ"] = (px, "um, effective")
        hdr["BAYERPAT"] = meta.bayer.fits_name
        hdr["XBAYROFF"] = 0
        hdr["YBAYROFF"] = 0
        hdr["FULLSCAL"] = (meta.full_scale, "data saturation value")
        hdr["BITDEPTH"] = (14, "real ADC bits")
        if meta.temperature is not None:
            hdr["CCD-TEMP"] = round(meta.temperature, 2)
        if meta.target_temp is not None:
            hdr["SET-TEMP"] = round(meta.target_temp, 1)
        hdr["FRAMEIDX"] = meta.index
        hdr["DATE-OBS"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(meta.timestamp)
        )
        if self.target:
            hdr["OBJECT"] = self.target
        for key, value in (extra or {}).items():
            hdr[key] = value

        path = self.session_dir / "subs" / f"sub_{meta.index:05d}{self.sub_suffix}"
        if self.compress:
            hdu = fits.CompImageHDU(data=raw, header=hdr, compression_type="RICE_1")
            fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)
        else:
            fits.PrimaryHDU(data=raw, header=hdr).writeto(path, overwrite=True)

        self.n_written += 1
        self.bytes_written += path.stat().st_size
        return path

    def write_stack(self, stack: np.ndarray, stats: dict, final: bool = False) -> None:
        if not self.session_dir:
            return
        name = "stack_final" if final else "stack"
        hdr = fits.Header()
        hdr["NCOMBINE"] = stats.get("n_stacked", 0)
        hdr["EXPTOTAL"] = (stats.get("integration", 0.0), "s of integration")
        hdr["NREJECT"] = stats.get("n_rejected", 0)
        if self.target:
            hdr["OBJECT"] = self.target
        arr = stack.transpose(2, 0, 1) if stack.ndim == 3 else stack
        fits.PrimaryHDU(arr.astype(np.float32), hdr).writeto(
            self.session_dir / f"{name}.fits", overwrite=True
        )

    def end(self, stats: dict) -> None:
        self._write_json(
            {
                "ended": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "duration_s": round(time.time() - self._t0, 1),
                "subs_written": self.n_written,
                "subs_skipped": self.n_skipped,
                "bytes_written": self.bytes_written,
                "stats": {
                    k: v for k, v in stats.items() if isinstance(v, (int, float, str))
                },
            }
        )

    @property
    def rate_mb_min(self) -> float:
        dt = time.time() - self._t0
        return (self.bytes_written / 1e6) / (dt / 60) if dt > 5 else 0.0

    def disk_report(self) -> str:
        if not self.session_dir:
            return ""
        free = shutil.disk_usage(self.session_dir).free / 1e9
        rate = self.rate_mb_min
        if rate <= 0:
            return _("{n} subs, {mb:.0f} MB, {free:.1f} GB free").format(
                n=self.n_written, mb=self.bytes_written / 1e6, free=free
            )
        hours = (free * 1000) / (rate * 60) if rate else float("inf")
        return _(
            "{n} subs, {mb:.0f} MB, {rate:.0f} MB/min, {free:.1f} GB free "
            "(~{hours:.1f} h)"
        ).format(
            n=self.n_written,
            mb=self.bytes_written / 1e6,
            rate=rate,
            free=free,
            hours=hours,
        )


def _slug(s: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in s.strip()]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40]


@dataclass
class Burst:
    recorder: Recorder
    max_frames: int = 0
    max_seconds: float = 0.0
    n: int = 0
    _t0: float = 0.0

    def begin(self, info: dict, cfg: dict) -> Path:
        self._t0 = time.time()
        return self.recorder.begin(info, dict(cfg, kind="burst"))

    @property
    def session_dir(self) -> Path | None:
        return self.recorder.session_dir

    @property
    def elapsed(self) -> float:
        return time.time() - self._t0 if self._t0 else 0.0

    @property
    def done(self) -> bool:
        if self.max_frames and self.n >= self.max_frames:
            return True
        return bool(self.max_seconds) and self.elapsed >= self.max_seconds

    @property
    def progress(self) -> float:
        by_frames = self.n / self.max_frames if self.max_frames else 0.0
        by_time = self.elapsed / self.max_seconds if self.max_seconds else 0.0
        return float(min(max(by_frames, by_time), 1.0))

    def write(
        self, raw: np.ndarray, meta: FrameMeta, sharpness: float | None = None
    ) -> Path | None:
        self.n += 1
        extra = (
            {"SHARPNS": (round(float(sharpness), 4), "gradient contrast")}
            if sharpness is not None
            else None
        )
        return self.recorder.write_sub(raw, replace(meta, index=self.n), extra)

    def end(self) -> dict:
        stats = {
            "frames": self.n,
            "seconds": round(self.elapsed, 1),
            "fps": round(self.n / self.elapsed, 2) if self.elapsed else 0.0,
        }
        self.recorder.end(stats)
        return stats
