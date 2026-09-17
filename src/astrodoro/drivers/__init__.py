from __future__ import annotations

from .base import (
    Bayer,
    CameraDevice,
    CameraError,
    CameraID,
    Geometry,
    ImgType,
)

_DRIVERS = ("svbony",)


def list_cameras() -> list[CameraID]:
    from importlib import import_module

    out: list[CameraID] = []
    for name in _DRIVERS:
        try:
            out += import_module(f".{name}", __package__).list_cameras()
        except (OSError, ImportError):
            continue
    return out


def camera(index: int = 0) -> CameraDevice:
    from ..i18n import gettext as _
    from .svbony import Camera

    cams = list_cameras()
    if not cams:
        raise CameraError(_("no camera found (is another capture program holding it?)"))
    return Camera(cams[min(index, len(cams) - 1)])


def open_camera(index: int = 0) -> CameraDevice:
    return camera(index).open()


__all__ = [
    "Bayer",
    "CameraDevice",
    "CameraError",
    "CameraID",
    "Geometry",
    "ImgType",
    "camera",
    "list_cameras",
    "open_camera",
]
