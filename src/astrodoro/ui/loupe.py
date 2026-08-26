"""The 5x loupe, floating over the live image.

It used to be the reason a whole mode existed. In practice the mode was opened
for this one widget and left immediately, because focusing is not a phase of the
night — it is something you redo whenever the temperature drifts, in the middle
of whatever you were doing. Here it is a panel you switch on over the frame you
are already looking at, in any mode, and the number it shows is the same one the
vitals bar carries all night.

Magnification is by pixel repetition, in `core/focus.loupe`: interpolating would
smooth exactly the shape you are trying to judge.
"""
from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..i18n import gettext as _
from .design import T_SMALL, T_XL, label_font

#: Side of the panel in pixels. The crop is 56x56 luminance pixels at 5x, so
#: anything wider would be empty margin.
SIDE = 208


class Loupe(QFrame):
    """A card that draws itself over the image, bottom right."""

    closed = Signal()
    beep_toggled = Signal(bool)
    reset_best = Signal()
    auto_star = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFixedWidth(SIDE)
        v = QVBoxLayout(self)
        v.setContentsMargins(9, 7, 9, 8)
        v.setSpacing(5)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        title = QLabel(_("loupe 5x"))
        title.setObjectName("statLabel")
        title.setFont(label_font())
        close = QPushButton("✕")
        close.setObjectName("ghost")
        close.setFixedWidth(22)
        close.setToolTip(_("close the loupe (Z)"))
        close.clicked.connect(self.closed.emit)
        top.addWidget(title, 1)
        top.addWidget(close)
        v.addLayout(top)

        self.view = pg.GraphicsLayoutWidget()
        self.view.setFixedHeight(SIDE - 34)
        vb = self.view.addViewBox(lockAspect=True, invertY=True)
        vb.setMouseEnabled(False, False)
        self.img = pg.ImageItem()
        vb.addItem(self.img)
        self._vb = vb
        v.addWidget(self.view)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        self.lbl_hfr = QLabel("—")
        self.lbl_hfr.setFont(T_XL())
        self.lbl_best = QLabel("")
        self.lbl_best.setFont(T_SMALL())
        self.lbl_best.setObjectName("statLabel")
        self.lbl_best.setAlignment(Qt.AlignRight | Qt.AlignBottom)
        row.addWidget(self.lbl_hfr)
        row.addWidget(self.lbl_best, 1)
        v.addLayout(row)

        self.lbl_verdict = QLabel(_("waiting for stars"))
        self.lbl_verdict.setFont(T_SMALL())
        self.lbl_verdict.setWordWrap(True)
        v.addWidget(self.lbl_verdict)

        self.lbl_where = QLabel("")
        self.lbl_where.setFont(T_SMALL())
        self.lbl_where.setObjectName("statLabel")
        self.lbl_where.setWordWrap(True)
        v.addWidget(self.lbl_where)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.chk_beep = QCheckBox(_("beep"))
        self.chk_beep.setToolTip(_(
            "One beep per frame, pitch rising as the HFR falls. With a hand on "
            "the focuser you are not looking at the screen."))
        self.chk_beep.toggled.connect(self.beep_toggled.emit)
        self.btn_auto = QPushButton(_("auto"))
        self.btn_auto.setToolTip(_("follow the brightest star again — click the "
                                   "image to pin another one"))
        self.btn_auto.clicked.connect(self.auto_star.emit)
        self.btn_best = QPushButton("↺")
        self.btn_best.setFixedWidth(30)
        self.btn_best.setToolTip(_("forget the session's best focus"))
        self.btn_best.clicked.connect(self.reset_best.emit)
        row.addWidget(self.chk_beep, 1)
        row.addWidget(self.btn_auto)
        row.addWidget(self.btn_best)
        v.addLayout(row)

        self.adjustSize()

    # ------------------------------------------------------------------ state
    def set_focus(self, crop, d: dict, pal) -> None:
        hfr = d.get("hfr", float("nan"))
        best = d.get("best", float("nan"))
        ratio = d.get("ratio", float("nan"))
        trend = d.get("trend", float("nan"))

        color = None
        if np.isfinite(ratio):
            color = (pal.ok if ratio < 1.05
                     else (pal.warn if ratio < 1.3 else pal.bad))
        self.lbl_hfr.setText(f"{hfr:.2f}" if np.isfinite(hfr) else "—")
        self.lbl_hfr.setStyleSheet(f"color: {color}" if color else "")
        self.lbl_best.setText(_("best {best:.2f}").format(best=best)
                              if np.isfinite(best) else "")
        self.lbl_verdict.setText(
            d.get("verdict", "—")
            + (f"   {trend:+.2f} px/min" if np.isfinite(trend) else ""))
        if crop is not None:
            c = crop.astype(np.float32)
            # Percentile floor rather than the minimum: one cold pixel in the
            # crop would set the black point and flatten the star.
            lo, hi = float(np.percentile(c, 5)), float(c.max())
            self.img.setImage(np.clip((c - lo) / max(hi - lo, 1e-6), 0, 1),
                              autoLevels=False, levels=(0, 1))
            self._vb.autoRange(padding=0)

    def set_pinned(self, xy) -> None:
        self.lbl_where.setText(
            _("pinned at {x:.0f}, {y:.0f}").format(x=xy[0], y=xy[1]) if xy
            else _("following the brightest star"))

    def set_palette(self, pal) -> None:
        self.view.setBackground(pal.plot_bg)

    # ---------------------------------------------------------------- placing
    def place(self) -> None:
        """Bottom right of the parent, clear of the image bar and the scrollbar."""
        p = self.parentWidget()
        if p is None:
            return
        self.adjustSize()
        self.move(max(p.width() - self.width() - 12, 0),
                  max(p.height() - self.height() - 12, 0))
        self.raise_()
