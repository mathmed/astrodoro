"""Frame sources: a live camera, and playback of a recorded session.

The abstraction exists for a practical reason: with it, everything downstream —
focus, alignment, threshold tuning — can be developed and tested in daylight by
feeding the system the subs of an earlier night. One night of capture becomes
weeks of development.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Protocol

import numpy as np

from ..drivers.svbony import Camera, list_cameras
from ..drivers.svbony.sdk import Bayer, ImgType, SVBError
from ..i18n import gettext as _


@dataclass
class FrameMeta:
    index: int
    timestamp: float
    exposure: float
    gain: int
    offset: int
    bin: int
    full_scale: int
    bayer: Bayer
    temperature: float | None = None
    target_temp: float | None = None
    cooler_power: int | None = None
    origin: str = "camera"
    path: str | None = None


class FrameSource(Protocol):
    live: bool

    def open(self) -> dict: ...
    def start(self) -> None: ...
    def resume(self) -> None: ...
    def read(self, timeout: float | None = None
             ) -> tuple[np.ndarray, FrameMeta] | None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    def apply(self, **kw) -> list[str]: ...


# --------------------------------------------------------------------- camera
class CameraSource:
    live = True

    def __init__(self, index: int = 0, bin: int = 2, exposure: float = 5.0,
                 gain: int = 250, offset: int = 20,
                 target_temp: float | None = None):
        self.index = index
        self._cfg = {"bin": bin, "exposure": exposure, "gain": gain,
                     "offset": offset, "target_temp": target_temp}
        self.cam: Camera | None = None
        self._n = 0
        self._first = True

    def open(self) -> dict:
        cams = list_cameras()
        if not cams:
            raise RuntimeError(_("no camera found (is another capture program "
                                 "holding it?)"))
        idx = min(self.index, len(cams) - 1)
        cam = Camera(cams[idx]).open()
        self.cam = cam
        cam.image_type = ImgType.RAW16
        g = cam.set_roi(bin=self._cfg["bin"])
        cam.gain = self._cfg["gain"]
        cam.exposure = self._cfg["exposure"]
        cam.offset = self._cfg["offset"]
        # The cooler is deliberately not switched on here: sending the final
        # target in one go makes the TEC pull 100% and the temperature plunge.
        # CoolerController ramps it instead (core/cooling.py).
        return {
            "name": cam.id.name, "firmware": cam.firmware, "port": cam.id.port,
            "width": g.width, "height": g.height, "bin": g.bin,
            "bayer": cam.bayer.name, "full_scale": cam.full_scale,
            "cooler": cam.supports_cooler, "forced": cam.forced_linear,
            "gain_range": (cam.controls[0].min, cam.controls[0].max),
            "pixel_um": cam.pixel_size_um, "live": True,
        }

    def start(self) -> None:
        self.cam.start_video()
        self._first = True

    def resume(self) -> None:
        # Video was stopped while paused; restarting discards the first frame,
        # which arrives stale.
        self.start()

    def read(self, timeout: float | None = None):
        cam = self.cam
        tmo = timeout if timeout is not None else cam.exposure * 3 + 8
        raw = cam.read_frame(timeout=tmo)
        if self._first:                 # the first frame after start is stale
            self._first = False
            raw = cam.read_frame(timeout=tmo)
        self._n += 1
        g = cam.geometry
        return raw, FrameMeta(
            index=self._n, timestamp=time.time(), exposure=cam.exposure,
            gain=cam.gain, offset=cam.offset, bin=g.bin,
            full_scale=cam.full_scale, bayer=cam.bayer,
            temperature=cam.temperature if cam.supports_cooler else None,
            target_temp=cam.target_temperature if cam.supports_cooler else None,
            cooler_power=cam.cooler_power if cam.supports_cooler else None,
            origin="camera",
        )

    def stop(self) -> None:
        if self.cam:
            try:
                self.cam.stop_video()
            except SVBError:
                pass

    def close(self) -> None:
        if self.cam:
            self.cam.close()
            self.cam = None

    def apply(self, **kw) -> list[str]:
        msgs: list[str] = []
        cam = self.cam
        for k, v in kw.items():
            try:
                if k == "gain":
                    cam.gain = int(v)
                elif k == "exposure":
                    cam.exposure = float(v)
                elif k == "offset":
                    cam.offset = int(v)
                elif k == "target_temp":
                    cam.target_temperature = float(v)
                elif k == "cooler":
                    cam.cooler = bool(v)
                elif k == "bin":
                    cam.stop_video()
                    cam.set_roi(bin=int(v))
                    self._cfg["bin"] = int(v)
                    cam.start_video()
                    self._first = True
                    msgs.append(_("bin{value} applied").format(value=v))
            except (SVBError, ValueError) as e:
                msgs.append(f"{k}: {e}")
        return msgs

    @property
    def geometry(self):
        return self.cam.geometry


# --------------------------------------------------------------------- replay
class ReplaySource:
    """Plays back the FITS subs of a recorded session, in order.

    Honours each frame's metadata (gain, exposure, temperature), so the
    pipeline behaves exactly as it did on the night of capture.
    """
    live = False

    def __init__(self, folder: str | Path, speed: float = 0.0,
                 loop: bool = False):
        self.folder = Path(folder)
        self.speed = speed        # 0 = as fast as possible; 1 = real time
        self.loop = loop
        self.files: list[Path] = []
        self._i = 0
        self._info: dict = {}
        self._t_last = 0.0

    def open(self) -> dict:
        patterns = ("*.fits", "*.fit", "*.fts")
        subs = self.folder / "subs"
        root = subs if subs.is_dir() else self.folder
        for pat in patterns:
            self.files.extend(sorted(root.glob(pat)))
        self.files = sorted(set(self.files))
        if not self.files:
            raise RuntimeError(_("no FITS files in {path}").format(path=root))

        from .recorder import read_fits
        d, h = read_fits(self.files[0])
        bayer = _bayer_from_header(h)
        self._info = {
            "name": f"replay: {self.folder.name}",
            "firmware": "-", "port": f"{len(self.files)} frames",
            "width": int(d.shape[1]), "height": int(d.shape[0]),
            "bin": int(h.get("XBINNING", 1)),
            "bayer": bayer.name,
            "full_scale": int(h.get("FULLSCAL", 65532)),
            "cooler": "CCD-TEMP" in h, "forced": [],
            "gain_range": (0, 570),
            "pixel_um": float(h.get("XPIXSZ", 4.63)),
            "live": False, "n_frames": len(self.files),
        }
        return self._info

    def start(self) -> None:
        self._i = 0
        self._t_last = 0.0

    def resume(self) -> None:
        # Does NOT rewind: resuming a paused replay must continue where it
        # stopped, not jump back to the first frame.
        self._t_last = 0.0

    def read(self, timeout: float | None = None):
        if self._i >= len(self.files):
            if not self.loop:
                return None
            self._i = 0
        path = self.files[self._i]
        self._i += 1
        from .recorder import read_fits
        raw, h = read_fits(path)
        exposure = float(h.get("EXPTIME", 1.0))

        if self.speed > 0:
            # Emulate the original cadence, dead read time included.
            wait = exposure / self.speed
            dt = time.perf_counter() - self._t_last
            if self._t_last and dt < wait:
                time.sleep(wait - dt)
            self._t_last = time.perf_counter()

        meta = FrameMeta(
            index=self._i, timestamp=_timestamp(h), exposure=exposure,
            gain=int(h.get("GAIN", 0)), offset=int(h.get("OFFSET", 0)),
            bin=int(h.get("XBINNING", 1)),
            full_scale=int(h.get("FULLSCAL", 65532)),
            bayer=_bayer_from_header(h),
            temperature=float(h["CCD-TEMP"]) if "CCD-TEMP" in h else None,
            origin="replay", path=str(path),
        )
        return raw.astype(np.uint16, copy=False), meta

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass

    def apply(self, **kw) -> list[str]:
        ignored = [k for k in kw if k in
                   ("gain", "exposure", "offset", "bin", "cooler", "target_temp")]
        if ignored:
            return [_("in replay, {names} come from the file and cannot change"
                      ).format(names=", ".join(ignored))]
        return []

    @property
    def progress(self) -> tuple[int, int]:
        return self._i, len(self.files)


def _bayer_from_header(h) -> Bayer:
    pat = str(h.get("BAYERPAT", "GRBG")).strip().upper()
    return {"RGGB": Bayer.RG, "BGGR": Bayer.BG,
            "GRBG": Bayer.GR, "GBRG": Bayer.GB}.get(pat, Bayer.GR)


def _timestamp(h) -> float:
    v = h.get("DATE-OBS")
    if not v:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v)).replace(
            tzinfo=UTC).timestamp()
    except Exception:
        return 0.0
