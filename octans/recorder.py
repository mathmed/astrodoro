"""Gravação da sessão: subs brutos, stacks e metadados.

Gravar os subs é a rede de segurança e, mais que isso, é o que permite
reprocessar a sessão depois e reproduzi-la em `ReplaySource` para desenvolver de
dia. Compressão RICE é sem perda e corta o volume pela metade ou mais.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits

from .source import FrameMeta


def read_fits(path: str | Path) -> tuple[np.ndarray, fits.Header]:
    """Lê o primeiro HDU com dados. Necessário porque um HDU comprimido (RICE)
    vive em hdul[1] e o primário fica vazio."""
    with fits.open(path, memmap=False) as hdul:
        for hdu in hdul:
            if hdu.data is not None:
                return np.asarray(hdu.data), hdu.header
    raise ValueError(f"{path}: nenhum HDU com dados")


@dataclass
class Recorder:
    root: Path | str = "sessions"
    target: str = ""
    save_subs: bool = True
    compress: bool = True
    every: int = 1                  # grava 1 de cada N subs
    session_dir: Path | None = None
    n_written: int = 0
    n_skipped: int = 0
    bytes_written: int = 0
    _t0: float = 0.0
    _info: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ sessão
    def begin(self, info: dict, cfg: dict) -> Path:
        self._t0 = time.time()
        self._info = dict(info)
        day = time.strftime("%Y-%m-%d")
        stamp = time.strftime("%H%M")
        slug = _slug(self.target) or "sessao"
        d = Path(self.root) / day / f"{stamp}_{slug}"
        n = 1
        while d.exists():
            n += 1
            d = Path(self.root) / day / f"{stamp}_{slug}_{n}"
        (d / "subs").mkdir(parents=True, exist_ok=True)
        self.session_dir = d
        self._write_json({"info": info, "config": cfg,
                          "target": self.target,
                          "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
        return d

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

    # ------------------------------------------------------------------ subs
    def write_sub(self, raw: np.ndarray, meta: FrameMeta) -> Path | None:
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
        hdr["XPIXSZ"] = (px, "um, efetivo")
        hdr["YPIXSZ"] = (px, "um, efetivo")
        hdr["BAYERPAT"] = meta.bayer.fits_name
        hdr["XBAYROFF"] = 0
        hdr["YBAYROFF"] = 0
        # Escala real dos dados: 14 bits deslocados <<2. Sem isto, qualquer
        # ferramenta assume 65535 como saturação e erra o ponto de clipping.
        hdr["FULLSCAL"] = (meta.full_scale, "valor de saturacao dos dados")
        hdr["BITDEPTH"] = (14, "bits reais do ADC")
        if meta.temperature is not None:
            hdr["CCD-TEMP"] = round(meta.temperature, 2)
        if meta.target_temp is not None:
            hdr["SET-TEMP"] = round(meta.target_temp, 1)
        hdr["FRAMEIDX"] = meta.index
        hdr["DATE-OBS"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                        time.gmtime(meta.timestamp))
        if self.target:
            hdr["OBJECT"] = self.target

        path = self.session_dir / "subs" / f"sub_{meta.index:05d}.fits"
        if self.compress:
            hdu = fits.CompImageHDU(data=raw, header=hdr, compression_type="RICE_1")
            fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path, overwrite=True)
        else:
            fits.PrimaryHDU(data=raw, header=hdr).writeto(path, overwrite=True)

        self.n_written += 1
        self.bytes_written += path.stat().st_size
        return path

    # ------------------------------------------------------------------ stack
    def write_stack(self, stack: np.ndarray, stats: dict, final: bool = False) -> None:
        if not self.session_dir:
            return
        name = "stack_final" if final else "stack"
        hdr = fits.Header()
        hdr["NCOMBINE"] = stats.get("n_stacked", 0)
        hdr["EXPTOTAL"] = (stats.get("integration", 0.0), "s de integracao")
        hdr["NREJECT"] = stats.get("n_rejected", 0)
        if self.target:
            hdr["OBJECT"] = self.target
        arr = stack.transpose(2, 0, 1) if stack.ndim == 3 else stack
        fits.PrimaryHDU(arr.astype(np.float32), hdr).writeto(
            self.session_dir / f"{name}.fits", overwrite=True)

    def write_preview(self, u8_rgb: np.ndarray, name: str = "stack.png") -> None:
        if not self.session_dir:
            return
        import cv2
        cv2.imwrite(str(self.session_dir / name),
                    cv2.cvtColor(u8_rgb, cv2.COLOR_RGB2BGR))

    def end(self, stats: dict) -> None:
        self._write_json({
            "ended": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_s": round(time.time() - self._t0, 1),
            "subs_written": self.n_written, "subs_skipped": self.n_skipped,
            "bytes_written": self.bytes_written,
            "stats": {k: v for k, v in stats.items()
                      if isinstance(v, (int, float, str))},
        })

    # ------------------------------------------------------------------ disco
    @property
    def rate_mb_min(self) -> float:
        dt = time.time() - self._t0
        return (self.bytes_written / 1e6) / (dt / 60) if dt > 5 else 0.0

    def disk_report(self) -> str:
        if not self.session_dir:
            return ""
        free = shutil.disk_usage(self.session_dir).free / 1e9
        r = self.rate_mb_min
        if r <= 0:
            return f"{self.n_written} subs, {self.bytes_written/1e6:.0f} MB, {free:.1f} GB livres"
        hours = (free * 1000) / (r * 60) if r else float("inf")
        return (f"{self.n_written} subs, {self.bytes_written/1e6:.0f} MB, "
                f"{r:.0f} MB/min, {free:.1f} GB livres (~{hours:.1f} h)")


def _slug(s: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in s.strip()]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:40]
