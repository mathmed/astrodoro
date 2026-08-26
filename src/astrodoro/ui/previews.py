"""The DSS thumbnail on screen, and the thread that fetches it.

Two pieces, deliberately apart: `PreviewLoader` knows about threads and never
touches a widget, `PreviewView` knows about painting and never touches the
network. `core/previews.py` sits under both and knows only files and HTTP.

What the picture is *for* is the rectangle drawn on it. "M8 is 45 arcminutes"
and "the frame is 51x29" are two numbers nobody composes in their head at
midnight; the same fact drawn on the real sky is instant — and it is the
difference between choosing a target and choosing a disappointment.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ..core import previews
from ..i18n import gettext as _
from .design import T_SMALL, label_font

#: Two at a time. The CDS is a free service run for everyone, and a wall of
#: parallel requests from one back garden is not a reasonable way to use it.
WORKERS = 2


class PreviewLoader(QObject):
    """Fetches cutouts off the Qt thread and hands back file paths."""

    #: key, path — key is the catalogue name the request was made with.
    ready = Signal(str, str)
    #: key — nothing could be fetched (offline, or the object is not covered).
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(max_workers=WORKERS,
                                        thread_name_prefix="preview")
        self._pending: set[str] = set()
        self._closed = False

    def cached(self, name: str, fov: float) -> Path | None:
        return previews.cached(name, fov)

    def request(self, name: str, ra: float, dec: float, fov: float) -> Path | None:
        """The cutout now if it is on disk, otherwise a fetch and `ready` later.

        Returns the cached path so the common case — an object looked at twice —
        paints without a round trip through the thread pool.
        """
        hit = previews.cached(name, fov)
        if hit is not None:
            return hit
        if self._closed:
            return None
        key = f"{name}@{fov:.0f}"
        if key in self._pending:
            return None
        self._pending.add(key)
        self._pool.submit(self._work, key, name, ra, dec, fov)
        return None

    def _work(self, key: str, name: str, ra: float, dec: float,
              fov: float) -> None:
        path = previews.fetch(name, ra, dec, fov)
        self._pending.discard(key)
        if self._closed:
            return
        # Signals cross threads on their own; nothing here touches a widget.
        if path is None:
            self.failed.emit(name)
        else:
            self.ready.emit(name, str(path))

    def shutdown(self) -> None:
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


class PreviewView(QWidget):
    """The cutout, with the sensor's field drawn on it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(150, 150)
        self.pal = None
        self._pix: QPixmap | None = None
        self._frame: tuple[float, float] | None = None   # fractions of the cutout
        self._note = _("pick an object")
        self._label = ""

    # ------------------------------------------------------------------ state
    def set_palette(self, pal) -> None:
        self.pal = pal
        self.update()

    def clear(self, note: str) -> None:
        self._pix = None
        self._frame = None
        self._label = ""
        self._note = note
        self.update()

    def show_image(self, path: str, frame: tuple[float, float] | None,
                   label: str = "") -> None:
        pix = QPixmap(path)
        # A JPEG that fails to load is a corrupt cache entry, not a picture.
        self._pix = pix if not pix.isNull() else None
        self._frame = frame
        self._label = label
        if self._pix is None:
            self._note = _("unreadable preview")
        self.update()

    # ----------------------------------------------------------------- paint
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        pal = self.pal
        bg = QColor(pal.bg if pal else "#000000")
        p.fillRect(self.rect(), bg)
        if self._pix is None:
            p.setPen(QColor(pal.text_dim if pal else "#888888"))
            p.setFont(T_SMALL())
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self._note)
            return

        side = min(self.width(), self.height())
        box = QRectF((self.width() - side) / 2.0, (self.height() - side) / 2.0,
                     side, side)
        p.drawPixmap(box.toRect(), self._pix)

        if self._frame is not None:
            fw, fh = self._frame
            # Clamped, not hidden: a frame wider than the cutout still has to
            # say so, and a rectangle running off the edges says it clearly.
            w, h = min(fw, 1.6) * side, min(fh, 1.6) * side
            r = QRectF(box.center().x() - w / 2, box.center().y() - h / 2, w, h)
            p.setPen(QPen(QColor(pal.accent if pal else "#77a8d8"), 2))
            p.drawRect(r)

        if self._label:
            p.setFont(label_font())
            p.setPen(QColor(pal.text_dim if pal else "#888888"))
            p.drawText(box.adjusted(4, 0, -4, -3),
                       Qt.AlignBottom | Qt.AlignRight, self._label)
