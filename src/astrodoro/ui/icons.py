"""Embedded vector icons, tinted with the theme colour.

Why not emoji: emoji are coloured bitmaps and cannot be tinted. In night mode
you would have blue and green dots glowing on screen, exactly what the red image
ramp exists to avoid. Every icon here is a monochrome stroke that takes the
theme colour, so in night mode it turns red along with everything else.

Each icon is a hand-written 24x24 SVG, rendered at 2x for sharpness on retina
displays and cached by (name, colour, size).
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Stroke icons.
STROKE = {
    # modes
    "frame":   'M4 9V5h4 M20 9V5h-4 M4 15v4h4 M20 15v4h-4'
               ' M12 9.5v-1.5 M12 16v-1.5 M9.5 12H8 M16 12h-1.5'
               ' M14.2 12a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0',
    "focus":   'M18.5 12a6.5 6.5 0 1 1-13 0 6.5 6.5 0 0 1 13 0'
               ' M12 2.5v2.6 M12 18.9v2.6 M2.5 12h2.6 M18.9 12h2.6',
    "stack":   'M12 3.2l8.2 4.3-8.2 4.3L3.8 7.5z M3.8 12.2l8.2 4.3 8.2-4.3'
               ' M3.8 16.6l8.2 4.3 8.2-4.3',
    "adjust":  'M4 7h16 M4 12h16 M4 17h16',
    # actions
    "refresh": 'M20.5 12a8.5 8.5 0 1 1-2.49-6.01 M20.5 3.4v4.2h-4.2',
    "rotate":  'M3.5 12a8.5 8.5 0 1 0 2.49-6.01 M3.5 3.4v4.2h4.2',
    "folder":  'M3.5 7.5h5.6l2 2h9.4v10H3.5z',
    "target":  'M12 4.2v3 M12 16.8v3 M4.2 12h3 M16.8 12h3'
               ' M19 12a7 7 0 1 1-14 0 7 7 0 0 1 14 0'
               ' M14.6 12a2.6 2.6 0 1 1-5.2 0 2.6 2.6 0 0 1 5.2 0',
    "arrow":   'M4.5 12h13.5 M13 6.8l5.2 5.2-5.2 5.2',
    "save":    'M12 4v10.5 M8 11l4 4 4-4 M4.5 19.5h15',
    "fit":     'M9.5 4.5H4.5v5 M14.5 4.5h5v5 M9.5 19.5H4.5v-5 M14.5 19.5h5v-5',
    "expand":  'M4.5 9.5v-5h5 M19.5 9.5v-5h-5 M4.5 14.5v5h5 M19.5 14.5v5h-5'
               ' M10 10L5.5 5.5 M14 10l4.5-4.5 M10 14l-4.5 4.5 M14 14l4.5 4.5',
    "moon":    'M20 14.2A8.2 8.2 0 1 1 10.4 3.4 6.6 6.6 0 0 0 20 14.2z',
    "list":    'M4.5 7h15 M4.5 12h15 M4.5 17h9',
    "trash":   'M4.5 7h15 M9.5 7V4.8h5V7 M6.5 7l1 12.2h9L17.5 7',
    "segment": 'M3.5 17.5h3.5 M3.5 17.5c0-7.4 17-7.4 17 0 M20.5 12.6v5h-5',
    "speaker": 'M4.5 9.6h3l4-3.6v12l-4-3.6h-3z M15.3 9.4a3.7 3.7 0 0 1 0 5.2'
               ' M17.9 7a7.2 7.2 0 0 1 0 10',
    "camera":  'M4 8.5h3.2l1.8-2h6l1.8 2H20v11H4z'
               ' M15.4 14a3.4 3.4 0 1 1-6.8 0 3.4 3.4 0 0 1 6.8 0',
    "star":    'M12 3.4l2.6 5.9 6.4.6-4.8 4.3 1.4 6.4L12 17.3 6.4 20.6l1.4-6.4'
               'L3 9.9l6.4-.6z',
    "clock":   'M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0 M12 7.4V12l3.2 2',
    "moon_dim": 'M20 14.2A8.2 8.2 0 1 1 10.4 3.4 6.6 6.6 0 0 0 20 14.2z'
                ' M12.5 9.5h.01 M15 12h.01',
    "dark":    'M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0 M6.6 12h10.8',
    "grid":    'M4.5 4.5h15v15h-15z M4.5 9.5h15 M4.5 14.5h15 M9.5 4.5v15'
               ' M14.5 4.5v15',
    # The project's own mark: the telescope of the engraved icon reduced to what
    # still reads at 16 px — the tube, the base, one strut, and the four stars
    # (in DOTS). Everything else in the engraving is texture.
    #
    # No tick across the aperture, deliberately: it turned the top of the tube
    # into a T that reads as a mallet. An ellipse there reads as a magnifying
    # glass, and an eyepiece stub at the low end is a stray blob at 16 px. The
    # bare diagonal on a base is the version that survives.
    "mark":    'M7.8 16.2l9.4-9.4 M4.6 20h10.8 M9.8 20l2.6-7',
}

# Filled icons.
FILL = {
    "play": 'M8 5.2l11 6.8-11 6.8z',
    "stop": 'M7.2 7.2h9.6v9.6H7.2z',
    "pause": 'M8.4 5.6h2.7v12.8H8.4z M12.9 5.6h2.7v12.8h-2.7z',
    "dot":  'M12 7.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9z',
}

# Slider handles for the "adjust" icon, drawn filled over the stroke.
#: Filled circles, as (cx, cy, r) on the 24x24 grid. Declared as data rather
#: than as path strings because a circle written as an arc path is unreadable,
#: and because two icons now need them.
DOTS: dict[str, tuple[tuple[float, float, float], ...]] = {
    # slider handles, drawn filled over the stroke
    "adjust": ((9, 7, 2.1), (15, 12, 2.1), (7, 17, 2.1)),
    # the four stars of the mark, in the Crux kite
    "mark": ((7.0, 3.9, 1.15), (4.4, 7.3, 0.85), (9.7, 6.7, 0.85),
             (6.9, 10.3, 0.7)),
}

_cache: dict[tuple, QIcon] = {}


def _svg(name: str, color: str, width: float) -> str:
    parts = []
    if name in STROKE:
        parts.append(
            f'<path d="{STROKE[name]}" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-linecap="round" '
            f'stroke-linejoin="round"/>')
    if name in FILL:
        parts.append(f'<path d="{FILL[name]}" fill="{color}"/>')
    for cx, cy, r in DOTS.get(name, ()):
        parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
    if not parts:
        raise KeyError(f"unknown icon: {name}")
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
            + "".join(parts) + "</svg>")


def icon(name: str, color: str, size: int = 17, width: float = 1.7,
         dpr: int = 2) -> QIcon:
    key = (name, color, size, width, dpr)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    renderer = QSvgRenderer(QByteArray(_svg(name, color, width).encode()))
    pm = QPixmap(size * dpr, size * dpr)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    pm.setDevicePixelRatio(dpr)
    ic = QIcon(pm)
    _cache[key] = ic
    return ic


def pixmap(name: str, color: str, size: int = 17, width: float = 1.7,
           dpr: int = 1) -> QPixmap:
    """A rendered pixmap rather than a QIcon, for callers that compose their own.

    `dpr` defaults to 1 here: an icon built from several sizes wants real pixel
    dimensions, not a device-pixel-ratio hint.
    """
    renderer = QSvgRenderer(QByteArray(_svg(name, color, width).encode()))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    renderer.render(painter)
    painter.end()
    return pm


def names() -> list[str]:
    return sorted(set(STROKE) | set(FILL))


def clear_cache() -> None:
    _cache.clear()
