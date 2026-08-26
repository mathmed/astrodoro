"""Design system: tokens, typography and reusable components.

Two decisions run through everything:

1. **Hierarchy by size, not by box.** The session's vital information
   (integration, frame health, HFR, platform budget) is large and always
   visible; everything else shrinks.

2. **In night mode, meaning comes from brightness, not hue.** If the whole
   screen is red, green-amber-red stops working as a code. So "ok", "warning"
   and "problem" become dim, medium and intense red.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from ..i18n import N_
from ..i18n import gettext as _

MONO_FAMILY = "Menlo"


# ------------------------------------------------------------------ typography
def font(size: int, weight: int = QFont.Normal, mono: bool = False) -> QFont:
    """An interface font. No family for ordinary text: `QFont()` already starts
    with the application's default font.

    Asking for an empty family — `QFont("", 10)` — is **not** the same thing: on
    macOS that matches `.Apple Color Emoji UI`, whose space character measures
    19 px against the system font's 4 px. The text comes out with the words
    spread apart as if justified.
    """
    f = QFont(MONO_FAMILY) if mono else QFont()
    f.setPointSize(size)
    f.setWeight(weight)
    return f


def T_DISPLAY() -> QFont: return font(38, QFont.Bold, mono=True)
def T_XL() -> QFont: return font(22, QFont.DemiBold, mono=True)
def T_L() -> QFont: return font(16, QFont.DemiBold, mono=True)
def T_H1() -> QFont: return font(15, QFont.DemiBold)
def T_H2() -> QFont: return font(12, QFont.DemiBold)
def T_BODY() -> QFont: return font(12)
def T_MONO() -> QFont: return font(11, QFont.Normal, mono=True)
def T_SMALL() -> QFont: return font(10)


def label_font() -> QFont:
    """The small, letter-spaced font used for every stat and section label."""
    f = T_SMALL()
    f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
    f.setWeight(QFont.DemiBold)
    return f


# --------------------------------------------------------------------- palettes
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
    #: Ceiling of the red image ramp in night mode (0 = do not apply).
    image_red: int = 0


DARK = Palette(
    bg="#16181c", surface="#1e2126", surface2="#262a30", border="#343941",
    text="#dcdfe4", text_dim="#8b93a0",
    ok="#5fbf7f", warn="#e0a44c", bad="#e0665f", idle="#5a6068",
    accent="#77a8d8", plot_bg="#16181c", curve="#77c4f0", mark="#e0665f",
)

# Three night intensities. The darkest is the one that preserves dark
# adaptation; the others exist because not every site is genuinely dark.
NIGHT = (
    Palette(bg="#000000", surface="#0a0201", surface2="#140403", border="#37100a",
            text="#8e2e20", text_dim="#5a1c13",
            ok="#4d1a11", warn="#8e2e20", bad="#cf4632", idle="#2a0a06",
            accent="#7a271b", plot_bg="#000000", curve="#8e2e20", mark="#cf4632",
            image_red=170),
    Palette(bg="#000000", surface="#0d0302", surface2="#1a0605", border="#4a1610",
            text="#b03a2a", text_dim="#74241a",
            ok="#632217", warn="#b03a2a", bad="#e8543c", idle="#340d08",
            accent="#96301f", plot_bg="#000000", curve="#b03a2a", mark="#e8543c",
            image_red=215),
    Palette(bg="#000000", surface="#120403", surface2="#210807", border="#5e1c14",
            text="#d24631", text_dim="#8d2d20", ok="#7a2a1c", warn="#d24631",
            bad="#ff6a4d", idle="#420f09", accent="#b3391f",
            plot_bg="#000000", curve="#d24631", mark="#ff6a4d", image_red=255),
)


def palette(theme: str, night_level: int = 1) -> Palette:
    return DARK if theme != "night" else NIGHT[max(0, min(2, night_level))]


def stylesheet(p: Palette, large_targets: bool = False) -> str:
    """`large_targets` grows the click areas — in the dark, cold and without
    reading glasses, a small target is expensive.

    Central caution: do **not** give a `background` to the generic `QWidget`
    selector. Every QWidget used merely to group a layout would inherit the
    window background and paint it over the card, drawing a dark rectangle
    around each group. Neutral widgets stay transparent and the background is
    declared only on the root, on cards and on input fields.
    """
    pad = "9px 14px" if large_targets else "5px 11px"
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
                     padding: 2px 6px; }}
QPushButton#ghost:hover {{ color: {p.text}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {p.bg}; border: 1px solid {p.border}; border-radius: 4px;
    padding: {ctrl}; color: {p.text}; selection-background-color: {p.accent};
    selection-color: {p.bg}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {p.accent}; }}
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

QStackedWidget {{ background: transparent; }}
QToolTip {{ background: {p.surface2}; color: {p.text};
            border: 1px solid {p.border}; padding: 5px; }}
QProgressBar {{ background: {p.surface2}; border: none; border-radius: 4px;
                height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}
"""


