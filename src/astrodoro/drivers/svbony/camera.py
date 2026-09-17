from __future__ import annotations

import ctypes as C
from dataclasses import dataclass

import numpy as np

from ...i18n import gettext as _
from ..base import Bayer, CameraID, Geometry, ImgType
from . import sdk
from .sdk import Control, SVBError, check


def list_cameras() -> list[CameraID]:
    out = []
    for idx in range(sdk.GetNumOfConnectedCameras()):
        info = sdk.CameraInfo()
        check("GetCameraInfo", sdk.GetCameraInfo(C.byref(info), idx))
        out.append(
            CameraID(
                index=idx,
                camera_id=info.CameraID,
                name=info.FriendlyName.decode(errors="replace"),
                serial=info.CameraSN.decode(errors="replace"),
                port=info.PortType.decode(errors="replace"),
            )
        )
    return out


@dataclass
class ControlInfo:
    control: int
    name: str
    description: str
    min: int
    max: int
    default: int
    writable: bool
    auto_supported: bool

    @property
    def label(self) -> str:
        try:
            return Control(self.control).name
        except ValueError:
            return f"UNKNOWN_{self.control}"


class Camera:
    def __init__(self, cam: CameraID):
        self.id = cam
        self._cid = cam.camera_id
        self._open = False
        self._buf: C.Array | None = None
        self._buf_geom: tuple | None = None
        self._streaming = False
        self.props: sdk.CameraProperty | None = None
        self.props_ex: sdk.CameraPropertyEx | None = None
        self.controls: dict[int, ControlInfo] = {}
        self.forced_linear: list[str] = []

    def open(self) -> Camera:
        sdk.GetNumOfConnectedCameras()
        info = sdk.CameraInfo()
        sdk.GetCameraInfo(C.byref(info), self.id.index)

        check("OpenCamera", sdk.OpenCamera(self._cid))
        self._open = True
        self.props = sdk.CameraProperty()
        check(
            "GetCameraProperty", sdk.GetCameraProperty(self._cid, C.byref(self.props))
        )
        self.props_ex = sdk.CameraPropertyEx()
        if sdk.GetCameraPropertyEx(self._cid, C.byref(self.props_ex)) != 0:
            self.props_ex = None
        try:
            sdk.SetAutoSaveParam(self._cid, 0)
        except Exception:
            pass

        self._load_controls()
        self._leave_auto_mode()
        self._force_linear()
        return self

    def close(self) -> None:
        if self._streaming:
            try:
                self.stop_video()
            except SVBError:
                pass
        if self._open:
            sdk.CloseCamera(self._cid)
            self._open = False

    def __enter__(self) -> Camera:
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    def _load_controls(self) -> None:
        n = C.c_int()
        check("GetNumOfControls", sdk.GetNumOfControls(self._cid, C.byref(n)))
        self.controls.clear()
        for k in range(n.value):
            caps = sdk.ControlCaps()
            check("GetControlCaps", sdk.GetControlCaps(self._cid, k, C.byref(caps)))
            self.controls[caps.ControlType] = ControlInfo(
                control=caps.ControlType,
                name=caps.Name.decode(errors="replace"),
                description=caps.Description.decode(errors="replace"),
                min=caps.MinValue,
                max=caps.MaxValue,
                default=caps.DefaultValue,
                writable=bool(caps.IsWritable),
                auto_supported=bool(caps.IsAutoSupported),
            )

    def _leave_auto_mode(self) -> None:
        for ctrl in (Control.EXPOSURE, Control.GAIN):
            info = self.controls.get(int(ctrl))
            if info is None or not info.writable:
                continue
            try:
                val, auto = self.get(ctrl)
            except SVBError:
                continue
            if auto:
                try:
                    self.set(ctrl, val, auto=False)
                except SVBError:
                    pass

    def _force_linear(self) -> None:
        neutral = {
            Control.WB_R: 128,
            Control.WB_G: 128,
            Control.WB_B: 128,
            Control.GAMMA: 100,
            Control.CONTRAST: 50,
            Control.BAD_PIXEL_CORRECTION_ENABLE: 0,
        }
        self.forced_linear = []
        for ctrl, want in neutral.items():
            info = self.controls.get(int(ctrl))
            if info is None or not info.writable:
                continue
            try:
                cur, _auto = self.get(ctrl)
            except SVBError:
                continue
            if cur != want:
                try:
                    self.set(ctrl, want)
                    self.forced_linear.append(f"{Control(ctrl).name} {cur}->{want}")
                except SVBError:
                    pass

    @property
    def _props(self) -> sdk.CameraProperty:
        """The camera's properties, which only exist once it is open."""
        if self.props is None:
            raise SVBError("GetCameraProperty", 4)  # CAMERA_CLOSED
        return self.props

    @property
    def sensor_size(self) -> tuple[int, int]:
        return int(self._props.MaxWidth), int(self._props.MaxHeight)

    @property
    def is_color(self) -> bool:
        return bool(self._props.IsColorCam)

    @property
    def bayer(self) -> Bayer:
        return Bayer(self._props.BayerPattern)

    @property
    def supported_bins(self) -> list[int]:
        return [b for b in self._props.SupportedBins if b != 0]

    @property
    def supported_formats(self) -> list[ImgType]:
        out = []
        for v in self._props.SupportedVideoFormat:
            if v < 0:
                break
            try:
                out.append(ImgType(v))
            except ValueError:
                break
        return out

    @property
    def bit_depth(self) -> int:
        return int(self._props.MaxBitDepth)

    @property
    def full_scale(self) -> int:
        t = self.image_type
        if t.bytes_per_pixel == 1:
            return 255
        return ((1 << self.bit_depth) - 1) << (16 - self.bit_depth)

    @property
    def pixel_size_um(self) -> float:
        v = C.c_float()
        check("GetSensorPixelSize", sdk.GetSensorPixelSize(self._cid, C.byref(v)))
        return float(v.value)

    @property
    def firmware(self) -> str:
        buf = C.create_string_buffer(128)
        if sdk.GetCameraFirmwareVersion(self._cid, buf) != 0:
            return "?"
        return buf.value.decode(errors="replace")

    @property
    def supports_cooler(self) -> bool:
        if self.props_ex is not None:
            return bool(self.props_ex.bSupportControlTemp)
        return Control.COOLER_ENABLE in self.controls

    def get(self, ctrl: int) -> tuple[int, bool]:
        val, auto = C.c_long(), C.c_int()
        check(
            "GetControlValue",
            sdk.GetControlValue(self._cid, int(ctrl), C.byref(val), C.byref(auto)),
        )
        return int(val.value), bool(auto.value)

    def set(self, ctrl: int, value: int, auto: bool = False) -> None:
        info = self.controls.get(int(ctrl))
        if info is not None:
            if not info.writable:
                raise ValueError(
                    _("{control} is not writable on this camera").format(
                        control=info.label
                    )
                )
            if not (info.min <= value <= info.max):
                raise ValueError(
                    _("{control}={value} is outside the range [{low}, {high}]").format(
                        control=info.label, value=value, low=info.min, high=info.max
                    )
                )
        check(
            "SetControlValue",
            sdk.SetControlValue(self._cid, int(ctrl), int(value), int(auto)),
        )

    @property
    def gain(self) -> int:
        return self.get(Control.GAIN)[0]

    @gain.setter
    def gain(self, v: int) -> None:
        self.set(Control.GAIN, v)

    @property
    def exposure(self) -> float:
        return self.get(Control.EXPOSURE)[0] / 1e6

    @exposure.setter
    def exposure(self, seconds: float) -> None:
        self.set(Control.EXPOSURE, int(round(seconds * 1e6)))

    @property
    def min_exposure(self) -> float:
        info = self.controls.get(int(Control.EXPOSURE))
        return (info.min / 1e6) if info is not None else 1e-4

    @property
    def offset(self) -> int:
        return self.get(Control.BLACK_LEVEL)[0]

    @offset.setter
    def offset(self, v: int) -> None:
        self.set(Control.BLACK_LEVEL, v)

    @property
    def temperature(self) -> float:
        return self.get(Control.CURRENT_TEMPERATURE)[0] / 10.0

    @property
    def cooler_power(self) -> int:
        return self.get(Control.COOLER_POWER)[0]

    @property
    def target_temperature(self) -> float:
        return self.get(Control.TARGET_TEMPERATURE)[0] / 10.0

    @target_temperature.setter
    def target_temperature(self, celsius: float) -> None:
        self.set(Control.TARGET_TEMPERATURE, int(round(celsius * 10)))

    @property
    def max_target_temperature(self) -> float:
        info = self.controls.get(int(Control.TARGET_TEMPERATURE))
        return (info.max / 10.0) if info is not None else 30.0

    @property
    def min_target_temperature(self) -> float:
        info = self.controls.get(int(Control.TARGET_TEMPERATURE))
        return (info.min / 10.0) if info is not None else -40.0

    @property
    def cooler(self) -> bool:
        return bool(self.get(Control.COOLER_ENABLE)[0])

    @cooler.setter
    def cooler(self, on: bool) -> None:
        self.set(Control.COOLER_ENABLE, 1 if on else 0)

    @property
    def bad_pixel_correction(self) -> bool:
        return bool(self.get(Control.BAD_PIXEL_CORRECTION_ENABLE)[0])

    @bad_pixel_correction.setter
    def bad_pixel_correction(self, on: bool) -> None:
        self.set(Control.BAD_PIXEL_CORRECTION_ENABLE, 1 if on else 0)

    @property
    def geometry(self) -> Geometry:
        x, y, w, h, b = (C.c_int() for _ in range(5))
        check(
            "GetROIFormat",
            sdk.GetROIFormat(self._cid, *(C.byref(v) for v in (x, y, w, h, b))),
        )
        return Geometry(x.value, y.value, w.value, h.value, b.value)

    @property
    def image_type(self) -> ImgType:
        v = C.c_int()
        check("GetOutputImageType", sdk.GetOutputImageType(self._cid, C.byref(v)))
        return ImgType(v.value)

    @image_type.setter
    def image_type(self, t: ImgType) -> None:
        check("SetOutputImageType", sdk.SetOutputImageType(self._cid, int(t)))
        self._buf = None

    def set_roi(
        self,
        bin: int = 1,
        x: int = 0,
        y: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> Geometry:
        if bin not in self.supported_bins:
            raise ValueError(
                _("bin {bin} is not supported; available: {available}").format(
                    bin=bin, available=self.supported_bins
                )
            )
        if bin > 2:
            import warnings

            warnings.warn(
                f"bin{bin} clips the highlights on this camera (the sum "
                f"saturates at 0xFFFF); use bin2 for lossless binning",
                RuntimeWarning,
                stacklevel=2,
            )
        mw, mh = self.sensor_size
        width = (mw // bin) if width is None else width
        height = (mh // bin) if height is None else height
        x -= x % 2
        y -= y % 2
        rc = sdk.SetROIFormat(self._cid, x, y, width, height, bin)
        if rc != 0:
            w8, h8 = width - width % 8, height - height % 8
            if sdk.SetROIFormat(self._cid, x, y, w8, h8, bin) == 0:
                width, height, rc = w8, h8, 0
        check("SetROIFormat", rc)
        self._buf = None
        return self.geometry

    def _ensure_buffer(self) -> tuple[C.Array, Geometry, ImgType]:
        g, t = self.geometry, self.image_type
        key = (g.width, g.height, int(t))
        if self._buf is None or self._buf_geom != key:
            self._buf = (C.c_ubyte * (g.width * g.height * t.bytes_per_pixel))()
            self._buf_geom = key
        return self._buf, g, t

    def start_video(self) -> None:
        check("StartVideoCapture", sdk.StartVideoCapture(self._cid))
        self._streaming = True

    def stop_video(self) -> None:
        check("StopVideoCapture", sdk.StopVideoCapture(self._cid))
        self._streaming = False

    def read_frame(self, timeout: float | None = None) -> np.ndarray:
        buf, g, t = self._ensure_buffer()
        if timeout is None:
            timeout = self.exposure * 2 + 1.0
        check(
            "GetVideoData",
            sdk.GetVideoData(self._cid, buf, len(buf), int(timeout * 1000)),
        )

        if t.bytes_per_pixel == 1:
            arr = np.frombuffer(buf, dtype=np.uint8, count=g.width * g.height)
            return arr.reshape(g.height, g.width).copy()
        if t is ImgType.RGB24:
            arr = np.frombuffer(buf, dtype=np.uint8, count=g.width * g.height * 3)
            return arr.reshape(g.height, g.width, 3).copy()
        arr = np.frombuffer(buf, dtype="<u2", count=g.width * g.height)
        return arr.reshape(g.height, g.width).copy()

    @property
    def gain_range(self) -> tuple[int, int]:
        info = self.controls.get(int(Control.GAIN))
        return (info.min, info.max) if info else (0, 0)

    @property
    def dropped_frames(self) -> int:
        v = C.c_int()
        check("GetDroppedFrames", sdk.GetDroppedFrames(self._cid, C.byref(v)))
        return v.value

    def describe(self) -> str:
        w, h = self.sensor_size
        lines = [
            f"{self.id.name}   sn={self.id.serial or '?'}   fw={self.firmware}",
            f"  sensor        {w} x {h}  @ {self.pixel_size_um:.2f} um   "
            f"{self.bit_depth} bits (usable 0..{(1 << self.bit_depth) - 1})",
            f"  colour        "
            f"{'yes, Bayer ' + self.bayer.name if self.is_color else 'mono'}",
            f"  bins          {self.supported_bins}",
            f"  formats       {[f.name for f in self.supported_formats]}",
            f"  cooler        {'yes' if self.supports_cooler else 'no'}",
            f"  current ROI   {self.geometry}   fmt={self.image_type.name}",
            "  controls:",
        ]
        for c in sorted(self.controls.values(), key=lambda c: c.control):
            try:
                val, auto = self.get(c.control)
                cur = f"{val}{' (auto)' if auto else ''}"
            except SVBError as e:
                cur = f"<{e.code}>"
            rw = "rw" if c.writable else "ro"
            lines.append(
                f"    {c.label:<32} {cur:>12}   [{c.min}..{c.max}] def={c.default} {rw}"
            )
        return "\n".join(lines)
