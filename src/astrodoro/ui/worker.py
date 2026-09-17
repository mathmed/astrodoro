from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from ..core import debayer, lucky, masters
from ..core.calibration import (
    calibrate,
    flat_master,
    hot_pixel_map,
    prepare_flat,
)
from ..core.cooling import CoolerController
from ..core.equatorial import PlatformMonitor
from ..core.focus import FocusMeter, SharpnessMeter, loupe, sharpness
from ..core.platform_align import LiveAlign
from ..core.recorder import Burst, Recorder, read_fits
from ..core.source import (
    CameraSource,
    FrameMeta,
    FrameSource,
    ReplaySource,
)
from ..core.stacker import LiveStacker
from ..core.stars import detect
from ..drivers import CameraError
from ..i18n import gettext as _
from ..settings import Settings

WINDOW_REFRESH = 2.0


@dataclass
class Config:
    mode: str = "frame"
    source: str = "camera"
    camera_index: int = 0
    replay_folder: str = ""
    replay_speed: float = 0.0
    replay_loop: bool = False
    bin: int = 2
    exposure: float = 5.0
    gain: int = 250
    offset: int = 20
    target_temp: float | None = None
    bias_path: str | None = None
    dark_path: str | None = None
    flat_path: str | None = None
    sigma_clip: float | None = 3.0
    ref_refresh: int = 10
    min_ref_stars: int = 10
    record: bool = True
    record_root: str = field(
        default_factory=lambda: str(Settings.load().path("capture_dir"))
    )
    bias_root: str = field(
        default_factory=lambda: str(Settings.load().path("bias_dir"))
    )
    dark_root: str = field(
        default_factory=lambda: str(Settings.load().path("dark_dir"))
    )
    flat_root: str = field(
        default_factory=lambda: str(Settings.load().path("flat_dir"))
    )
    target_name: str = ""
    record_every: int = 1
    burst_seconds: float = 30.0
    burst_frames: int = 0
    burst_compress: bool = False
    body: str = "moon"
    display_fps: float = 12.0
    compress: bool = True
    quality_weighting: bool = True
    strictness: str = "normal"
    cooling_ramp: float = 2.0
    warm_on_finish: bool = True

    @classmethod
    def from_settings(cls, s: Settings, **overrides) -> Config:
        base = {
            "bin": s.binning,
            "exposure": s.exposure_s,
            "gain": s.gain,
            "offset": s.offset,
            "burst_seconds": s.lucky_burst_seconds,
            "burst_frames": s.lucky_burst_frames,
            "burst_compress": s.lucky_burst_compress,
            "body": s.lucky_body,
            "display_fps": s.lucky_display_fps,
            "record_root": str(s.path("capture_dir")),
            "bias_root": str(s.path("bias_dir")),
            "dark_root": str(s.path("dark_dir")),
            "flat_root": str(s.path("flat_dir")),
        }
        base.update(overrides)
        return cls(**base)  # type: ignore[arg-type]


