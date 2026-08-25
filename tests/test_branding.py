"""The application icon carries two drawings, and picks the right one.

The engraved plate is unreadable below about 48 px — its hatching collapses into
flat grey — so the small sizes come from the simplified stroke glyph. If either
half goes missing the icon still "works", just badly, which is exactly the kind
of regression nobody notices.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QSize

from astrodoro.ui import icons
from astrodoro.ui.branding import GLYPH_SIZES, PLATE, PLATE_SIZES, app_icon


def _opacity(pixmap) -> float:
    """Fraction of fully opaque pixels: the plate is a filled squircle, the
    glyph is a thin stroke on transparency."""
    img = pixmap.toImage()
    w, h = img.width(), img.height()
    opaque = sum(1 for y in range(h) for x in range(w)
                 if img.pixelColor(x, y).alpha() > 200)
    return opaque / max(w * h, 1)


def test_the_mark_glyph_exists():
    assert "mark" in icons.STROKE
    assert len(icons.DOTS["mark"]) == 4, "the mark carries the four Crux stars"


def test_dots_still_serve_the_slider_icon(qapp):
    """`DOTS` replaced a hardcoded block that only knew about `adjust`.

    Needs `qapp`: QPixmap aborts the process without a QApplication.
    """
    assert len(icons.DOTS["adjust"]) == 3
    assert not icons.pixmap("adjust", "#ffffff", 24).isNull()


def test_the_plate_is_package_data():
    """It must sit inside the package, or an installed copy has no icon."""
    assert PLATE.exists(), f"{PLATE} missing — run scripts/derive_brand.py"
    assert PLATE.parts[-3:] == ("ui", "assets", "icon.png")


def test_the_icon_offers_every_size(qapp):
    have = {s.width() for s in app_icon().availableSizes()}
    for size in (*GLYPH_SIZES, *PLATE_SIZES):
        assert size in have, f"{size} px missing from the icon"


@pytest.mark.parametrize("size", GLYPH_SIZES)
def test_small_sizes_come_from_the_glyph(qapp, size):
    pm = app_icon().pixmap(QSize(size, size))
    assert _opacity(pm) < 0.4, (
        f"at {size} px the icon looks like the filled plate, not the glyph — "
        "the plate is illegible at this size")


@pytest.mark.parametrize("size", (128, 256, 512))
def test_large_sizes_come_from_the_plate(qapp, size):
    pm = app_icon().pixmap(QSize(size, size))
    assert _opacity(pm) > 0.5, (
        f"at {size} px the icon looks like the bare glyph, not the engraved "
        "plate — the detail is what the Dock has room for")


def test_the_window_sets_the_icon(qapp, settings):
    """`main()` sets it on the application; the window inherits it."""
    from astrodoro.ui.main import MainWindow
    qapp.setWindowIcon(app_icon())
    w = MainWindow(settings)
    try:
        assert not w.windowIcon().isNull()
    finally:
        w.close()
