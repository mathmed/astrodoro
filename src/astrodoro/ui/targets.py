from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ..core.tonight import Suggestion, compass_point
from ..i18n import N_
from ..i18n import gettext as _

COLUMNS = [
    N_("score"),
    N_("object"),
    N_("type"),
    N_("mag"),
    N_("size"),
    N_("altitude"),
    N_("time left"),
    N_("Moon"),
]
STRETCH_COLUMN = 1


class TargetTable(QTableWidget):
    chosen = Signal(object)
    activated_target = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels([_(c) for c in COLUMNS])
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setWordWrap(False)
        self.setSortingEnabled(False)
        head = self.horizontalHeader()
        for i in range(len(COLUMNS)):
            head.setSectionResizeMode(
                i,
                QHeaderView.ResizeMode.Stretch
                if i == STRETCH_COLUMN
                else QHeaderView.ResizeMode.ResizeToContents,
            )
        self.verticalHeader().setDefaultSectionSize(26)
        self._rows: list[Suggestion] = []
        self.pal = None
        self.itemSelectionChanged.connect(self._selected)
        self.itemDoubleClicked.connect(lambda item: self._emit_activated(item.row()))

    def set_palette(self, pal) -> None:
        self.pal = pal
        self._repaint_scores()

    def set_rows(self, rows: list[Suggestion]) -> None:
        keep = self.current()
        keep_name = keep.obj.name if keep is not None else None
        self._rows = rows
        self.setRowCount(len(rows))
        for r, s in enumerate(rows):
            for c, text in enumerate(_cells(s)):
                item = QTableWidgetItem(text)
                if c in (0, 3, 4, 5, 6, 7):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                item.setToolTip(" · ".join(s.reasons()))
                self.setItem(r, c, item)
        self._repaint_scores()
        if keep_name is not None:
            for r, s in enumerate(rows):
                if s.obj.name == keep_name:
                    self.selectRow(r)
                    return
        if rows:
            self.selectRow(0)
        else:
            self.chosen.emit(None)

    def current(self) -> Suggestion | None:
        r = self.currentRow()
        return self._rows[r] if 0 <= r < len(self._rows) else None

    def _repaint_scores(self) -> None:
        if self.pal is None:
            return
        for r, s in enumerate(self._rows):
            item = self.item(r, 0)
            if item is None:
                continue
            item.setForeground(QColor(_score_colour(s.score, self.pal)))

    def _selected(self) -> None:
        self.chosen.emit(self.current())

    def _emit_activated(self, row: int) -> None:
        if 0 <= row < len(self._rows):
            self.activated_target.emit(self._rows[row])

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._emit_activated(self.currentRow())
            return
        super().keyPressEvent(ev)


def _score_colour(score: float, pal) -> str:
    if score >= 70:
        return pal.ok
    if score >= 45:
        return pal.warn
    return pal.text_dim


def _cells(s: Suggestion) -> list[str]:
    o = s.obj
    if not (np.isfinite(o.major_arcmin) and o.major_arcmin > 0):
        size = "—"
    elif o.major_arcmin < 1.0:
        size = f'{o.major_arcmin * 60:.0f}"'
    else:
        size = f"{o.major_arcmin:.0f}'"
    left = (
        _("all night")
        if np.isinf(s.minutes_left)
        else _("{min:.0f} min").format(min=s.minutes_left)
    )
    moon = "—" if not np.isfinite(s.moon_sep) else f"{s.moon_sep:.0f}°"
    return [
        f"{s.score:.0f}",
        o.label,
        o.kind_label,
        f"{o.mag:.1f}" if np.isfinite(o.mag) else "—",
        size,
        f"{s.alt:.0f}° {compass_point(s.az)}",
        left,
        moon,
    ]
