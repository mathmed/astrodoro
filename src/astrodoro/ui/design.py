from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QLocale, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableView,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..i18n import N_
from ..i18n import gettext as _

MONO_FAMILY = "Menlo"


def font(
    size: int,
    weight: QFont.Weight = QFont.Weight.Normal,
    mono: bool = False,
) -> QFont:
    f = QFont(MONO_FAMILY) if mono else QFont()
    f.setPointSize(size)
    f.setWeight(weight)
    return f


def T_DISPLAY() -> QFont:
    return font(38, QFont.Weight.Bold, mono=True)


def T_XL() -> QFont:
    return font(22, QFont.Weight.DemiBold, mono=True)


def T_L() -> QFont:
    return font(16, QFont.Weight.DemiBold, mono=True)


def T_H2() -> QFont:
    return font(12, QFont.Weight.DemiBold)


def T_BODY() -> QFont:
    return font(12)


def T_MONO() -> QFont:
    return font(11, QFont.Weight.Normal, mono=True)


def T_SMALL() -> QFont:
    return font(10)


def label_font() -> QFont:
    f = T_SMALL()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
    f.setWeight(QFont.Weight.DemiBold)
    return f


@dataclass(frozen=True)
class Palette:
    bg: str
    surface: str
    surface2: str
    border: str
    text: str
    text_dim: str
    ok: str
    warn: str
    bad: str
    idle: str
    accent: str
    plot_bg: str
    curve: str
    mark: str
    image_red: int = 0


DARK = Palette(
    bg="#16181c",
    surface="#1e2126",
    surface2="#262a30",
    border="#343941",
    text="#dcdfe4",
    text_dim="#8b93a0",
    ok="#5fbf7f",
    warn="#e0a44c",
    bad="#e0665f",
    idle="#5a6068",
    accent="#77a8d8",
    plot_bg="#16181c",
    curve="#77c4f0",
    mark="#e0665f",
)

NIGHT = (
    Palette(
        bg="#000000",
        surface="#0a0201",
        surface2="#140403",
        border="#37100a",
        text="#8e2e20",
        text_dim="#5a1c13",
        ok="#4d1a11",
        warn="#8e2e20",
        bad="#cf4632",
        idle="#2a0a06",
        accent="#7a271b",
        plot_bg="#000000",
        curve="#8e2e20",
        mark="#cf4632",
        image_red=170,
    ),
    Palette(
        bg="#000000",
        surface="#0d0302",
        surface2="#1a0605",
        border="#4a1610",
        text="#b03a2a",
        text_dim="#74241a",
        ok="#632217",
        warn="#b03a2a",
        bad="#e8543c",
        idle="#340d08",
        accent="#96301f",
        plot_bg="#000000",
        curve="#b03a2a",
        mark="#e8543c",
        image_red=215,
    ),
    Palette(
        bg="#000000",
        surface="#120403",
        surface2="#210807",
        border="#5e1c14",
        text="#d24631",
        text_dim="#8d2d20",
        ok="#7a2a1c",
        warn="#d24631",
        bad="#ff6a4d",
        idle="#420f09",
        accent="#b3391f",
        plot_bg="#000000",
        curve="#d24631",
        mark="#ff6a4d",
        image_red=255,
    ),
)


def palette(theme: str, night_level: int = 1) -> Palette:
    return DARK if theme != "night" else NIGHT[max(0, min(2, night_level))]


