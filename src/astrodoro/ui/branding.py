"""The application icon.

Two sources, because no single drawing survives the whole size range. The
engraved plate carries the detail that reads in the Dock; below about 48 px its
hatching collapses into grey, so the small sizes come from a simplified stroke
glyph instead. A `QIcon` holds both and Qt picks whichever matches the size it
is asked for.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap

from . import icons

#: The engraved plate, package data so it survives being installed.
PLATE = Path(__file__).resolve().parent / "assets" / "icon.png"

#: Where the plate stops being legible. Measured by rendering it down: at 32 px
#: the tube has lost its shape and the stars are loose red squares.
GLYPH_MAX = 48

PLATE_SIZES = (64, 128, 256, 512)
GLYPH_SIZES = (16, 24, 32, 48)


def app_icon(colour: str = "#dcdfe4") -> QIcon:
    """The window and Dock icon, detailed above `GLYPH_MAX` and simplified below."""
    icon = QIcon()
    for size in GLYPH_SIZES:
        icon.addPixmap(icons.pixmap("mark", colour, size))
    if PLATE.exists():
        plate = QPixmap(str(PLATE))
        if not plate.isNull():
            for size in PLATE_SIZES:
                icon.addPixmap(plate.scaled(
                    QSize(size, size), Qt.KeepAspectRatio,
                    Qt.SmoothTransformation))
    return icon