class CaptureWorker(QObject):
    frame = Signal(object, object, object, dict)
    focus = Signal(object, dict)
    log = Signal(str)
    failed = Signal(str)
    opened = Signal(dict)
    paused = Signal(bool)
    cooling = Signal(dict)
    bias_saved = Signal(str)
    dark_saved = Signal(str)
    flat_saved = Signal(str)
    calibration = Signal(dict)
    burst_state = Signal(bool)
    finished = Signal()

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._flags: set[str] = set()

        self.src: FrameSource | None = None
        self.stacker: LiveStacker | None = None
        self.focus_meter = FocusMeter()
        self.sharp_meter = SharpnessMeter()
        self.platform = PlatformMonitor()
        self.align: LiveAlign | None = None
        self.integrating = False
        self._realigning = False
        self.cool = CoolerController(ramp_c_per_min=cfg.cooling_ramp)
        self._t_cool = 0.0
        self._dark_temp: float | None = None
        self._dark_exposure: float | None = None
        self._n_master = {"bias": 20, "dark": 20, "flat": 20}
        self.recorder: Recorder | None = None
        self.burst: Burst | None = None
        self.info: dict = {}
        self._bias: np.ndarray | None = None
        self._dark: np.ndarray | None = None
        self._flat: np.ndarray | None = None
        self._hot: np.ndarray | None = None
        self._t_display = 0.0
        self._window: lucky.Box | None = None
        self._window_shape: tuple[int, int] = (0, 0)
        self._t_window = 0.0
        self._loupe_xy: tuple[float, float] | None = None

    def _open_camera(self):
        """The camera behind the source. Only the live source has one."""
        cam = getattr(self._source, "cam", None)
        if cam is None:
            raise RuntimeError("this source has no camera")
        return cam

    @property
    def _stack(self) -> LiveStacker:
        """The accumulator, which only exists while a stack is being built."""
        if self.stacker is None:
            raise RuntimeError("no stack is running")
        return self.stacker

    @property
    def _source(self) -> FrameSource:
        """The frame source, which only exists once the run has opened one."""
        if self.src is None:
            raise RuntimeError("the capture has not been opened")
        return self.src

    def request(self, **kw) -> None:
        with self._lock:
            self._pending.update(kw)

    def flag(self, name: str) -> None:
        with self._lock:
            self._flags.add(name)

    def stop(self) -> None:
        self._stop.set()

    def set_paused(self, on: bool) -> None:
        if on:
            self._pause.set()
        else:
            self._pause.clear()

    def set_loupe(self, xy) -> None:
        self._loupe_xy = xy

    def run(self) -> None:
        try:
            self._open()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
            self.finished.emit()
            return
        try:
            self._source.start()
            streaming = True
            failures = 0
            while not self._stop.is_set():
                self._apply_pending()
                self._step_cooling()

                if self._pause.is_set():
                    if streaming:
                        self._source.stop()
                        streaming = False
                        self.paused.emit(True)
                        self.log.emit(
                            _("paused — camera open, stack and recording preserved")
                        )
                    time.sleep(0.15)
                    continue
                if not streaming:
                    self._source.resume()
                    streaming = True
                    self.paused.emit(False)
                    self.log.emit(_("resumed"))

                try:
                    got = self._source.read()
                except CameraError as e:
                    if self._stop.is_set():
                        break
                    self.log.emit(_("capture: {error}").format(error=e))
                    continue
                if got is None:
                    self.log.emit(_("end of frames (replay)"))
                    break
                raw, meta = got
                try:
                    self._process(raw, meta)
                    failures = 0
                except Exception as e:
                    failures += 1
                    if failures == 1:
                        import traceback

                        self.log.emit(
                            _("error processing frame {index}: {error}").format(
                                index=meta.index, error=f"{type(e).__name__}: {e}"
                            )
                        )
                        for line in traceback.format_exc().strip().splitlines()[-4:]:
                            self.log.emit("  " + line)
                    else:
                        self.log.emit(
                            _(
                                "error on frame {index} ({count} in a row): {error}"
                            ).format(
                                index=meta.index, count=failures, error=type(e).__name__
                            )
                        )
                    if failures >= 10:
                        raise
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            self._finish()

    def _finish(self) -> None:
        self._stop_burst(_("burst closed with the session"))
        try:
            self._source.stop()
        except Exception:
            pass

        cam = getattr(self.src, "cam", None)
        if (
            self.cfg.warm_on_finish
            and cam is not None
            and getattr(cam, "supports_cooler", False)
            and self.cool.target is not None
        ):
            self.cool.set_target(None)
            deadline = time.time() + 180.0
            while time.time() < deadline:
                self._step_cooling(force=True)
                if self.cool.state.phase == "off":
                    break
                time.sleep(2.0)
            else:
                self.log.emit(_("warm-up cut off at the 3 minute limit"))
            try:
                cam.cooler = False
            except CameraError:
                pass

        st = self.stacker
        if st is not None and st.started:
            rec = self.recorder
            if rec is None:
                try:
                    rec = Recorder(
                        root=self.cfg.record_root,
                        target=self.cfg.target_name,
                        save_subs=False,
                    )
                    rec.begin(
                        self.info, {"mode": self.cfg.mode, "source": self.cfg.source}
                    )
                except Exception as e:
                    self.log.emit(
                        _("could not create the output folder: {error}").format(error=e)
                    )
                    rec = None
            if rec is not None:
                try:
                    snap = self._stats_snapshot()
                    rec.write_stack(st.result(), snap, final=True)
                    rec.end(snap)
                    self.log.emit(
                        _("final stack written to {path}").format(path=rec.session_dir)
                    )
                except Exception as e:
                    self.log.emit(
                        _("error writing the final stack: {error}").format(error=e)
                    )
        try:
            self._source.close()
        except Exception:
            pass
        self.finished.emit()

    def _open(self) -> None:
        c = self.cfg
        if c.source == "replay":
            if not c.replay_folder:
                raise RuntimeError(_("no replay folder selected"))
            self.src = ReplaySource(
                c.replay_folder, speed=c.replay_speed, loop=c.replay_loop
            )
        else:
            self.src = CameraSource(
                index=c.camera_index,
                bin=c.bin,
                exposure=c.exposure,
                gain=c.gain,
                offset=c.offset,
                target_temp=c.target_temp,
            )
        self.info = self._source.open()
        self._load_bias()
        self._load_dark()
        self._load_flat()
        self._new_stacker()

        if c.record and c.source == "camera":
            self.recorder = Recorder(
                root=c.record_root,
                target=c.target_name,
                save_subs=True,
                compress=c.compress,
                every=c.record_every,
            )
            d = self.recorder.begin(
                self.info,
                {
                    "exposure": c.exposure,
                    "gain": c.gain,
                    "offset": c.offset,
                    "bin": c.bin,
                    "sigma_clip": c.sigma_clip,
                    "bias": c.bias_path,
                    "dark": c.dark_path,
                    "flat": c.flat_path,
                },
            )
            self.log.emit(_("recording to {path}").format(path=d))

        if self.cfg.target_temp is not None and self.info.get("cooler"):
            self._request_cooling(True)

        self.opened.emit(self.info)
        if self.info.get("forced"):
            self.log.emit(
                _("pipeline linearised: {changes}").format(
                    changes=", ".join(self.info["forced"])
                )
            )
        if self.info.get("live") and self.info.get("port") != "USB3.0":
            self.log.emit(
                _(
                    "USB negotiated at {port}: ~475 ms of dead time per frame "
                    "(irrelevant for long subs, slow while focusing)"
                ).format(port=self.info["port"])
            )

    def _emit_calibration(self, kind: str, path: str, reason: str = "") -> None:
        self.calibration.emit({"kind": kind, "path": path, "reason": reason})
        if reason:
            self.log.emit(reason)

    def _read_master(self, kind: str, path: str | None):
        if not path:
            self._emit_calibration(kind, "")
            return None, None
        p = Path(path)
        try:
            d, hdr = read_fits(p)
        except Exception as e:
            self._emit_calibration(
                kind, "", _("unreadable {kind}: {error}").format(kind=kind, error=e)
            )
            return None, None
        d = d.astype(np.float32)
        shape = (self.info.get("height"), self.info.get("width"))
        if shape[0] and d.shape != shape:
            self._emit_calibration(
                kind,
                "",
                _("{kind} ignored: {got} != {want} (different bin?)").format(
                    kind=kind, got=d.shape, want=shape
                ),
            )
            return None, None
        cam = getattr(self.src, "cam", None)
        if cam is not None:
            try:
                for warning in masters.mismatch(
                    kind, hdr, masters.Setup.from_camera(cam)
                ):
                    self.log.emit(_("warning: {detail}").format(detail=warning))
            except (CameraError, TypeError, ValueError):
                pass
        return d, hdr

    def _load_bias(self) -> None:
        self._bias = None
        d, _hdr = self._read_master("bias", self.cfg.bias_path)
        if d is None:
            return
        self._bias = d
        self._emit_calibration("bias", str(self.cfg.bias_path))
        self.log.emit(
            _("bias: {name} (median {median:.0f})").format(
                name=Path(self.cfg.bias_path or "").name, median=np.median(d)
            )
        )
        if self._dark is not None:
            self.log.emit(
                _(
                    "the dark in force already contains the bias — the "
                    "bias is not subtracted twice"
                )
            )

    def _load_dark(self) -> None:
        self._dark = self._hot = None
        self._dark_temp = self._dark_exposure = None
        d, hdr = self._read_master("dark", self.cfg.dark_path)
        if d is None:
            return
        p = Path(self.cfg.dark_path or "")
        self._dark = d
        self._emit_calibration("dark", str(p))
        self.log.emit(
            _("dark: {name} (median {median:.0f})").format(
                name=p.name, median=np.median(d)
            )
        )
        for key, attr in (("CCD-TEMP", "_dark_temp"), ("EXPTIME", "_dark_exposure")):
            try:
                setattr(self, attr, float(hdr[key]))
            except (KeyError, TypeError, ValueError):
                setattr(self, attr, None)

        hot_path = p.with_name(p.name.replace(".fits", "_hot.npy"))
        if hot_path.exists():
            h = np.load(hot_path)
            if h.shape == d.shape:
                self._hot = h
                self.log.emit(_("hot pixel map: {n} px").format(n=int(h.sum())))

    def _load_flat(self) -> None:
        self._flat = None
        d, _hdr = self._read_master("flat", self.cfg.flat_path)
        if d is None:
            return
        p = Path(self.cfg.flat_path or "")
        self._flat = prepare_flat(d)
        if self._flat is None:
            self._emit_calibration("flat", "", _("invalid flat (median <= 0)"))
            return
        self._emit_calibration("flat", str(p))
        self.log.emit(
            _("flat: {name} (corner vignetting {percent:.0f}% of the centre)").format(
                name=p.name, percent=self._flat.min() * 100
            )
        )

    def _request_cooling(self, on: bool) -> None:
        cam = getattr(self.src, "cam", None)
        if cam is None or not cam.supports_cooler:
            return
        current = cam.temperature
        if on and self.cfg.target_temp is not None:
            self.cool.set_target(self.cfg.target_temp, current=current)
            self.log.emit(
                _(
                    "cooler: ramping from {current:+.1f} to "
                    "{target:+.1f} C at {rate:.1f} C/min"
                ).format(
                    current=current,
                    target=self.cfg.target_temp,
                    rate=self.cfg.cooling_ramp,
                )
            )
        else:
            self.cool.set_target(None)
            self.log.emit(_("cooler: warming up before switching off"))

    def _step_cooling(self, force: bool = False) -> None:
        cam = getattr(self.src, "cam", None)
        if cam is None or not cam.supports_cooler:
            return
        now = time.time()
        if not force and now - self._t_cool < 2.0:
            return
        self._t_cool = now
        try:
            current, power = cam.temperature, cam.cooler_power
            setpoint, enable = self.cool.update(current, power, now)
            if setpoint is not None:
                clamped = min(
                    max(setpoint, cam.min_target_temperature),
                    cam.max_target_temperature,
                )
                cam.target_temperature = float(clamped)
            if cam.cooler != enable:
                cam.cooler = enable
        except CameraError as e:
            self.log.emit(_("cooler: {error}").format(error=e))
            return
        st = self.cool.state
        self.cooling.emit(
            {
                "phase": st.phase,
                "current": st.current,
                "target": st.target,
                "setpoint": st.setpoint,
                "power": st.power,
                "stable": st.stable,
                "eta_s": st.eta_s,
                "ambient": st.ambient,
                "message": st.message,
                "dark_delta": (
                    None if self._dark_temp is None else st.current - self._dark_temp
                ),
            }
        )

    def _grab(self, kind: str, n: int, report=None) -> list[np.ndarray]:
        frames: list[np.ndarray] = []
        try:
            for k in range(n + 1):
                if self._stop.is_set():
                    self.log.emit(_("{kind} cancelled").format(kind=kind))
                    return []
                got = self._source.read()
                if got is None:
                    break
                if k == 0:
                    continue
                frames.append(got[0].astype(np.float32))
                if report is not None:
                    report(frames[-1], len(frames))
                elif len(frames) % 5 == 0 or len(frames) == n:
                    self.log.emit(f"  {kind} {len(frames)}/{n}")
        except CameraError as e:
            self.log.emit(_("{kind} interrupted: {error}").format(kind=kind, error=e))
        return frames

    def _master_camera(self, kind: str):
        cam = getattr(self.src, "cam", None)
        if cam is None:
            self.log.emit(
                _("recording a {kind} needs the camera, not a replay").format(kind=kind)
            )
        return cam

    def _capture_bias(self, n: int) -> None:
        cam = self._master_camera("bias")
        if cam is None:
            return
        session_exposure = cam.exposure
        try:
            shortest = cam.min_exposure
        except CameraError:
            shortest = 1e-4
        for m in self._source.apply(exposure=shortest):
            self.log.emit(m)
        try:
            setup = masters.Setup.from_camera(cam)
            self.log.emit(
                _(
                    "recording bias: {n} frames of {exp:.0f} us, gain {gain}, "
                    "offset {offset}, bin{bin} — keep the sensor capped"
                ).format(
                    n=n,
                    exp=setup.exposure * 1e6,
                    gain=setup.gain,
                    offset=setup.offset,
                    bin=setup.bin,
                )
            )
            frames = self._grab("bias", n)
            if len(frames) < 3:
                self.log.emit(_("not enough frames for a bias"))
                return
            master = masters.combine(frames)
            path = masters.write(self.cfg.bias_root, "bias", master, setup, len(frames))
            self.log.emit(
                _(
                    "master bias -> {path}  (median {median:.0f} ADU, "
                    "noise {sigma:.1f} ADU)"
                ).format(path=path, median=np.median(master), sigma=master.std())
            )
            self.cfg.bias_path = str(path)
            self._load_bias()
            self.bias_saved.emit(str(path))
        finally:
            for m in self._source.apply(exposure=session_exposure):
                self.log.emit(m)
            if not self._stop.is_set():
                try:
                    self._source.read()
                except CameraError:
                    pass
            self.log.emit(_("exposure back to {exp:.2f}s").format(exp=session_exposure))

    def _capture_dark(self, n: int) -> None:
        cam = self._master_camera("dark")
        if cam is None:
            return
        setup = masters.Setup.from_camera(cam)
        self.log.emit(
            _("recording dark: {n} frames of {exp:.2f}s, gain {gain}, bin{bin}").format(
                n=n, exp=setup.exposure, gain=setup.gain, bin=setup.bin
            )
            + (
                f", {setup.temperature:+.1f} °C"
                if setup.temperature is not None
                else ""
            )
        )
        frames = self._grab("dark", n)
        if len(frames) < 3:
            self.log.emit(_("not enough frames for a dark"))
            return

        master = masters.combine(frames)
        path = masters.write(self.cfg.dark_root, "dark", master, setup, len(frames))
        hot = hot_pixel_map(master)
        np.save(path.with_name(path.name.replace(".fits", "_hot.npy")), hot)
        self.log.emit(
            _("master dark -> {path}  ({n} hot pixels, {pct:.4f}%)").format(
                path=path, n=int(hot.sum()), pct=100 * hot.mean()
            )
        )

        self.cfg.dark_path = str(path)
        self._load_dark()
        self.dark_saved.emit(str(path))

    def _capture_flat(self, n: int) -> None:
        cam = self._master_camera("flat")
        if cam is None:
            return
        setup = masters.Setup.from_camera(cam)
        self.log.emit(
            _("recording flat: {n} frames of {exp:.2f}s, gain {gain}, bin{bin}").format(
                n=n, exp=setup.exposure, gain=setup.gain, bin=setup.bin
            )
        )

        def report(f, k):
            if k % 5 and k != n:
                return
            med = float(np.median(f))
            warn = "  " + _("SATURATING") if f.max() >= cam.full_scale * 0.98 else ""
            self.log.emit(
                _("  flat {k}/{n} — median {pct:.0f}% of scale").format(
                    k=k, n=n, pct=med / cam.full_scale * 100
                )
                + warn
            )

        frames = self._grab("flat", n, report=report)
        if len(frames) < 3:
            self.log.emit(_("not enough frames for a flat"))
            return

        pedestal, source = self._flat_pedestal(setup)
        master = flat_master(frames, pedestal)
        if pedestal is not None:
            self.log.emit(
                _("  {source} subtracted from the flat").format(source=source)
            )
        else:
            self.log.emit(
                _(
                    "  warning: no bias or matching dark — the offset "
                    "pedestal stays baked into the flat"
                )
            )
        med = float(np.median(master))
        if med <= 0:
            self.log.emit(_("invalid flat (median <= 0)"))
            return
        if med > cam.full_scale * 0.9:
            self.log.emit(_("warning: flat close to saturation — shorten the exposure"))
        elif med < cam.full_scale * 0.15:
            self.log.emit(
                _(
                    "warning: dark flat, it carries noise into the data "
                    "— add light or lengthen the exposure"
                )
            )

        path = masters.write(self.cfg.flat_root, "flat", master, setup, len(frames))

        norm = master / med
        self.log.emit(
            _(
                "master flat -> {path}  (median {pct:.0f}% of scale, "
                "corner at {corner:.0f}% of the centre)"
            ).format(path=path, pct=med / cam.full_scale * 100, corner=norm.min() * 100)
        )

        self.cfg.flat_path = str(path)
        self._load_flat()
        self.flat_saved.emit(str(path))

    def _flat_pedestal(self, setup: masters.Setup):
        if self._bias is not None:
            return self._bias, _("bias")
        have, want = self._dark_exposure, setup.exposure
        if (
            self._dark is not None
            and have is not None
            and abs(have - want) <= max(0.05 * want, 0.01)
        ):
            return self._dark, _("session dark")
        return None, ""

    def _new_stacker(self) -> None:
        h, w = self.info["height"], self.info["width"]
        c = self.cfg
        self.stacker = LiveStacker(
            (h, w),
            channels=3,
            sigma_clip=c.sigma_clip,
            saturation=debayer.LUM_SUM,
            ref_refresh=c.ref_refresh,
            min_ref_stars=c.min_ref_stars,
            quality_weighting=c.quality_weighting,
        )
        self.stacker.set_strictness(c.strictness)
        self.platform.clear()

    def _apply_pending(self) -> None:
        with self._lock:
            pending, flags = self._pending, self._flags
            self._pending, self._flags = {}, set()

        tec = {
            k: pending.pop(k) for k in list(pending) if k in ("target_temp", "cooler")
        }
        if "target_temp" in tec:
            self.cfg.target_temp = float(tec["target_temp"])
        if "cooler" in tec:
            self._request_cooling(bool(tec["cooler"]))
        elif "target_temp" in tec and self.cool.target is not None:
            self._request_cooling(True)

        align = {k: pending.pop(k) for k in list(pending) if k == "align"}
        proc = {
            k: pending.pop(k)
            for k in list(pending)
            if k
            in (
                "bias_path",
                "dark_path",
                "flat_path",
                "sigma_clip",
                "strictness",
                "bias_frames",
                "dark_frames",
                "flat_frames",
                "record_every",
                "target_name",
                "mode",
                "burst_seconds",
                "burst_frames",
            )
        }
        if pending:
            for m in self._source.apply(**pending):
                self.log.emit(m)
            if "bin" in pending:
                self.info.update(
                    width=self._source.geometry.width,
                    height=self._source.geometry.height,
                    bin=self._source.geometry.bin,
                )
                self._load_bias()
                self._load_dark()
                self._load_flat()
                self._new_stacker()
                self.log.emit(_("geometry changed: stack reset"))

        for k, v in proc.items():
            if k == "bias_path":
                self.cfg.bias_path = v or None
                self._load_bias()
            elif k == "dark_path":
                self.cfg.dark_path = v or None
                self._load_dark()
                if self.cfg.bias_path:
                    self._load_bias()
            elif k == "flat_path":
                self.cfg.flat_path = v or None
                self._load_flat()
            elif k in ("bias_frames", "dark_frames", "flat_frames"):
                self._n_master[k.removesuffix("_frames")] = max(int(v), 3)
            elif k == "strictness":
                if self.stacker:
                    self.stacker.set_strictness(str(v))
                self.cfg.strictness = str(v)
                self.log.emit(_("frame acceptance: {level}").format(level=v))
            elif k == "sigma_clip":
                self.cfg.sigma_clip = v
                self._new_stacker()
                self.log.emit(_("sigma clip changed: stack reset"))
            elif k == "record_every" and self.recorder:
                self.recorder.every = int(v)
            elif k == "burst_seconds":
                self.cfg.burst_seconds = max(float(v), 0.0)
            elif k == "burst_frames":
                self.cfg.burst_frames = max(int(v), 0)
            elif k == "body":
                if str(v) != self.cfg.body:
                    self._window = None
                self.cfg.body = str(v)
            elif k == "target_name":
                self.cfg.target_name = str(v)
                if self.recorder:
                    d = self.recorder.rename_target(str(v))
                    if d:
                        self.log.emit(_("recording to {path}").format(path=d))
            elif k == "mode":
                self._set_mode(str(v))

        if "align" in align:
            self._set_align(align["align"])

        if "reset" in flags:
            self._new_stacker()
            self.focus_meter = FocusMeter()
            self.log.emit(_("stack reset"))
        if "integrate_on" in flags:
            self.set_integrating(True)
        if "integrate_off" in flags:
            self.set_integrating(False)
        if "realign_on" in flags:
            if self.can_integrate and self.stacker and self.stacker.started:
                self._realigning = True
                self.set_integrating(False)
                self.log.emit(
                    _(
                        "paused for manual recentring — nudge the "
                        "tube until the arrow on the image "
                        "disappears"
                    )
                )
            else:
                self.log.emit(_("no reference yet to realign against"))
        if "realign_off" in flags:
            if self._realigning:
                self._realigning = False
                if self.stacker:
                    self.stacker.new_segment()
                self.set_integrating(True)
        if "capture_bias" in flags:
            self._capture_bias(self._n_master["bias"])
        if "capture_dark" in flags:
            self._capture_dark(self._n_master["dark"])
        if "capture_flat" in flags:
            self._capture_flat(self._n_master["flat"])
        if "burst_start" in flags:
            self._start_burst()
        if "burst_stop" in flags:
            self._stop_burst(_("burst stopped"))
        if "reset_focus_best" in flags:
            self.focus_meter.reset_best()
            self.sharp_meter.reset_best()
            self.log.emit(_("session best focus reset"))

    def _set_align(self, minutes) -> None:
        if not minutes:
            if self.align is not None:
                self.log.emit(_("platform alignment stopped"))
            self.align = None
            return
        if self.align is None:
            self.align = LiveAlign(window_s=float(minutes) * 60.0)
            self.log.emit(
                _(
                    "aligning the platform — keep a field of stars in view; "
                    "the reading restarts every time the tube moves"
                )
            )

    @property
    def can_integrate(self) -> bool:
        return self.cfg.mode == "stack"

    @property
    def stacking(self) -> bool:
        return self.integrating and self.can_integrate

    def set_integrating(self, on: bool) -> None:
        before = self.stacking
        self.integrating = bool(on)
        if self.stacking and not before:
            if not self.platform.started:
                self.platform.clear()
                self.platform.started = True
                self.log.emit(
                    _(
                        "integrating: stacking, recording and measuring "
                        "the residual rotation"
                    )
                )
            else:
                self.log.emit(_("integration resumed on the existing accumulator"))
        elif before and not self.stacking:
            self.log.emit(_("integration stopped — capture continues, stack preserved"))

    def _set_mode(self, mode: str) -> None:
        before = self.stacking
        if self.cfg.mode == "lucky" and mode != "lucky":
            self._stop_burst(_("burst closed on leaving PLANETS"))
        self.cfg.mode = mode
        now = self.stacking
        if before and not now:
            self.log.emit(
                _("{mode}: integration on hold, stack preserved").format(mode=mode)
            )
        elif now and not before:
            self.log.emit(_("integration resumed"))

    def _process(self, raw: np.ndarray, meta: FrameMeta) -> None:
        st = self._stack
        t0 = time.perf_counter()

        f = calibrate(
            raw, meta, dark=self._dark, flat=self._flat, hot=self._hot, bias=self._bias
        )
        lum = debayer.cfa_to_luminance(f)

        if self.cfg.mode == "lucky":
            self._process_lucky(raw, meta, f, lum, t0)
            return

        cfa16 = (f * 65535).astype(np.uint16)
        rgb = (
            debayer.to_rgb(cfa16, meta.bayer, quality="linear").astype(np.float32)
            / 65535.0
        )

        if not self.stacking:
            sf = detect(
                lum, scale=2.0, max_stars=60, central=0.70, saturation=debayer.LUM_SUM
            )
            if self.align is not None:
                self.align.add(sf.xy, meta.timestamp)
            sample = (
                self.focus_meter.add(sf, meta.temperature) if len(sf) >= 3 else None
            )
            stats = self._stats_snapshot(None, None, meta, t0)
            realigning = self._realigning and self.can_integrate
            stats.update(
                accepted=None,
                reason="",
                n_stars=len(sf),
                fwhm=sf.median_fwhm,
                matched=0,
                rms=float("nan"),
                rotation=0.0,
                frame_ms=0.0,
                realign=self._realign_info(sf) if realigning else None,
            )
            self.frame.emit(rgb, st.result() if st.started else None, cfa16, stats)
            self._emit_focus(sample, sf, lum, outcome=None)
            return

        o = st.add(rgb, lum, exposure=meta.exposure, lum_scale=2.0)
        al = o.alignment

        if al is not None and al.ok and o.accepted:
            dx, dy = st.drift()
            self.platform.add(al.rotation_deg, dx, dy, o.fwhm)
            if self.align is not None:
                self.align.add_rotation(al.rotation_deg, meta.timestamp)

        sample = (
            self.focus_meter.add(_StarProxy(o), meta.temperature)
            if o.n_stars >= 3
            else None
        )

        if self.recorder:
            try:
                self.recorder.write_sub(raw, meta)
            except Exception as e:
                self.log.emit(_("error writing sub: {error}").format(error=e))

        stats = self._stats_snapshot(o, al, meta, t0)
        self.frame.emit(rgb, st.result() if st.started else None, cfa16, stats)
        self._emit_focus(sample, None, lum, outcome=o)

    def _process_lucky(self, raw, meta, f, lum, t0) -> None:
        box = self._body_window(lum)
        lv = lucky.levels(f, box.scaled(2.0) if box else None)
        value = sharpness(box.crop(lum) if box else lum)
        sample = self.sharp_meter.add(value, meta.temperature)

        if self.burst is not None:
            try:
                self.burst.write(raw, meta, sharpness=value)
            except Exception as e:
                self._stop_burst(_("burst interrupted: {error}").format(error=e))
            else:
                if self.burst.done:
                    self._stop_burst()

        if not self._due_for_display():
            return

        centre = self._follow_body(lum, box)
        cfa16 = (f * 65535).astype(np.uint16)
        rgb = (
            debayer.to_rgb(cfa16, meta.bayer, quality="linear").astype(np.float32)
            / 65535.0
        )
        stats = self._stats_snapshot(None, None, meta, t0)
        stats.update(
            accepted=None,
            reason="",
            n_stars=0,
            fwhm=float("nan"),
            matched=0,
            rms=float("nan"),
            rotation=0.0,
            frame_ms=0.0,
            lucky=self._lucky_stats(lv, value, box, centre),
        )
        self.frame.emit(rgb, None, None, stats)
        self._emit_sharpness(sample, lum, box)

    def _due_for_display(self) -> bool:
        fps = self.cfg.display_fps
        if fps <= 0:
            return True
        now = time.monotonic()
        if now - self._t_display < 1.0 / fps:
            return False
        self._t_display = now
        return True

    def _body_window(self, lum: np.ndarray) -> lucky.Box | None:
        shape = (lum.shape[0], lum.shape[1])
        if self._window is not None and shape != self._window_shape:
            self._window = None
        now = time.monotonic()
        if self._window is not None and now - self._t_window < WINDOW_REFRESH:
            return self._window
        self._t_window = now
        self._window_shape = shape
        found = lucky.window(lum, side=(self._window.w if self._window else None))
        if found is not None:
            self._window = found
        return self._window

    def _follow_body(self, lum: np.ndarray, box) -> tuple[float, float] | None:
        if box is None:
            return None
        cx, cy = lucky.centroid(lum, box, stride=lucky.tracking_stride(box))
        self._window = box.recentred(cx, cy, lum.shape)
        return (cx * 2.0, cy * 2.0)

    def _lucky_stats(self, lv, value: float, box, centre=None) -> dict:
        b = self.sharp_meter.best
        d = {
            "peak": lv.peak,
            "clipped": lv.clipped,
            "lit": lv.lit,
            "factor": lv.factor,
            "advice": lv.verdict(),
            "sharpness": value,
            "window": (box.scaled(2.0).w if box else 0),
            "cx": (centre[0] if centre else float("nan")),
            "cy": (centre[1] if centre else float("nan")),
            "best": b.value if b else float("nan"),
            "recording": self.burst is not None,
            "frames": 0,
            "elapsed": 0.0,
            "progress": 0.0,
            "folder": "",
        }
        if self.burst is not None:
            d.update(
                frames=self.burst.n,
                elapsed=self.burst.elapsed,
                progress=self.burst.progress,
                folder=str(self.burst.session_dir or ""),
            )
        return d

    def _emit_sharpness(self, sample, lum: np.ndarray, box=None) -> None:
        m = self.sharp_meter
        default = (
            box.centre if box is not None else (lum.shape[1] / 2.0, lum.shape[0] / 2.0)
        )
        xy = (
            (self._loupe_xy[0] / 2.0, self._loupe_xy[1] / 2.0)
            if self._loupe_xy
            else default
        )
        crop = loupe(lum, xy, half=28, zoom=5)
        self.focus.emit(
            crop,
            {
                "hfr": sample.value if sample else float("nan"),
                "fwhm": float("nan"),
                "n_stars": 0,
                "trend": float("nan"),
                "ratio": m.ratio_to_best(),
                "best": m.best.value if m.best else float("nan"),
                "verdict": m.verdict(),
                "series": m.series(),
            },
        )

    def _start_burst(self) -> None:
        if self.burst is not None:
            return
        if not isinstance(self.src, CameraSource):
            self.log.emit(_("a burst needs the camera, not a replay"))
            self.burst_state.emit(False)
            return
        c = self.cfg
        rec = Recorder(
            root=c.record_root,
            target=c.target_name or lucky.BODIES[c.body].label,
            save_subs=True,
            compress=c.burst_compress,
            every=1,
            sub_suffix=".fit",
        )
        b = Burst(recorder=rec, max_frames=c.burst_frames, max_seconds=c.burst_seconds)
        cam = self._open_camera()
        g = cam.geometry
        try:
            d = b.begin(
                self.info,
                {
                    "exposure": cam.exposure,
                    "gain": cam.gain,
                    "offset": cam.offset,
                    "bin": g.bin,
                    "bias": c.bias_path,
                    "dark": c.dark_path,
                    "flat": c.flat_path,
                    "mode": "lucky",
                    "body": c.body,
                },
            )
        except OSError as e:
            self.log.emit(
                _("could not create the burst folder: {error}").format(error=e)
            )
            self.burst_state.emit(False)
            return
        self.burst = b
        self.log.emit(_("burst recording to {path}").format(path=d))
        self.burst_state.emit(True)

    def _stop_burst(self, reason: str = "") -> None:
        b, self.burst = self.burst, None
        self.burst_state.emit(False)
        if b is None:
            return
        st = b.end()
        if reason:
            self.log.emit(reason)
        self.log.emit(
            _("burst: {n} frames in {seconds:.1f} s ({fps:.1f} fps) -> {path}").format(
                n=st["frames"], seconds=st["seconds"], fps=st["fps"], path=b.session_dir
            )
        )

    def _emit_focus(self, sample, starfield, lum, outcome) -> None:
        fs = self.focus_meter
        xy = self._loupe_xy
        if xy is None:
            if outcome is not None:
                xy = outcome.brightest
            elif starfield is not None and len(starfield):
                i = int(np.argmax(starfield.flux))
                xy = (float(starfield.xy[i, 0]), float(starfield.xy[i, 1]))
        crop = loupe(lum, (xy[0] / 2.0, xy[1] / 2.0), half=28, zoom=5) if xy else None
        self.focus.emit(
            crop,
            {
                "hfr": sample.hfr if sample else float("nan"),
                "fwhm": sample.fwhm if sample else float("nan"),
                "n_stars": (
                    len(starfield)
                    if starfield is not None
                    else (outcome.n_stars if outcome else 0)
                ),
                "trend": fs.trend(),
                "ratio": fs.ratio_to_best(),
                "best": fs.best.hfr if fs.best else float("nan"),
                "verdict": fs.verdict(),
                "series": fs.series(),
            },
        )

    def _realign_info(self, stars) -> dict:
        fail = {
            "ok": False,
            "dx": 0.0,
            "dy": 0.0,
            "distance": 0.0,
            "rotation_deg": 0.0,
            "reason": "",
        }
        al = self.stacker.preview_alignment(stars) if self.stacker else None
        if al is None:
            return {**fail, "reason": _("too few stars")}
        if not al.ok:
            return {**fail, "reason": al.reason}
        cx = self.info.get("width", 0) / 2.0
        cy = self.info.get("height", 0) / 2.0
        if al.matrix is None:
            return {}
        a, t = al.matrix[:, :2], al.matrix[:, 2]
        try:
            src = np.linalg.solve(a, np.array([cx, cy]) - t)
        except np.linalg.LinAlgError:
            return {**fail, "reason": _("degenerate alignment")}
        dx, dy = float(src[0] - cx), float(src[1] - cy)
        return {
            "ok": True,
            "dx": dx,
            "dy": dy,
            "distance": float(np.hypot(dx, dy)),
            "rotation_deg": al.rotation_deg,
            "reason": "",
        }

    def _stats_snapshot(self, o=None, al=None, meta=None, t0=None) -> dict:
        st = self.stacker
        shape = (self.info.get("height", 1), self.info.get("width", 1))
        s = {
            "mode": self.cfg.mode,
            "stacking": self.stacking,
            "integrating": self.integrating,
            "can_integrate": self.can_integrate,
            "realigning": self._realigning and self.can_integrate,
            "n_stacked": st.n_stacked if st else 0,
            "n_rejected": st.n_rejected if st else 0,
            "rejections": dict(st.rejections) if st else {},
            "integration": st.total_exposure if st else 0.0,
            "integration_eff": st.weighted_exposure if st else 0.0,
            "overlap": st.overlap_fraction() if st else 0.0,
            "drift": st.drift() if st else (0.0, 0.0),
            "platform": self.platform.report(shape),
            "platform_advice": self.platform.advice(shape),
            "align": self.align.status() if self.align else None,
            "recording": (
                self.burst.recorder.disk_report()
                if self.burst
                else (self.recorder.disk_report() if self.recorder else "")
            ),
        }
        if o is not None:
            s.update(
                accepted=o.accepted,
                reason=o.reason,
                n_stars=o.n_stars,
                fwhm=o.fwhm,
                hfr=o.hfr,
                elong=o.elong,
                halo=o.halo,
                limits=dict(o.limits),
                frame_ms=o.elapsed_ms,
                proc_ms=(time.perf_counter() - t0) * 1e3 if t0 else 0.0,
                matched=al.n_matched if al else 0,
                rms=al.rms if al else float("nan"),
                rotation=al.rotation_deg if al else 0.0,
                weight=o.weight,
                score=o.score,
            )
        if meta is not None:
            s.update(
                temp=meta.temperature,
                cooler_power=meta.cooler_power,
                exposure=meta.exposure,
                gain=meta.gain,
                frame_index=meta.index,
                origin=meta.origin,
                bayer=meta.bayer,
            )
        if isinstance(self.src, ReplaySource):
            i, n = self.src.progress
            s["replay_progress"] = (i, n)
        return s


class _StarProxy:
    def __init__(self, outcome):
        self._o = outcome

    @property
    def median_hfr(self) -> float:
        return getattr(self._o, "hfr", float("nan"))

    @property
    def median_fwhm(self) -> float:
        return self._o.fwhm

    @property
    def peak(self):
        return np.empty(0)

    def __len__(self) -> int:
        return self._o.n_stars