def stylesheet(p: Palette, large_targets: bool = False) -> str:
    pad = "9px 14px" if large_targets else "5px 11px"
    ghost = "8px 12px" if large_targets else "4px 9px"
    ctrl = "6px" if large_targets else "4px"
    bar = "12px" if large_targets else "9px"
    return f"""
QMainWindow, QWidget#root {{ background: {p.bg}; }}
QWidget {{ background: transparent; color: {p.text}; }}

QFrame#card {{ background: {p.surface}; border: 1px solid {p.border};
               border-radius: 7px; }}
QLabel#sectionTitle {{ color: {p.text_dim}; }}
QLabel#statLabel {{ color: {p.text_dim}; }}

QPushButton {{ background: {p.surface2}; border: 1px solid {p.border};
               border-radius: 5px; padding: {pad}; color: {p.text}; }}
QPushButton:hover {{ background: {p.border}; }}
QPushButton:disabled {{ color: {p.idle}; border-color: {p.idle};
                        background: {p.surface}; }}
QPushButton:checked {{ background: {p.accent}; border-color: {p.accent};
                       color: {p.bg}; }}
QPushButton#mode {{ text-align: left; padding: 11px 14px; font-weight: 600; }}
QPushButton#ghost {{ background: transparent; border: none; color: {p.text_dim};
                     padding: {ghost}; }}
QPushButton#ghost:hover {{ color: {p.text}; }}
QPushButton#ghost:checked {{ background: {p.surface2}; border-radius: 5px;
                             color: {p.accent}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QComboBox,
QPlainTextEdit {{
    background: {p.bg}; border: 1px solid {p.border}; border-radius: 4px;
    padding: {ctrl}; color: {p.text}; selection-background-color: {p.accent};
    selection-color: {p.bg}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateTimeEdit:focus,
QComboBox:focus {{
    border-color: {p.accent}; }}
QDateTimeEdit:disabled {{ color: {p.text_dim}; background: {p.surface}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {p.surface2}; color: {p.text};
    border: 1px solid {p.border}; selection-background-color: {p.accent};
    selection-color: {p.bg}; outline: none; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 14px;
    background: transparent; border: none; }}

QSlider::groove:horizontal {{ height: 4px; background: {p.border};
                              border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.text}; width: 14px; margin: -6px 0;
                              border-radius: 7px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}

QCheckBox, QRadioButton {{ color: {p.text}; spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px;
    border: 1px solid {p.border}; border-radius: 3px; background: {p.bg}; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p.accent}; border-color: {p.accent}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: {bar}; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.border}; min-height: 34px;
    border-radius: 4px; margin: 1px; }}
QScrollBar::handle:vertical:hover {{ background: {p.text_dim}; }}
QScrollBar:horizontal {{ background: transparent; height: {bar}; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {p.border}; min-width: 34px;
    border-radius: 4px; margin: 1px; }}
QScrollBar::handle:horizontal:hover {{ background: {p.text_dim}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}

QTableView {{ background: {p.surface}; border: 1px solid {p.border};
    border-radius: 7px; gridline-color: transparent; outline: none;
    color: {p.text}; }}
QTableView::item {{ padding: 2px 8px; border: none; }}
QTableView::item:selected {{ background: {p.accent}; color: {p.bg}; }}
QHeaderView::section {{ background: {p.bg}; color: {p.text_dim}; border: none;
    border-bottom: 1px solid {p.border}; padding: 5px 8px; }}
QTableCornerButton::section {{ background: {p.bg}; border: none; }}

QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {p.surface2};
    border: none; }}
QCalendarWidget QToolButton {{ background: transparent; color: {p.text};
    border: none; border-radius: 4px; padding: 4px 10px; }}
QCalendarWidget QToolButton:hover {{ background: {p.border}; }}
QCalendarWidget QToolButton::menu-indicator {{ image: none; }}
QCalendarWidget QMenu {{ background: {p.surface2}; color: {p.text};
    border: 1px solid {p.border}; }}
QCalendarWidget QMenu::item:selected {{ background: {p.accent}; color: {p.bg}; }}
QCalendarWidget QSpinBox {{ background: {p.bg}; color: {p.text};
    border: 1px solid {p.border}; border-radius: 4px; }}
QCalendarWidget QAbstractItemView {{ background: {p.surface}; color: {p.text};
    border: none; outline: none; selection-background-color: {p.accent};
    selection-color: {p.bg}; }}
QCalendarWidget QAbstractItemView:disabled {{ color: {p.idle}; }}
QCalendarWidget QAbstractItemView::item {{ padding: 0; }}

QStackedWidget {{ background: transparent; }}
QToolTip {{ background: {p.surface2}; color: {p.text};
            border: 1px solid {p.border}; padding: 5px; }}
QProgressBar {{ background: {p.surface2}; border: none; border-radius: 4px;
                height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}
"""


