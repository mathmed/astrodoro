from __future__ import annotations

import ctypes as C
import os
from collections.abc import Callable
from enum import IntEnum
from pathlib import Path

from ..base import CameraError

_LIBNAME = "libSVBCameraSDK.dylib"
_VENDOR = Path(__file__).resolve().parents[4] / "vendor" / "lib"
_ASTRODMX = Path("/Applications/AstroDMx Capture.app/Contents/Resources/lib")


def _load() -> C.CDLL:
    tried: list[str] = []
    candidates: list[Path] = []
    if os.environ.get("SVB_SDK_PATH"):
        candidates.append(Path(os.environ["SVB_SDK_PATH"]))
    candidates += [_VENDOR / _LIBNAME, _ASTRODMX / _LIBNAME]
    for p in candidates:
        tried.append(str(p))
        if p.exists():
            try:
                return C.CDLL(str(p))
            except OSError as e:
                tried[-1] += f"  ({e})"
    raise OSError(
        f"{_LIBNAME} not found or not loadable. Run scripts/setup_sdk.sh.\n"
        + "\n".join("  tried: " + t for t in tried)
    )


_lib: C.CDLL | None = None


def library() -> C.CDLL:
    global _lib
    if _lib is None:
        _lib = _load()
    return _lib


class Control(IntEnum):
    GAIN = 0
    EXPOSURE = 1
    GAMMA = 2
    GAMMA_CONTRAST = 3
    WB_R = 4
    WB_G = 5
    WB_B = 6
    FLIP = 7
    FRAME_SPEED_MODE = 8
    CONTRAST = 9
    SHARPNESS = 10
    SATURATION = 11
    AUTO_TARGET_BRIGHTNESS = 12
    BLACK_LEVEL = 13
    COOLER_ENABLE = 14
    TARGET_TEMPERATURE = 15
    CURRENT_TEMPERATURE = 16
    COOLER_POWER = 17
    BAD_PIXEL_CORRECTION_ENABLE = 18
    BAD_PIXEL_CORRECTION_THRESHOLD = 19


class CameraMode(IntEnum):
    NORMAL = 0
    TRIG_SOFT = 1
    TRIG_RISE_EDGE = 2
    TRIG_FALL_EDGE = 3
    TRIG_DOUBLE_EDGE = 4
    TRIG_HIGH_LEVEL = 5
    TRIG_LOW_LEVEL = 6
    END = -1


ERRORS = {
    0: "SUCCESS",
    1: "INVALID_INDEX",
    2: "INVALID_ID",
    3: "INVALID_CONTROL_TYPE",
    4: "CAMERA_CLOSED",
    5: "CAMERA_REMOVED",
    6: "INVALID_PATH",
    7: "INVALID_FILEFORMAT",
    8: "INVALID_SIZE",
    9: "INVALID_IMGTYPE",
    10: "OUTOF_BOUNDARY",
    11: "TIMEOUT",
    12: "INVALID_SEQUENCE",
    13: "BUFFER_TOO_SMALL",
    14: "VIDEO_MODE_ACTIVE",
    15: "EXPOSURE_IN_PROGRESS",
    16: "GENERAL_ERROR",
    17: "INVALID_MODE",
    18: "INVALID_DIRECTION",
    19: "UNKNOW_SENSOR_TYPE",
}

TIMEOUT = 11


class SVBError(CameraError):
    def __init__(self, op: str, code: int):
        self.op, self.code = op, code
        super().__init__(f"{op} failed: {ERRORS.get(code, '?')} ({code})")


def check(op: str, code: int) -> None:
    if code != 0:
        raise SVBError(op, code)


class CameraInfo(C.Structure):
    _fields_ = [
        ("FriendlyName", C.c_char * 32),
        ("CameraSN", C.c_char * 32),
        ("PortType", C.c_char * 32),
        ("DeviceID", C.c_uint),
        ("CameraID", C.c_int),
    ]


