from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol, runtime_checkable


class Bayer(IntEnum):
    RG = 0
    BG = 1
    GR = 2
    GB = 3

    @property
    def fits_name(self) -> str:
        return {Bayer.RG: "RGGB", Bayer.BG: "BGGR", Bayer.GR: "GRBG", Bayer.GB: "GBRG"}[
            self
        ]


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
        return 2


class CameraError(RuntimeError):
    pass


@dataclass(frozen=True)
class CameraID:
    index: int
    camera_id: int
    name: str
    serial: str
    port: str

    def __str__(self) -> str:
        return f"[{self.index}] {self.name}  sn={self.serial or '?'}  ({self.port})"


@dataclass
class Geometry:
    x: int
    y: int
    width: int
    height: int
    bin: int


@runtime_checkable
class CameraDevice(Protocol):
    id: CameraID

    def open(self) -> CameraDevice: ...

    def close(self) -> None: ...

    def __enter__(self) -> CameraDevice: ...

    def __exit__(self, *_exc) -> None: ...

    @property
    def bayer(self) -> Bayer: ...

    @property
    def full_scale(self) -> int: ...

    @property
    def pixel_size_um(self) -> float: ...

    @property
    def firmware(self) -> str: ...

    @property
    def is_color(self) -> bool: ...

    @property
    def bit_depth(self) -> int: ...

    @property
    def min_exposure(self) -> float: ...

    @property
    def gain_range(self) -> tuple[int, int]: ...

    @property
    def dropped_frames(self) -> int: ...

    def describe(self) -> str: ...

    # What `open()` had to reassert to get linear data out of the camera, as
    # "NAME old->new" strings. Empty when it was already linear.
    forced_linear: list[str]

    @property
    def geometry(self) -> Geometry: ...

    image_type: ImgType
    gain: int
    exposure: float
    offset: int

    def set_roi(
        self,
        bin: int = 1,
        x: int = 0,
        y: int = 0,
        width: int | None = None,
        height: int | None = None,
    ) -> Geometry: ...

    def start_video(self) -> None: ...

    def stop_video(self) -> None: ...

    def read_frame(self, timeout: float | None = None): ...

    @property
    def supports_cooler(self) -> bool: ...

    @property
    def temperature(self) -> float: ...

    @property
    def cooler_power(self) -> int: ...

    cooler: bool
    target_temperature: float