DAY_NAMES = (
    Qt.DayOfWeek.Monday,
    Qt.DayOfWeek.Tuesday,
    Qt.DayOfWeek.Wednesday,
    Qt.DayOfWeek.Thursday,
    Qt.DayOfWeek.Friday,
    Qt.DayOfWeek.Saturday,
    Qt.DayOfWeek.Sunday,
)


def style_calendar(cal: QCalendarWidget, p: Palette) -> None:
    cal.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
    cal.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.ShortDayNames)
    cal.setGridVisible(False)

    day = QTextCharFormat()
    day.setForeground(QColor(p.text))
    for d in DAY_NAMES:
        cal.setWeekdayTextFormat(d, day)
    head = QTextCharFormat()
    head.setForeground(QColor(p.text_dim))
    cal.setHeaderTextFormat(head)

    fm = cal.fontMetrics()
    short = QLocale.FormatType.ShortFormat
    names = (cal.locale().dayName(d.value, short) for d in DAY_NAMES)
    cell = max(fm.horizontalAdvance(n) for n in names) + 12
    view = cal.findChild(QTableView, "qt_calendar_calendarview")
    if view is not None:
        view.horizontalHeader().setMinimumSectionSize(cell)
    cal.setMinimumWidth(cell * 7 + 12)


def image_lut(p: Palette) -> np.ndarray | None:
    if not p.image_red:
        return None
    x = np.arange(256, dtype=np.float32) / 255.0
    lut = np.zeros((256, 3), dtype=np.uint8)
    lut[:, 0] = np.clip(x * p.image_red, 0, 255)
    lut[:, 1] = np.clip(x**2.6 * p.image_red * 0.16, 0, 255)
    lut[:, 2] = np.clip(x**3.4 * p.image_red * 0.07, 0, 255)
    return lut


class Card(QFrame):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(11, 9, 11, 11)
        self._v.setSpacing(7)
        if title:
            lab = QLabel(title.upper())
            lab.setObjectName("sectionTitle")
            f = label_font()
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
            lab.setFont(f)
            self._v.addWidget(lab)

    def add(self, w: QWidget, stretch: int = 0) -> QWidget:
        self._v.addWidget(w, stretch)
        return w

    def add_layout(self, lay):
        self._v.addLayout(lay)
        return lay

    def row(self, *widgets, spacing: int = 6):
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(spacing)
        for w in widgets:
            h.addWidget(w)
        self._v.addLayout(h)
        return h

    def field(self, label: str, w: QWidget, hint: str = "") -> QWidget:
        h = QHBoxLayout()
        h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setFont(T_BODY())
        lab.setMinimumWidth(84)
        if hint:
            lab.setToolTip(hint)
            w.setToolTip(hint)
        h.addWidget(lab)
        h.addWidget(w, 1)
        self._v.addLayout(h)
        return w


class ElidedLabel(QLabel):
    _full = ""
    _mode = Qt.TextElideMode.ElideRight
    _min_chars = 8
    _tip = ""

    def __init__(
        self,
        text: str = "",
        mode=Qt.TextElideMode.ElideRight,
        min_chars: int = 8,
        parent: QWidget | None = None,
    ):
        super().__init__(text, parent)
        self._full = text
        self._mode = mode
        self._min_chars = min_chars
        self._tip = ""

    def text(self) -> str:
        return self._full

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()
        self.updateGeometry()

    def setToolTip(self, tip: str) -> None:
        self._tip = tip
        super().setToolTip(tip)

    def _elide(self) -> None:
        w = self.contentsRect().width()
        fm = self.fontMetrics()
        shown = self._full if w <= 0 else fm.elidedText(self._full, self._mode, w)
        if shown == QLabel.text(self):
            return
        super().setText(shown)
        if not self._tip:
            super().setToolTip("" if shown == self._full else self._full)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._elide()

    def setFont(self, f) -> None:
        super().setFont(f)
        self._elide()

    def minimumSizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(
            fm.horizontalAdvance("0") * self._min_chars,
            super().minimumSizeHint().height(),
        )

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        m = self.contentsMargins()
        return QSize(
            fm.horizontalAdvance(self._full) + m.left() + m.right() + 2,
            super().sizeHint().height(),
        )