def image_lut(p: Palette) -> np.ndarray | None:
    """The red ramp applied to the whole image in night mode.

    Painting only the controls and leaving a white 2000x1400 nebula on screen
    preserves no dark adaptation at all — the image is the program's largest
    light source.
    """
    if not p.image_red:
        return None
    x = np.arange(256, dtype=np.float32) / 255.0
    lut = np.zeros((256, 3), dtype=np.uint8)
    lut[:, 0] = np.clip(x * p.image_red, 0, 255)
    lut[:, 1] = np.clip(x ** 2.6 * p.image_red * 0.16, 0, 255)
    lut[:, 2] = np.clip(x ** 3.4 * p.image_red * 0.07, 0, 255)
    return lut


# ------------------------------------------------------------------ components
class Card(QFrame):
    """A group with a discreet title.

    Replaces QGroupBox, whose framed inset title steals height and attention.
    """

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
            f.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
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
    """A one-line readout that shrinks instead of widening the window.

    A plain `QLabel` reports the width of its whole text as its *minimum* width,
    and Qt honours a layout's minimum by resizing the window: one long readout —
    the object's name, the view bar's line of measurements — was enough to make
    the window wider than the screen, with its right edge out of sight. Measured
    on the TARGETS screen: the summary line asked for 992 px and took the central
    widget's minimum from 1334 px to 2082 px.

    So the width is decided by the layout, not by the text: whatever does not fit
    is elided, and the full string stays in the tooltip. `text()` still answers
    the full string — the elision is only what gets painted.
    """

    #: Class-level defaults so a paint or resize that arrives before `__init__`
    #: finishes still has something to read.
    _full = ""
    _mode = Qt.ElideRight
    _min_chars = 8
    _tip = ""

    def __init__(self, text: str = "", mode=Qt.ElideRight, min_chars: int = 8,
                 parent: QWidget | None = None):
        super().__init__(text, parent)
        self._full = text
        self._mode = mode
        self._min_chars = min_chars
        self._tip = ""

    # ------------------------------------------------------------------- text
    def text(self) -> str:
        return self._full

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()
        self.updateGeometry()

    def setToolTip(self, tip: str) -> None:
        """A tooltip set from outside wins over the full-text one."""
        self._tip = tip
        super().setToolTip(tip)

    def _elide(self) -> None:
        w = self.contentsRect().width()
        fm = self.fontMetrics()
        shown = self._full if w <= 0 else fm.elidedText(self._full, self._mode, w)
        # Only when it actually changed: `QLabel.setText` invalidates the layout,
        # and re-eliding on every resize event would re-enter the layout pass.
        if shown == QLabel.text(self):
            return
        super().setText(shown)
        if not self._tip:
            super().setToolTip("" if shown == self._full else self._full)

    # ----------------------------------------------------------------- events
    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._elide()

    def setFont(self, f) -> None:
        super().setFont(f)
        self._elide()

    # ------------------------------------------------------------------ sizes
    def minimumSizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance("0") * self._min_chars,
                     super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        """Asked from the *full* text: the layout should still hand over the
        whole width when there is width to hand over."""
        fm = self.fontMetrics()
        m = self.contentsMargins()
        return QSize(fm.horizontalAdvance(self._full) + m.left() + m.right() + 2,
                     super().sizeHint().height())