class CameraProperty(C.Structure):
    _fields_ = [
        ("MaxHeight", C.c_long),
        ("MaxWidth", C.c_long),
        ("IsColorCam", C.c_int),
        ("BayerPattern", C.c_int),
        ("SupportedBins", C.c_int * 16),
        ("SupportedVideoFormat", C.c_int * 8),
        ("MaxBitDepth", C.c_int),
        ("IsTriggerCam", C.c_int),
    ]


class CameraPropertyEx(C.Structure):
    _fields_ = [
        ("bSupportPulseGuide", C.c_int),
        ("bSupportControlTemp", C.c_int),
        ("Unused", C.c_int * 64),
    ]


class ControlCaps(C.Structure):
    _fields_ = [
        ("Name", C.c_char * 64),
        ("Description", C.c_char * 128),
        ("MaxValue", C.c_long),
        ("MinValue", C.c_long),
        ("DefaultValue", C.c_long),
        ("IsAutoSupported", C.c_int),
        ("IsWritable", C.c_int),
        ("ControlType", C.c_int),
        ("Unused", C.c_char * 32),
    ]


i, li, p = C.c_int, C.c_long, C.POINTER

_PROTOS: dict[str, tuple] = {
    "GetNumOfConnectedCameras": ("SVBGetNumOfConnectedCameras", i),
    "GetSDKVersion": ("SVBGetSDKVersion", C.c_char_p),
    "GetCameraInfo": ("SVBGetCameraInfo", i, p(CameraInfo), i),
    "GetCameraProperty": ("SVBGetCameraProperty", i, i, p(CameraProperty)),
    "GetCameraPropertyEx": ("SVBGetCameraPropertyEx", i, i, p(CameraPropertyEx)),
    "OpenCamera": ("SVBOpenCamera", i, i),
    "CloseCamera": ("SVBCloseCamera", i, i),
    "GetNumOfControls": ("SVBGetNumOfControls", i, i, p(i)),
    "GetControlCaps": ("SVBGetControlCaps", i, i, i, p(ControlCaps)),
    "GetControlValue": ("SVBGetControlValue", i, i, i, p(li), p(i)),
    "SetControlValue": ("SVBSetControlValue", i, i, i, li, i),
    "SetOutputImageType": ("SVBSetOutputImageType", i, i, i),
    "GetOutputImageType": ("SVBGetOutputImageType", i, i, p(i)),
    "SetROIFormat": ("SVBSetROIFormat", i, i, i, i, i, i, i),
    "GetROIFormat": ("SVBGetROIFormat", i, i, p(i), p(i), p(i), p(i), p(i)),
    "StartVideoCapture": ("SVBStartVideoCapture", i, i),
    "StopVideoCapture": ("SVBStopVideoCapture", i, i),
    "GetVideoData": ("SVBGetVideoData", i, i, C.POINTER(C.c_ubyte), li, i),
    "GetDroppedFrames": ("SVBGetDroppedFrames", i, i, p(i)),
    "GetSensorPixelSize": ("SVBGetSensorPixelSize", i, i, p(C.c_float)),
    "GetCameraFirmwareVersion": ("SVBGetCameraFirmwareVersion", i, i, C.c_char_p),
    "GetSerialNumber": ("SVBGetSerialNumber", i, i, C.c_void_p),
    "SetCameraMode": ("SVBSetCameraMode", i, i, i),
    "GetCameraMode": ("SVBGetCameraMode", i, i, p(i)),
    "WhiteBalanceOnce": ("SVBWhiteBalanceOnce", i, i),
    "RestoreDefaultParam": ("SVBRestoreDefaultParam", i, i),
    "SetAutoSaveParam": ("SVBSetAutoSaveParam", i, i, i),
}

_resolved: dict[str, Callable] = {}


def _fn(name: str):
    fn = _resolved.get(name)
    if fn is None:
        symbol, restype, *argtypes = _PROTOS[name]
        fn = getattr(library(), symbol)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        _resolved[name] = fn
    return fn


def __getattr__(name: str):
    if name == "lib":
        return library()
    if name in _PROTOS:
        return _fn(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def sdk_version() -> str:
    return _fn("GetSDKVersion")().decode()