class Stat(QWidget):
    def __init__(
        self,
        label: str,
        value: str = "—",
        big: bool = False,
        min_width: int = 104,
        parent=None,
    ):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 1, 0, 1)
        v.setSpacing(3)
        self.lab = QLabel(label.upper())
        self.lab.setObjectName("statLabel")
        self.lab.setFont(label_font())
        self.val = ElidedLabel(value, min_chars=4)
        self.val.setFont(T_XL() if big else T_L())
        v.addWidget(self.lab)
        v.addWidget(self.val)
        v.addStretch(1)
        self.setMinimumWidth(min_width)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def set(self, text: str, color: str | None = None) -> None:
        self.val.setText(text)
        self.val.setStyleSheet(f"color: {color}" if color else "")

    def set_label(self, text: str) -> None:
        self.lab.setText(text.upper())


KIND_LABELS = {
    "stars": N_("too few stars"),
    "trailed": N_("trailed"),
    "smear": N_("flux spread out"),
    "background": N_("high background"),
    "fwhm": N_("FWHM"),
    "registration": N_("registration"),
    "signal": N_("little signal"),
    "settling": N_("settling"),
    "reference": N_("reference frame"),
}

CRITERION_LABELS = {
    "weight": N_("weight"),
    "elongation": N_("elongation"),
    "fwhm": N_("FWHM"),
    "halo": N_("halo"),
    "stars": N_("stars"),
}


def kind_label(kind: str) -> str:
    return _(KIND_LABELS.get(kind, kind))