class Stat(QWidget):
    """Small label on top, large value underneath — the unit of the vitals bar.

    The minimum width and margins are what stop the readouts from touching each
    other and the label from touching the card border.
    """

    def __init__(self, label: str, value: str = "—", big: bool = False,
                 min_width: int = 104, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 1, 0, 1)
        v.setSpacing(3)
        self.lab = QLabel(label.upper())
        self.lab.setObjectName("statLabel")
        self.lab.setFont(label_font())
        # Elided: an object's full name ("NGC 6543 (Cat's Eye Nebula)") in the
        # 22 pt display font asks for ~600 px, and a plain label would take the
        # whole window with it.
        self.val = ElidedLabel(value, min_chars=4)
        self.val.setFont(T_XL() if big else T_L())
        v.addWidget(self.lab)
        v.addWidget(self.val)
        v.addStretch(1)
        self.setMinimumWidth(min_width)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def set(self, text: str, color: str | None = None) -> None:
        self.val.setText(text)
        self.val.setStyleSheet(f"color: {color}" if color else "")

    def set_label(self, text: str) -> None:
        self.lab.setText(text.upper())


#: Display name of each rejection category and of each criterion, keyed by the
#: identifiers `core.stacker` produces. Kept here so the whole interface names
#: them the same way, and so translators find them in one place.
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
    """The last N frames as marks: accepted, rejected, reason on hover.

    A rejection count says nothing; the *sequence* says everything. Three
    isolated rejections in fifty frames is seeing. Twelve in a row is cloud, dew
    or the tube off target — and you want to see that without reading a log.

    Hovering tells you what happened to the frame; clicking shows the frame.
    """

    chosen = Signal(int)             # mark clicked, by position in the strip

    def __init__(self, capacity: int = 90, parent=None):
        super().__init__(parent)
        self.capacity = capacity
        self.marks: list[int] = []      # 1 accepted, 0 rejected
        self.info: list[dict] = []      # per-mark detail, for the tooltip
        self.sel: int | None = None     # mark under review, or none
        # Optional "is this frame still stored?" predicate: only the owner of
        # the history knows, and without it the text would invite clicking an
        # old mark whose image is gone.
        self.has_image = None
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(14)
        self.setMaximumHeight(14)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
            # The selection is a position in the strip, and the strip moved:
            # without fixing it here, the review outline migrates to another
            # frame on its own.
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
        """Highlight the mark under review. Highlight only — the window is the
        only thing that has the image."""
        self.sel = i if (i is not None and 0 <= i < len(self.marks)) else None
        self.update()

    # --------------------------------------------------------------- details
    def index_at(self, x: float) -> int:
        """The mark under the cursor, or -1."""
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
        """Everything known about one frame, for the hover.

        The strip shows the pattern; the next question is always "that red one,
        why?". Without this, answering it meant hunting the right line in a log
        that has already scrolled away.
        """
        d = self.info[i] or {}
        ok = bool(self.marks[i])
        n = d.get("index")
        parts = [_("frame {n}").format(n=n) if n
                 else _("{n} back").format(n=len(self.marks) - i),
                 _("accepted") if ok else _("rejected")]
        if not ok and d.get("reason"):
            parts[-1] = _("rejected — {reason}").format(reason=d["reason"])
        measures = []
        if d.get("n_stars") is not None:
            measures.append(_("{n} stars").format(n=d["n_stars"]))
        for key, name in (("hfr", "HFR"), ("fwhm", "FWHM")):
            v = d.get(key)
            if v is not None and v == v:        # discards NaN
                measures.append(f"{name} {v:.2f} px")
        if d.get("time"):
            measures.append(d["time"])
        stored = self.has_image is None or self.has_image(n)
        return ("  ·  ".join(parts)
                + ("\n" + "  ·  ".join(measures) if measures else "")
                + self.criteria(d)
                + ("\n" + _("click to see this frame") if stored
                   else "\n" + _("out of memory — image not kept")))

    def criteria(self, d: dict) -> str:
        """Each measurement next to its limit, and which one failed.

        A frame's verdict comes from criteria whose rulers are of different
        natures — elongation is an absolute limit, FWHM is relative to the best
        of the integration — and without seeing both numbers side by side the
        decision looks like a lottery. Here it looks like what it is: one line
        per criterion, value against limit, and the word FAIL on whichever
        blocked it. Deliberately no colour: in night mode hue separates nothing.
        """
        limits = d.get("limits") or {}
        lines = []
        # Weight comes first: it is what decides. The others follow because they
        # say WHICH defect pulled the weight down, which is what guides action.
        for key, fmt in (("weight", "{:.2f}"), ("elongation", "{:.2f}"),
                         ("fwhm", "{:.2f} px"), ("halo", "{:.2f}")):
            v, ceiling = d.get(key), limits.get(key)
            # An infinite limit is the first frame of the integration, which has
            # no best FWHM to compare against yet; showing "/ inf" is noise.
            if (v is None or ceiling is None
                    or not (np.isfinite(v) and np.isfinite(ceiling))):
                continue
            # Weight fails from BELOW (more is better); defects fail from above.
            from_below = key == "weight"
            failed = v < ceiling if from_below else v > ceiling
            name = _(CRITERION_LABELS.get(key, key))
            lines.append(f"{name:<12} " + fmt.format(v) + " / "
                         + fmt.format(ceiling)
                         + ("  " + _("FAIL") if failed else ""))
        if lines and d.get("weight") is not None:
            lines.append(_("(weight = what this frame is worth compared to what "
                           "the night is delivering)"))
        return ("\n" + "\n".join(lines)) if lines else ""

    def mousePressEvent(self, ev) -> None:
        """A click opens the frame behind that mark.

        The strip answers "when" and "why"; what is left is "how did that frame
        look", and only the image answers that. The sub is on disk, but opening
        the disk in another program mid-session is losing the session.
        """
        if ev.button() != Qt.LeftButton:
            return
        i = self.index_at(ev.position().x())
        if i >= 0:
            self.chosen.emit(i)

    def leaveEvent(self, ev) -> None:
        QToolTip.hideText()

    def streak(self) -> int:
        """Consecutive rejections at the end of the series."""
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
            pnt.fillRect(int(x), 2 if m else 0, max(1, int(bw - 1)),
                         h - 4 if m else h, col)
        if self.sel is not None and self.sel < n:
            # The mark under review is distinguished by SHAPE (a full-height
            # outline), not by colour: in night mode everything is red.
            pnt.setPen(QPen(QColor(self._p.text), 1))
            pnt.setBrush(Qt.NoBrush)
            pnt.drawRect(int(self.sel * bw) - 1, 0,
                         max(3, int(bw - 1) + 2), h - 1)


class ModeRail(QWidget):
    """Mode selector: icon plus label, exclusive, in a 2x2 grid.

    A grid rather than a column because four stacked buttons cost 168 px of a
    735 px column, and that height is worth more to the active mode's panel.

    The selected button's icon is re-tinted: against an accent-coloured
    background, an icon in the normal text colour would disappear.
    """

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
        """fn(name, selected) -> QIcon"""
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
        """Show `key` as the selected mode without announcing it.

        For a mode changed in code: `select` would emit, the window would call
        back into the rail, and the two would bounce off each other.
        """
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        self.current = key
        self.refresh_icons()

    def select(self, key: str) -> None:
        self.mark(key)
        self.changed.emit(key)
