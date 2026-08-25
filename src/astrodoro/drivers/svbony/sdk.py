"""Low-level ctypes binding for the SVBony Camera SDK.

Maps SVBCameraSDK.h (API 3.0.0 / lib v1.13.4) almost one to one. Every
convenience lives in `camera.py`; this is only the C surface.

In C mode the header does `#define SVB_CONTROL_TYPE int` (and likewise for
SVB_BOOL, SVB_ERROR_CODE, SVB_IMG_TYPE, SVB_BAYER_PATTERN), so all of those
parameters travel as c_int.
"""
from __future__ import annotations

import ctypes as C
import os
from enum import IntEnum
from pathlib import Path

_LIBNAME = "libSVBCameraSDK.dylib"
# `vendor/lib` at the root of a source checkout, where scripts/setup_sdk.sh
# puts the prepared dylib.
_VENDOR = Path(__file__).resolve().parents[4] / "vendor" / "lib"
# Fallback: the arm64 dylib shipped with AstroDMx, which is the same SDK.
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


lib = _load()


# ---------------------------------------------------------------------- enums
class Control(IntEnum):
    GAIN = 0
    EXPOSURE = 1                      # microseconds
    GAMMA = 2
    GAMMA_CONTRAST = 3
    WB_R = 4
    WB_G = 5
    WB_B = 6
    FLIP = 7
    FRAME_SPEED_MODE = 8              # 0 low, 1 medium, 2 high
    CONTRAST = 9
    SHARPNESS = 10
    SATURATION = 11
    AUTO_TARGET_BRIGHTNESS = 12
    BLACK_LEVEL = 13                  # offset
    COOLER_ENABLE = 14                # 0/1
    TARGET_TEMPERATURE = 15           # units of 0.1 C
    CURRENT_TEMPERATURE = 16          # units of 0.1 C
    COOLER_POWER = 17                 # 0-100
    BAD_PIXEL_CORRECTION_ENABLE = 18
    BAD_PIXEL_CORRECTION_THRESHOLD = 19


class ImgType(IntEnum):
    RAW8 = 0
    RAW10 = 1
    RAW12 = 2
    RAW14 = 3
    RAW16 = 4
    Y8 = 5
    Y10 = 6
    Y12 = 7
    Y14 = 8
    Y16 = 9
    RGB24 = 10
    RGB32 = 11

    @property
    def bytes_per_pixel(self) -> int:
        if self in (ImgType.RAW8, ImgType.Y8):
            return 1
        if self is ImgType.RGB24:
            return 3
        if self is ImgType.RGB32:
            return 4
        return 2  # RAW10/12/14/16 and Y10..Y16 arrive packed into 16 bits


class Bayer(IntEnum):
    RG = 0
    BG = 1
    GR = 2
    GB = 3

    @property
    def fits_name(self) -> str:
        """FITS BAYERPAT value, as Siril/PixInsight/ASTAP expect it."""
        return {Bayer.RG: "RGGB", Bayer.BG: "BGGR",
                Bayer.GR: "GRBG", Bayer.GB: "GBRG"}[self]

    @property
    def cv_code_rgb(self) -> str:
        """Name of the matching OpenCV demosaic code.

        OpenCV's naming is shifted with respect to the sensor pattern:
        SVB_BAYER_RG (RGGB) corresponds to COLOR_BayerBG2RGB.
        """
        return {
            Bayer.RG: "COLOR_BayerBG2RGB",
            Bayer.BG: "COLOR_BayerRG2RGB",
            Bayer.GR: "COLOR_BayerGB2RGB",
            Bayer.GB: "COLOR_BayerGR2RGB",
        }[self]


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


class SVBError(RuntimeError):
    def __init__(self, op: str, code: int):
        self.op, self.code = op, code
        super().__init__(f"{op} failed: {ERRORS.get(code, '?')} ({code})")


def check(op: str, code: int) -> None:
    if code != 0:
        raise SVBError(op, code)


# -------------------------------------------------------------------- structs
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
        ("SupportedBins", C.c_int * 16),      # zero terminates the list
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


# ---------------------------------------------------------------- prototypes
def _proto(name, restype, *argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


i, li, p = C.c_int, C.c_long, C.POINTER

GetNumOfConnectedCameras = _proto("SVBGetNumOfConnectedCameras", i)
GetSDKVersion = _proto("SVBGetSDKVersion", C.c_char_p)
GetCameraInfo = _proto("SVBGetCameraInfo", i, p(CameraInfo), i)
GetCameraProperty = _proto("SVBGetCameraProperty", i, i, p(CameraProperty))
GetCameraPropertyEx = _proto("SVBGetCameraPropertyEx", i, i, p(CameraPropertyEx))
OpenCamera = _proto("SVBOpenCamera", i, i)
CloseCamera = _proto("SVBCloseCamera", i, i)
GetNumOfControls = _proto("SVBGetNumOfControls", i, i, p(i))
GetControlCaps = _proto("SVBGetControlCaps", i, i, i, p(ControlCaps))
GetControlValue = _proto("SVBGetControlValue", i, i, i, p(li), p(i))
SetControlValue = _proto("SVBSetControlValue", i, i, i, li, i)
SetOutputImageType = _proto("SVBSetOutputImageType", i, i, i)
GetOutputImageType = _proto("SVBGetOutputImageType", i, i, p(i))
SetROIFormat = _proto("SVBSetROIFormat", i, i, i, i, i, i, i)
GetROIFormat = _proto("SVBGetROIFormat", i, i, p(i), p(i), p(i), p(i), p(i))
StartVideoCapture = _proto("SVBStartVideoCapture", i, i)
StopVideoCapture = _proto("SVBStopVideoCapture", i, i)
GetVideoData = _proto("SVBGetVideoData", i, i, C.POINTER(C.c_ubyte), li, i)
GetDroppedFrames = _proto("SVBGetDroppedFrames", i, i, p(i))
GetSensorPixelSize = _proto("SVBGetSensorPixelSize", i, i, p(C.c_float))
GetCameraFirmwareVersion = _proto("SVBGetCameraFirmwareVersion", i, i, C.c_char_p)
GetSerialNumber = _proto("SVBGetSerialNumber", i, i, C.c_void_p)
SetCameraMode = _proto("SVBSetCameraMode", i, i, i)
GetCameraMode = _proto("SVBGetCameraMode", i, i, p(i))
WhiteBalanceOnce = _proto("SVBWhiteBalanceOnce", i, i)
RestoreDefaultParam = _proto("SVBRestoreDefaultParam", i, i)
SetAutoSaveParam = _proto("SVBSetAutoSaveParam", i, i, i)


def sdk_version() -> str:
    return GetSDKVersion().decode()