class HealthStrip(QWidget):
    chosen = Signal(int)

    def __init__(self, capacity: int = 90, parent=None):
        super().__init__(parent)
        self.capacity = capacity
        self.marks: list[int] = []
        self.info: list[dict] = []
        self.sel: int | None = None
        self.has_image = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(14)
        self.setMaximumHeight(14)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setVisible(False)
        self._p = DARK

    def set_palette(self, p: Palette) -> None:
        self._p = p
        self.update()

    def push(self, ok: bool, info: dict | None = None) -> None:
        self.marks.append(1 if ok else 0)
        self.info.append(info or {})
        if len(self.marks) > self.capacity:
            cut = len(self.marks) - self.capacity
            del self.marks[:cut]
            del self.info[:cut]
            if self.sel is not None:
                self.sel = None if self.sel < cut else self.sel - cut
        self.setVisible(True)
        self.update()

    def clear(self) -> None:
        self.marks.clear()
        self.info.clear()
        self.sel = None
        self.setVisible(False)
        self.update()

    def select(self, i: int | None) -> None:
        self.sel = i if (i is not None and 0 <= i < len(self.marks)) else None
        self.update()

    def index_at(self, x: float) -> int:
        if not self.marks:
            return -1
        bw = max(2.0, self.width() / max(len(self.marks), self.capacity))
        i = int(x // bw)
        return i if 0 <= i < len(self.marks) else -1

    def mouseMoveEvent(self, ev) -> None:
        i = self.index_at(ev.position().x())
        if i < 0:
            QToolTip.hideText()
            return
        QToolTip.showText(ev.globalPosition().toPoint(), self.tooltip(i), self)

    def tooltip(self, i: int) -> str:
        d = self.info[i] or {}
        ok = bool(self.marks[i])
        n = d.get("index")
        parts = [
            _("frame {n}").format(n=n)
            if n
            else _("{n} back").format(n=len(self.marks) - i),
            _("accepted") if ok else _("rejected"),
        ]
        if not ok and d.get("reason"):
            parts[-1] = _("rejected — {reason}").format(reason=d["reason"])
        measures = []
        if d.get("n_stars") is not None:
            measures.append(_("{n} stars").format(n=d["n_stars"]))
        for key, name in (("hfr", "HFR"), ("fwhm", "FWHM")):
            v = d.get(key)
            if v is not None and v == v:
                measures.append(f"{name} {v:.2f} px")
        if d.get("time"):
            measures.append(d["time"])
        stored = self.has_image is None or self.has_image(n)
        return (
            "  ·  ".join(parts)
            + ("\n" + "  ·  ".join(measures) if measures else "")
            + self.criteria(d)
            + (
                "\n" + _("click to see this frame")
                if stored
                else "\n" + _("out of memory — image not kept")
            )
        )

    def criteria(self, d: dict) -> str:
        limits = d.get("limits") or {}
        lines = []
        for key, fmt in (
            ("weight", "{:.2f}"),
            ("elongation", "{:.2f}"),
            ("fwhm", "{:.2f} px"),
            ("halo", "{:.2f}"),
        ):
            v, ceiling = d.get(key), limits.get(key)
            if (
                v is None
                or ceiling is None
                or not (np.isfinite(v) and np.isfinite(ceiling))
            ):
                continue
            from_below = key == "weight"
            failed = v < ceiling if from_below else v > ceiling
            name = _(CRITERION_LABELS.get(key, key))
            lines.append(
                f"{name:<12} "
                + fmt.format(v)
                + " / "
                + fmt.format(ceiling)
                + ("  " + _("FAIL") if failed else "")
            )
        if lines and d.get("weight") is not None:
            lines.append(
                _(
                    "(weight = what this frame is worth compared to what "
                    "the night is delivering)"
                )
            )
        return ("\n" + "\n".join(lines)) if lines else ""

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        i = self.index_at(ev.position().x())
        if i >= 0:
            self.chosen.emit(i)

    def leaveEvent(self, ev) -> None:
        QToolTip.hideText()

    def streak(self) -> int:
        n = 0
        for m in reversed(self.marks):
            if m:
                break
            n += 1
        return n

    def paintEvent(self, ev) -> None:
        if not self.marks:
            return
        pnt = QPainter(self)
        w, h = self.width(), self.height()
        n = len(self.marks)
        bw = max(2.0, w / max(n, self.capacity))
        for i, m in enumerate(self.marks):
            x = i * bw
            col = QColor(self._p.ok if m else self._p.bad)
            pnt.fillRect(
                int(x), 2 if m else 0, max(1, int(bw - 1)), h - 4 if m else h, col
            )
        if self.sel is not None and self.sel < n:
            pnt.setPen(QPen(QColor(self._p.text), 1))
            pnt.setBrush(Qt.BrushStyle.NoBrush)
            pnt.drawRect(int(self.sel * bw) - 1, 0, max(3, int(bw - 1) + 2), h - 1)


class ModeRail(QWidget):
    changed = Signal(str)

    def __init__(self, modes: list[tuple], columns: int = 2, parent=None):
        super().__init__(parent)
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(5)
        self.buttons: dict[str, QPushButton] = {}
        self.icon_names: dict[str, str] = {}
        self._provider = None
        for i, mode in enumerate(modes):
            key, label, hint = mode[0], mode[1], mode[2]
            b = QPushButton(label)
            b.setObjectName("mode")
            b.setCheckable(True)
            b.setToolTip(hint)
            b.clicked.connect(lambda _checked=False, k=key: self.select(k))
            g.addWidget(b, i // columns, i % columns)
            self.buttons[key] = b
            if len(mode) > 3 and mode[3]:
                self.icon_names[key] = mode[3]
        for c in range(columns):
            g.setColumnStretch(c, 1)
        self.current = modes[0][0]
        self.buttons[self.current].setChecked(True)

    def set_icon_provider(self, fn) -> None:
        self._provider = fn
        self.refresh_icons()

    def refresh_icons(self) -> None:
        if self._provider is None:
            return
        for key, b in self.buttons.items():
            name = self.icon_names.get(key)
            if not name:
                continue
            b.setIcon(self._provider(name, b.isChecked()))
            b.setIconSize(QSize(17, 17))

    def mark(self, key: str) -> None:
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        self.current = key
        self.refresh_icons()

    def select(self, key: str) -> None:
        self.mark(key)
        self.changed.emit(key)


def hms(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def shorten(path: str, keep: int = 34) -> str:
    from pathlib import Path as _Path

    p = str(path)
    home = str(_Path.home())
    if p.startswith(home):
        p = "~" + p[len(home) :]
    return p if len(p) <= keep else "…" + p[-(keep - 1) :]


def tag(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("statLabel")
    lab.setFont(T_SMALL())
    return lab
