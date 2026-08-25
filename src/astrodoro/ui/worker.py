"""Capture, calibration, stacking, focusing and recording thread.

The GUI only renders what comes out of here. Parameter changes arrive through a
lock-protected dictionary and are applied between frames — not through Qt slots,
because the loop is blocked inside SVBGetVideoData for the whole exposure and
this thread's event loop would not run.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from ..core import debayer
from ..core.calibration import calibrate, hot_pixel_map
from ..core.cooling import CoolerController
from ..core.equatorial import PlatformMonitor
from ..core.focus import FocusMeter, loupe
from ..core.recorder import Recorder, read_fits
from ..core.source import CameraSource, FrameMeta, ReplaySource
from ..core.stacker import LiveStacker
from ..core.stars import detect
from ..drivers.svbony.sdk import SVBError
from ..i18n import gettext as _
from ..settings import Settings


@dataclass
class Config:
    # The mode decides what to do with the frames, not just what to display:
    #   frame/focus  -> capture + star detection (for HFR). No stacking, no
    #                   recording, no rotation sampling.
    #   stack/config -> ALLOW integrating, but do not start it. The user does,
    #                   explicitly. config is included so that opening the
    #                   settings panel mid-session does not stop the stack.
    mode: str = "frame"
    # source
    source: str = "camera"            # "camera" | "replay"
    camera_index: int = 0
    replay_folder: str = ""
    replay_speed: float = 0.0
    replay_loop: bool = False
    # camera
    bin: int = 2
    exposure: float = 5.0
    gain: int = 250
    offset: int = 20
    target_temp: float | None = None
    # processing
    dark_path: str | None = None
    flat_path: str | None = None
    sigma_clip: float | None = 3.0
    ref_refresh: int = 10
    min_ref_stars: int = 10
    # recording
    record: bool = True
    # Resolved from the user settings at construction, not hardcoded: a relative
    # default here would write into the current working directory, which for a
    # checkout means into the repository.
    record_root: str = field(
        default_factory=lambda: str(Settings.load().path("capture_dir")))
    dark_root: str = field(
        default_factory=lambda: str(Settings.load().path("dark_dir")))
    flat_root: str = field(
        default_factory=lambda: str(Settings.load().path("flat_dir")))
    target_name: str = ""
    record_every: int = 1
    compress: bool = True
    quality_weighting: bool = True
    strictness: str = "normal"
    cooling_ramp: float = 2.0
    warm_on_finish: bool = True

    @classmethod
    def from_settings(cls, s: Settings, **overrides) -> Config:
        base = {"bin": s.binning, "exposure": s.exposure_s, "gain": s.gain,
                "offset": s.offset,
                "record_root": str(s.path("capture_dir")),
                "dark_root": str(s.path("dark_dir")),
                "flat_root": str(s.path("flat_dir"))}
        base.update(overrides)
        return cls(**base)


class CaptureWorker(QObject):
    # The calibrated mosaic (uint16) travels alongside the RGB because that is
    # what the frame history archives — 6x lighter than the RGB, see ui/history.py
    frame = Signal(object, object, object, dict)   # live_rgb, stack|None, cfa, stats
    focus = Signal(object, dict)            # loupe|None, focus data
    log = Signal(str)
    failed = Signal(str)
    opened = Signal(dict)
    paused = Signal(bool)
    cooling = Signal(dict)
    dark_saved = Signal(str)
    flat_saved = Signal(str)
    finished = Signal()

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._flags: set[str] = set()

        self.src = None
        self.stacker: LiveStacker | None = None
        self.focus_meter = FocusMeter()
        self.platform = PlatformMonitor()
        # Integrating is a commitment, not a view: it starts accumulating,
        # writes subs to disk and consumes platform travel. Switching modes must
        # not trigger it — the user starts it.
        self.integrating = False
        self.cool = CoolerController(ramp_c_per_min=cfg.cooling_ramp)
        self._t_cool = 0.0
        self._dark_temp: float | None = None
        self._n_dark = 20
        self._n_flat = 20
        self.recorder: Recorder | None = None
        self.info: dict = {}
        self._dark: np.ndarray | None = None
        self._flat: np.ndarray | None = None
        self._hot: np.ndarray | None = None
        self._last_lum: np.ndarray | None = None
        self._loupe_xy: tuple[float, float] | None = None

    # ------------------------------------------------- commands (GUI thread)
    def request(self, **kw) -> None:
        with self._lock:
            self._pending.update(kw)

    def flag(self, name: str) -> None:
        with self._lock:
            self._flags.add(name)

    def stop(self) -> None:
        self._stop.set()

    def set_paused(self, on: bool) -> None:
        """Pausing preserves everything: the camera stays open, and the stack,
        the recording and the platform monitor stay intact. Only frame reading
        stops."""
        if on:
            self._pause.set()
        else:
            self._pause.clear()

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    def set_loupe(self, xy) -> None:
        self._loupe_xy = xy

    def last_luminance(self) -> np.ndarray | None:
        return self._last_lum

    # ------------------------------------------------- loop (own thread)
    def run(self) -> None:
        try:
            self._open()
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
            self.finished.emit()
            return
        try:
            self.src.start()
            streaming = True
            failures = 0
            while not self._stop.is_set():
                self._apply_pending()
                self._step_cooling()

                if self._pause.is_set():
                    if streaming:
                        self.src.stop()
                        streaming = False
                        self.paused.emit(True)
                        self.log.emit(_("paused — camera open, stack and "
                                        "recording preserved"))
                    time.sleep(0.15)
                    continue
                if not streaming:
                    self.src.resume()
                    streaming = True
                    self.paused.emit(False)
                    self.log.emit(_("resumed"))

                try:
                    got = self.src.read()
                except SVBError as e:
                    if self._stop.is_set():
                        break
                    self.log.emit(_("capture: {error}").format(error=e))
                    continue
                if got is None:
                    self.log.emit(_("end of frames (replay)"))
                    break
                raw, meta = got
                # An error processing one frame must not cost the session. In the
                # field, losing the camera and the accumulator over an unforeseen
                # case is far worse than skipping a frame. Consecutive failures
                # still abort, so a persistent problem is not masked.
                try:
                    self._process(raw, meta)
                    failures = 0
                except Exception as e:
                    failures += 1
                    if failures == 1:
                        import traceback
                        self.log.emit(_("error processing frame {index}: {error}"
                                        ).format(index=meta.index,
                                                 error=f"{type(e).__name__}: {e}"))
                        for line in traceback.format_exc().strip().splitlines()[-4:]:
                            self.log.emit("  " + line)
                    else:
                        self.log.emit(
                            _("error on frame {index} ({count} in a row): {error}"
                              ).format(index=meta.index, count=failures,
                                       error=type(e).__name__))
                    if failures >= 10:
                        raise
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            self._finish()

    def _finish(self) -> None:
        """Always shut down having written the stack.

        Stopping used to discard the accumulator along with the worker — forty
        minutes of integration lost in one click. If there was no recorder (subs
        off, or a replay), one is created on the spot just to save the result.
        """
        try:
            self.src.stop()
        except Exception:
            pass

        # Warm up before switching off: a cold sensor with the TEC cut condenses.
        cam = getattr(self.src, "cam", None)
        if (self.cfg.warm_on_finish and cam is not None
                and getattr(cam, "supports_cooler", False)
                and self.cool.target is not None):
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
            except SVBError:
                pass

        st = self.stacker
        if st is not None and st.started:
            rec = self.recorder
            if rec is None:
                try:
                    rec = Recorder(root=self.cfg.record_root,
                                   target=self.cfg.target_name, save_subs=False)
                    rec.begin(self.info, {"mode": self.cfg.mode,
                                          "source": self.cfg.source})
                except Exception as e:
                    self.log.emit(_("could not create the output folder: {error}"
                                    ).format(error=e))
                    rec = None
            if rec is not None:
                try:
                    snap = self._stats_snapshot()
                    rec.write_stack(st.result(), snap, final=True)
                    rec.end(snap)
                    self.log.emit(_("final stack written to {path}").format(
                        path=rec.session_dir))
                except Exception as e:
                    self.log.emit(_("error writing the final stack: {error}"
                                    ).format(error=e))
        try:
            self.src.close()
        except Exception:
            pass
        self.finished.emit()

    # --------------------------------------------------------------- internal
    def _open(self) -> None:
        c = self.cfg
        if c.source == "replay":
            if not c.replay_folder:
                raise RuntimeError(_("no replay folder selected"))
            self.src = ReplaySource(c.replay_folder, speed=c.replay_speed,
                                    loop=c.replay_loop)
        else:
            self.src = CameraSource(index=c.camera_index, bin=c.bin,
                                    exposure=c.exposure, gain=c.gain,
                                    offset=c.offset, target_temp=c.target_temp)
        self.info = self.src.open()
        self._load_dark()
        self._load_flat()
        self._new_stacker()

        if c.record and c.source == "camera":
            self.recorder = Recorder(root=c.record_root, target=c.target_name,
                                     save_subs=True, compress=c.compress,
                                     every=c.record_every)
            d = self.recorder.begin(self.info, {
                "exposure": c.exposure, "gain": c.gain, "offset": c.offset,
                "bin": c.bin, "dark": c.dark_path, "sigma_clip": c.sigma_clip,
            })
            self.log.emit(_("recording to {path}").format(path=d))

        if self.cfg.target_temp is not None and self.info.get("cooler"):
            self._request_cooling(True)

        self.opened.emit(self.info)
        if self.info.get("forced"):
            self.log.emit(_("pipeline linearised: {changes}").format(
                changes=", ".join(self.info["forced"])))
        if self.info.get("live") and self.info.get("port") != "USB3.0":
            self.log.emit(
                _("USB negotiated at {port}: ~475 ms of dead time per frame "
                  "(irrelevant for long subs, slow while focusing)").format(
                      port=self.info["port"]))

    def _load_dark(self) -> None:
        self._dark = self._hot = None
        if not self.cfg.dark_path:
            return
        p = Path(self.cfg.dark_path)
        try:
            d, hdr = read_fits(p)
        except Exception as e:
            self.log.emit(_("unreadable dark: {error}").format(error=e))
            return
        d = d.astype(np.float32)
        shape = (self.info.get("height"), self.info.get("width"))
        if shape[0] and d.shape != shape:
            self.log.emit(_("dark ignored: {got} != {want} (different bin?)"
                            ).format(got=d.shape, want=shape))
            return
        self._dark = d
        self.log.emit(_("dark: {name} (median {median:.0f})").format(
            name=p.name, median=np.median(d)))
        # A dark is only valid at the temperature it was taken: the thermal
        # signal doubles every ~6 C, so 3 C of difference already leaves a
        # visible residual.
        try:
            if "CCD-TEMP" in hdr:
                self._dark_temp = float(hdr["CCD-TEMP"])
                cam = getattr(self.src, "cam", None)
                if cam is not None and cam.supports_cooler:
                    delta = cam.temperature - self._dark_temp
                    if abs(delta) > 2.0:
                        self.log.emit(
                            _("warning: dark taken at {dark:+.1f} C, sensor now "
                              "at {now:+.1f} C ({delta:+.1f} C apart)").format(
                                  dark=self._dark_temp, now=cam.temperature,
                                  delta=delta))
        except Exception:
            pass
        hot_path = p.with_name(p.name.replace(".fits", "_hot.npy"))
        if hot_path.exists():
            h = np.load(hot_path)
            if h.shape == d.shape:
                self._hot = h
                self.log.emit(_("hot pixel map: {n} px").format(n=int(h.sum())))

    def _load_flat(self) -> None:
        """Master flat, normalised to a median of 1.

        A fast Newtonian with a 4/3" sensor has visible vignetting, plus dust on
        the sensor window. Without a flat the corners go dark and the autostretch
        gives it away. On a colour sensor the flat is applied to the raw mosaic,
        which corrects vignetting and the channel response difference at once —
        the right behaviour for OSC.
        """
        self._flat = None
        if not self.cfg.flat_path:
            return
        p = Path(self.cfg.flat_path)
        try:
            d, _hdr = read_fits(p)
        except Exception as e:
            self.log.emit(_("unreadable flat: {error}").format(error=e))
            return
        d = d.astype(np.float32)
        shape = (self.info.get("height"), self.info.get("width"))
        if shape[0] and d.shape != shape:
            self.log.emit(_("flat ignored: {got} != {want} (different bin?)"
                            ).format(got=d.shape, want=shape))
            return
        m = float(np.median(d))
        if m <= 0:
            self.log.emit(_("invalid flat (median <= 0)"))
            return
        # Normalise and clamp the divisor: a very low flat pixel would amplify
        # noise without limit in the corner.
        self._flat = np.clip(d / m, 0.15, 4.0).astype(np.float32)
        self.log.emit(_("flat: {name} (corner vignetting {percent:.0f}% of the "
                        "centre)").format(name=p.name,
                                          percent=self._flat.min() * 100))

    # ------------------------------------------------------------------ cooling
    def _request_cooling(self, on: bool) -> None:
        cam = getattr(self.src, "cam", None)
        if cam is None or not cam.supports_cooler:
            return
        current = cam.temperature
        if on and self.cfg.target_temp is not None:
            self.cool.set_target(self.cfg.target_temp, current=current)
            self.log.emit(_("cooler: ramping from {current:+.1f} to "
                            "{target:+.1f} C at {rate:.1f} C/min").format(
                                current=current, target=self.cfg.target_temp,
                                rate=self.cfg.cooling_ramp))
        else:
            self.cool.set_target(None)
            self.log.emit(_("cooler: warming up before switching off"))

    def _step_cooling(self, force: bool = False) -> None:
        """One controller step. Called from the loop, including while paused —
        cooling takes fifteen to twenty minutes and you want it to progress
        while you frame and focus."""
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
                cam.target_temperature = float(setpoint)
            if cam.cooler != enable:
                cam.cooler = enable
        except SVBError as e:
            self.log.emit(_("cooler: {error}").format(error=e))
            return
        st = self.cool.state
        self.cooling.emit({
            "phase": st.phase, "current": st.current, "target": st.target,
            "setpoint": st.setpoint, "power": st.power, "stable": st.stable,
            "eta_s": st.eta_s, "ambient": st.ambient, "message": st.message,
            "dark_delta": (None if self._dark_temp is None
                           else st.current - self._dark_temp),
        })

    # ------------------------------------------------------- calibration frames
    def _capture_dark(self, n: int) -> None:
        """Record a master dark with the parameters of the session in progress.

        Doing it here rather than in a separate command is the point: a dark
        needs to match exposure, gain, offset, bin AND temperature with the
        lights. Mid-session all five are already right by construction — running
        a separate command later forces you to reproduce everything, and the
        temperature is the one that most often gets away.
        """
        cam = getattr(self.src, "cam", None)
        if cam is None:
            self.log.emit(_("recording a dark needs the camera, not a replay"))
            return
        exp, gain, offset = cam.exposure, cam.gain, cam.offset
        g = cam.geometry
        temp = cam.temperature if cam.supports_cooler else None
        self.log.emit(_("recording dark: {n} frames of {exp:.2f}s, gain {gain}, "
                        "bin{bin}").format(n=n, exp=exp, gain=gain, bin=g.bin)
                      + (f", {temp:+.1f} °C" if temp is not None else ""))

        frames = []
        try:
            for k in range(n + 1):
                if self._stop.is_set():
                    self.log.emit(_("dark cancelled"))
                    return
                got = self.src.read()
                if got is None:
                    break
                if k == 0:
                    continue                      # the first one is stale
                frames.append(got[0].astype(np.float32))
                if len(frames) % 5 == 0 or len(frames) == n:
                    self.log.emit(f"  dark {len(frames)}/{n}")
        except SVBError as e:
            self.log.emit(_("dark interrupted: {error}").format(error=e))
        if len(frames) < 3:
            self.log.emit(_("not enough frames for a dark"))
            return

        master = np.median(np.stack(frames), axis=0).astype(np.float32)
        folder = Path(self.cfg.dark_root)
        folder.mkdir(parents=True, exist_ok=True)
        name = (f"dark_g{gain}_o{offset}_e{exp:.2f}s_bin{g.bin}"
                + (f"_{temp:+.0f}C" if temp is not None else "") + ".fits")
        path = folder / name

        from astropy.io import fits
        hdr = fits.Header()
        hdr["IMAGETYP"] = "DARK"
        hdr["EXPTIME"] = exp
        hdr["GAIN"] = gain
        hdr["OFFSET"] = offset
        hdr["XBINNING"] = g.bin
        hdr["NCOMBINE"] = len(frames)
        hdr["BAYERPAT"] = cam.bayer.fits_name
        hdr["FULLSCAL"] = cam.full_scale
        if temp is not None:
            hdr["CCD-TEMP"] = round(temp, 2)
        fits.PrimaryHDU(master, hdr).writeto(path, overwrite=True)

        hot = hot_pixel_map(master)
        np.save(path.with_name(path.name.replace(".fits", "_hot.npy")), hot)
        self.log.emit(_("master dark -> {path}  ({n} hot pixels, {pct:.4f}%)"
                        ).format(path=path, n=int(hot.sum()),
                                 pct=100 * hot.mean()))

        self.cfg.dark_path = str(path)
        self._load_dark()
        self.dark_saved.emit(str(path))

    def _capture_flat(self, n: int) -> None:
        """Record a master flat with the parameters of the session in progress.

        Same reason as the dark: gain and bin must match the lights, and
        mid-session they already do. The exposure is different — a flat is taken
        pointing at an illuminated surface, and the histogram should land near
        half scale. The program does not change the exposure by itself: doing so
        here would upset the session, and you are the one framing the white sheet.

        The loaded dark is subtracted when the shape matches: without it the
        sensor's offset pedestal enters the flat as a multiplicative error.
        """
        cam = getattr(self.src, "cam", None)
        if cam is None:
            self.log.emit(_("recording a flat needs the camera, not a replay"))
            return
        exp, gain = cam.exposure, cam.gain
        g = cam.geometry
        self.log.emit(_("recording flat: {n} frames of {exp:.2f}s, gain {gain}, "
                        "bin{bin}").format(n=n, exp=exp, gain=gain, bin=g.bin))

        frames = []
        try:
            for k in range(n + 1):
                if self._stop.is_set():
                    self.log.emit(_("flat cancelled"))
                    return
                got = self.src.read()
                if got is None:
                    break
                if k == 0:
                    continue                      # the first one is stale
                f = got[0].astype(np.float32)
                frames.append(f)
                if len(frames) % 5 == 0 or len(frames) == n:
                    med = float(np.median(f))
                    warn = ("  " + _("SATURATING")
                            if f.max() >= cam.full_scale * 0.98 else "")
                    self.log.emit(
                        _("  flat {k}/{n} — median {pct:.0f}% of scale").format(
                            k=len(frames), n=n,
                            pct=med / cam.full_scale * 100) + warn)
        except SVBError as e:
            self.log.emit(_("flat interrupted: {error}").format(error=e))
        if len(frames) < 3:
            self.log.emit(_("not enough frames for a flat"))
            return

        master = np.median(np.stack(frames), axis=0).astype(np.float32)
        if self._dark is not None and self._dark.shape == master.shape:
            master = np.maximum(master - self._dark, 1.0)
            self.log.emit(_("  session dark subtracted from the flat"))
        med = float(np.median(master))
        if med <= 0:
            self.log.emit(_("invalid flat (median <= 0)"))
            return
        if med > cam.full_scale * 0.9:
            self.log.emit(_("warning: flat close to saturation — shorten the "
                            "exposure"))
        elif med < cam.full_scale * 0.15:
            self.log.emit(_("warning: dark flat, it carries noise into the data "
                            "— add light or lengthen the exposure"))

        folder = Path(self.cfg.flat_root)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"flat_g{gain}_bin{g.bin}.fits"
        from astropy.io import fits
        hdr = fits.Header()
        hdr["IMAGETYP"] = "FLAT"
        hdr["EXPTIME"] = exp
        hdr["GAIN"] = gain
        hdr["XBINNING"] = g.bin
        hdr["NCOMBINE"] = len(frames)
        hdr["BAYERPAT"] = cam.bayer.fits_name
        hdr["FULLSCAL"] = cam.full_scale
        fits.PrimaryHDU(master, hdr).writeto(path, overwrite=True)

        norm = master / med
        self.log.emit(_("master flat -> {path}  (median {pct:.0f}% of scale, "
                        "corner at {corner:.0f}% of the centre)").format(
                            path=path, pct=med / cam.full_scale * 100,
                            corner=norm.min() * 100))

        self.cfg.flat_path = str(path)
        self._load_flat()
        self.flat_saved.emit(str(path))

    def _new_stacker(self) -> None:
        h, w = self.info["height"], self.info["width"]
        c = self.cfg
        self.stacker = LiveStacker((h, w), channels=3, sigma_clip=c.sigma_clip,
                                   saturation=debayer.LUM_SUM,
                                   ref_refresh=c.ref_refresh,
                                   min_ref_stars=c.min_ref_stars,
                                   quality_weighting=c.quality_weighting)
        self.stacker.set_strictness(c.strictness)
        self.platform.clear()

    def _apply_pending(self) -> None:
        with self._lock:
            pending, flags = self._pending, self._flags
            self._pending, self._flags = {}, set()

        # Cooling goes through the controller, not straight to the camera.
        tec = {k: pending.pop(k) for k in list(pending)
               if k in ("target_temp", "cooler")}
        if "target_temp" in tec:
            self.cfg.target_temp = float(tec["target_temp"])
        if "cooler" in tec:
            self._request_cooling(bool(tec["cooler"]))
        elif "target_temp" in tec and self.cool.target is not None:
            self._request_cooling(True)

        proc = {k: pending.pop(k) for k in list(pending)
                if k in ("dark_path", "flat_path", "sigma_clip", "strictness",
                         "dark_frames", "flat_frames", "record_every",
                         "target_name", "mode")}
        if pending:
            for m in self.src.apply(**pending):
                self.log.emit(m)
            if "bin" in pending:
                self.info.update(width=self.src.geometry.width,
                                 height=self.src.geometry.height,
                                 bin=self.src.geometry.bin)
                self._load_dark()
                self._load_flat()
                self._new_stacker()
                self.log.emit(_("geometry changed: stack reset"))

        for k, v in proc.items():
            if k == "dark_path":
                self.cfg.dark_path = v or None
                self._load_dark()
            elif k == "flat_path":
                self.cfg.flat_path = v or None
                self._load_flat()
            elif k == "dark_frames":
                self._n_dark = max(int(v), 3)
            elif k == "flat_frames":
                self._n_flat = max(int(v), 3)
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
            elif k == "target_name":
                self.cfg.target_name = str(v)
                if self.recorder:
                    d = self.recorder.rename_target(str(v))
                    if d:
                        self.log.emit(_("recording to {path}").format(path=d))
            elif k == "mode":
                self._set_mode(str(v))

        if "new_segment" in flags and self.stacker:
            self.stacker.new_segment()
            self.log.emit(_("new segment: thresholds relaxed, the reference will "
                            "be re-extracted from the stack"))
        if "reset" in flags:
            self._new_stacker()
            self.focus_meter = FocusMeter()
            self.log.emit(_("stack reset"))
        if "integrate_on" in flags:
            self.set_integrating(True)
        if "integrate_off" in flags:
            self.set_integrating(False)
        if "capture_dark" in flags:
            self._capture_dark(self._n_dark)
        if "capture_flat" in flags:
            self._capture_flat(self._n_flat)
        if "reset_focus_best" in flags:
            self.focus_meter.reset_best()
            self.log.emit(_("session best focus reset"))

    @property
    def can_integrate(self) -> bool:
        return self.cfg.mode in ("stack", "config")

    @property
    def stacking(self) -> bool:
        """Stacks only if the user asked AND the mode allows it."""
        return self.integrating and self.can_integrate

    def set_integrating(self, on: bool) -> None:
        before = self.stacking
        self.integrating = bool(on)
        if self.stacking and not before:
            if not self.platform.started:
                self.platform.clear()
                self.platform.started = True
                self.log.emit(_("integrating: stacking, recording and measuring "
                                "the residual rotation"))
            else:
                self.log.emit(_("integration resumed on the existing accumulator"))
        elif before and not self.stacking:
            self.log.emit(_("integration stopped — capture continues, stack "
                            "preserved"))

    def _set_mode(self, mode: str) -> None:
        before = self.stacking
        self.cfg.mode = mode
        now = self.stacking
        if before and not now:
            self.log.emit(_("{mode}: integration on hold, stack preserved"
                            ).format(mode=mode))
        elif now and not before:
            self.log.emit(_("integration resumed"))

    def _process(self, raw: np.ndarray, meta: FrameMeta) -> None:
        st = self.stacker
        t0 = time.perf_counter()

        f = calibrate(raw, meta, dark=self._dark, flat=self._flat, hot=self._hot)
        cfa16 = (f * 65535).astype(np.uint16)
        rgb = debayer.to_rgb(cfa16, meta.bayer,
                             quality="linear").astype(np.float32) / 65535.0
        lum = debayer.cfa_to_luminance(f)
        self._last_lum = lum

        if not self.stacking:
            # Light path: detect stars only, for HFR and the count. Nothing
            # touches the accumulator, the disk or the platform monitor.
            sf = detect(lum, scale=2.0, max_stars=60, central=0.70,
                        saturation=debayer.LUM_SUM)
            sample = (self.focus_meter.add(sf, meta.temperature)
                      if len(sf) >= 3 else None)
            stats = self._stats_snapshot(None, None, meta, t0)
            stats.update(accepted=None, reason="", n_stars=len(sf),
                         fwhm=sf.median_fwhm, matched=0, rms=float("nan"),
                         rotation=0.0, frame_ms=0.0)
            self.frame.emit(rgb, st.result() if st.started else None, cfa16, stats)
            self._emit_focus(sample, sf, lum, outcome=None)
            return

        o = st.add(rgb, lum, exposure=meta.exposure, lum_scale=2.0)
        al = o.alignment

        if al is not None and al.ok and o.accepted:
            self.platform.add(al.rotation_deg, *st.drift(), o.fwhm)

        sample = (self.focus_meter.add(_StarProxy(o), meta.temperature)
                  if o.n_stars >= 3 else None)

        if self.recorder:
            try:
                self.recorder.write_sub(raw, meta)
            except Exception as e:
                self.log.emit(_("error writing sub: {error}").format(error=e))

        stats = self._stats_snapshot(o, al, meta, t0)
        self.frame.emit(rgb, st.result() if st.started else None, cfa16, stats)
        self._emit_focus(sample, None, lum, outcome=o)

    def _emit_focus(self, sample, starfield, lum, outcome) -> None:
        fs = self.focus_meter
        xy = self._loupe_xy
        if xy is None:
            if outcome is not None:
                xy = outcome.brightest
            elif starfield is not None and len(starfield):
                i = int(np.argmax(starfield.flux))
                xy = (float(starfield.xy[i, 0]), float(starfield.xy[i, 1]))
        crop = (loupe(lum, (xy[0] / 2.0, xy[1] / 2.0), half=28, zoom=5)
                if xy else None)
        self.focus.emit(crop, {
            "hfr": sample.hfr if sample else float("nan"),
            "fwhm": sample.fwhm if sample else float("nan"),
            "n_stars": (len(starfield) if starfield is not None
                        else (outcome.n_stars if outcome else 0)),
            "trend": fs.trend(), "ratio": fs.ratio_to_best(),
            "best": fs.best.hfr if fs.best else float("nan"),
            "verdict": fs.verdict(), "series": fs.series(),
        })

    def _stats_snapshot(self, o=None, al=None, meta=None, t0=None) -> dict:
        st = self.stacker
        shape = (self.info.get("height", 1), self.info.get("width", 1))
        s = {
            "mode": self.cfg.mode,
            "stacking": self.stacking,
            "integrating": self.integrating,
            "can_integrate": self.can_integrate,
            "n_stacked": st.n_stacked if st else 0,
            "n_rejected": st.n_rejected if st else 0,
            "rejections": dict(st.rejections) if st else {},
            "integration": st.total_exposure if st else 0.0,
            "integration_eff": st.weighted_exposure if st else 0.0,
            "overlap": st.overlap_fraction() if st else 0.0,
            "drift": st.drift() if st else (0.0, 0.0),
            "platform": self.platform.report(shape),
            "platform_advice": self.platform.advice(shape),
            "recording": self.recorder.disk_report() if self.recorder else "",
        }
        if o is not None:
            s.update(accepted=o.accepted, reason=o.reason, n_stars=o.n_stars,
                     fwhm=o.fwhm, hfr=o.hfr, elong=o.elong, halo=o.halo,
                     limits=dict(o.limits), frame_ms=o.elapsed_ms,
                     proc_ms=(time.perf_counter() - t0) * 1e3 if t0 else 0.0,
                     matched=al.n_matched if al else 0,
                     rms=al.rms if al else float("nan"),
                     rotation=al.rotation_deg if al else 0.0,
                     weight=o.weight, score=o.score)
        if meta is not None:
            s.update(temp=meta.temperature, cooler_power=meta.cooler_power,
                     exposure=meta.exposure, gain=meta.gain,
                     frame_index=meta.index, origin=meta.origin,
                     bayer=meta.bayer)
        if isinstance(self.src, ReplaySource):
            i, n = self.src.progress
            s["replay_progress"] = (i, n)
        return s


class _StarProxy:
    """Adapts the stacker's outcome to the interface FocusMeter expects.

    The stacker has already detected the stars; re-detecting them just to
    measure focus would double the per-frame processing time.
    """

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
