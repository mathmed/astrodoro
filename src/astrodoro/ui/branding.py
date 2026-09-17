from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap

from . import icons

PLATE = Path(__file__).resolve().parent / "assets" / "icon.png"

PLATE_SIZES = (64, 128, 256, 512)
GLYPH_SIZES = (16, 24, 32, 48)


def app_icon(colour: str = "#dcdfe4") -> QIcon:
    icon = QIcon()
    for size in GLYPH_SIZES:
        icon.addPixmap(icons.pixmap("mark", colour, size))
    if PLATE.exists():
        plate = QPixmap(str(PLATE))
        if not plate.isNull():
            for size in PLATE_SIZES:
                icon.addPixmap(
                    plate.scaled(
                        QSize(size, size),
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
    return icon
