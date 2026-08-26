"""Main window — an EAA panel organised by task mode.

The interface is not a settings screen: it is a sequence of phases with
different screen needs. Choosing a target wants the ranked list and the sky's
constraints; framing wants the live frame large and the target direction;
integrating wants the stack and the frame health. Each mode rearranges the
screen for the phase you are in.

Focusing used to be a mode of its own and is not any more: the only part of it
anyone opened was the loupe, and focus is not a phase — it is something you
redo whenever the temperature drifts, in the middle of whatever you were doing.
It now floats over the image in any mode (`ui/loupe.py`), and the HFR it used to
show large is the one the vitals bar carries all night anyway.

Above all of that sits a vitals bar that never leaves the screen, because in the
dark you glance, you do not read panels.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import (
    QDateTime,
    QEvent,
    QObject,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QGuiApplication,
    QImage,
    QKeySequence,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..core import background, previews, stretch, tonight
from ..core.catalog import Catalog
from ..core.tonight import compass_point
from ..drivers.svbony import list_cameras
from ..i18n import N_, set_language
from ..i18n import available as available_languages
from ..i18n import gettext as _
from ..pointing import Pointing, brightstars, constellations, sky_vectors
from ..pointing.handset import Handset
from ..pointing.pushto import guide
from ..settings import Settings
from . import icons
from .audio import Beeper
from .branding import app_icon
from .design import (
    T_BODY,
    T_DISPLAY,
    T_H2,
    T_L,
    T_MONO,
    T_SMALL,
    T_XL,
    Card,
    ElidedLabel,
    HealthStrip,
    ModeRail,
    Palette,
    Stat,
    image_lut,
    kind_label,
    label_font,
    palette,
    stylesheet,
)
from .history import FrameHistory
from .loupe import Loupe
from .previews import PreviewLoader, PreviewView
from .skymap import Mark, SkyMap
from .targets import TargetTable
from .worker import CaptureWorker, Config

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)

#: How the program names itself to the desktop.
APP_NAME = "Astrodoro"

#: (key, label, tooltip, icon). The order is the order of the night.
MODES = [
    ("frame", N_("1  FRAME"),
     N_("find and centre the target — live frame, phone sensor and push-to"),
     "frame"),
    ("targets", N_("2  TARGETS"),
     N_("what is worth imaging at this hour, ranked — altitude, Moon, size"),
     "list"),
    ("stack", N_("3  INTEGRATE"),
     N_("stack, record and follow the platform"), "stack"),
    ("config", N_("4  CONFIG"),
     N_("folders, observing site, optics and language"), "adjust"),
]

#: Stretch presets: (target background, shadow clip).
STRETCH_PRESETS = {N_("soft"): (0.15, 3.2), N_("medium"): (0.25, 2.8),
                   N_("strong"): (0.40, 2.2)}

#: Replay speed choices: label -> multiplier (0 = as fast as possible).
REPLAY_SPEEDS = {N_("as fast as possible"): 0.0, N_("real time"): 1.0,
                 N_("4x"): 4.0, N_("10x"): 10.0}

STRICTNESS_LEVELS = (N_("lenient"), N_("normal"), N_("strict"))


class _WheelGuard(QObject):
    """Stops the mouse wheel from changing field values.

    Qt's default is that scrolling over a spinbox, combo or slider changes the
    value. In a scrollable panel that means changing exposure, gain or target
    temperature by accident just while looking for another control — and in the
    dark you do not notice.

    The event is swallowed at the field and resent to the viewport of the
    nearest scroll area, so the panel keeps scrolling normally. Without that
    resend, the field would be a dead hole in the middle of the scroll.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._resending = False

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.Wheel or self._resending:
            return False
        target = obj.parentWidget()
        while target is not None and not isinstance(target, QAbstractScrollArea):
            target = target.parentWidget()
        if target is not None:
            self._resending = True
            try:
                QApplication.sendEvent(target.viewport(), ev)
            finally:
                self._resending = False
        return True


class MainWindow(QMainWindow):
    #: Emitted when the user picks another language; `main()` rebuilds the window.
    language_changed = Signal(str)

    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.setWindowTitle("Astrodoro")
        av = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1500, int(av.width() * 0.98)),
                    min(980, int(av.height() * 0.96)))

        self.settings = settings or Settings.load()
        self.worker: CaptureWorker | None = None
        self.thread: QThread | None = None
        self.beeper = Beeper()

        self._theme = self.settings.theme
        self._night_level = int(self.settings.night_level)
        self._large_targets = bool(self.settings.large_targets)
        self.pal: Palette = palette(self._theme, self._night_level)

        self._view = "stack"
        self._live = self._stack = self._q = self._q_src = None
        # Recent frames kept for review, and what is being reviewed.
        self._hist = FrameHistory()
        self._revisit: dict | None = None
        self._black = 0.0
        self._dark_path = None
        self._flat_path = None
        self._prep = None
        self._prep_key = None
        self._prep_src = None
        self._replay_folder = ""
        self._target = None
        self._catalog: Catalog | None = None
        self._cat_missing = False
        # The phone sensor answers "where does the tube point" in this mode.
        self.point = Pointing(self.settings.latitude, self.settings.longitude,
                              self.settings.elevation_m)
        self.hs = Handset(port=self.settings.handset_port)
        self.hs.sample.connect(self._on_sample)
        self.hs.status.connect(self.on_log)
        self.hs.clients.connect(self._on_handset_clients)
        self._found = None
        self._target_name = ""
        # TARGETS: the sky the current list was computed for, when it was
        # computed, and a guard so setting the hour field does not read as the
        # user editing it.
        self._sky = None
        self._targets_t = 0.0
        self._when_guard = False
        #: Star the loupe is pinned to, or None to follow the brightest.
        self._loupe_xy: tuple[float, float] | None = None
        #: Which object the preview panel is currently waiting for.
        self._preview_want = ""
        # DSS thumbnails: fetched off the Qt thread, cached on disk.
        self.preview_loader = PreviewLoader(self)
        self.preview_loader.ready.connect(self._preview_ready)
        self.preview_loader.failed.connect(self._preview_failed)
        # Alignment suggestions, and which of them is on screen.
        self._align_picks: list = []
        self._align_i = 0
        self._align_t = 0.0
        self._completer: QCompleter | None = None
        self._marks: list = []
        self._marks_t = 0.0
        self._lines = None
        self._const_names: list = []
        self._dsos: list | None = None
        self._iconed: list[tuple] = []
        self._paused = False
        self._cool: dict = {}
        self._state = "idle"
        self._t_frame = 0.0
        self._exposure = self.settings.exposure_s
        self._last_stats: dict = {}

        self._build()
        self._block_wheel()
        self._shortcuts()
        self.refresh_cameras()
        self._restore()
        self.apply_theme()
        self.set_mode("frame")

        self.tick = QTimer(self)
        self.tick.timeout.connect(self._on_tick)
        self.tick.start(120)

        # The sensor has its own loop: samples arrive at 20 Hz and the push-to
        # screen has to keep up with the hand pushing the tube. The window's
        # 120 ms tick belongs to the frame, which takes seconds.
        self.tick_sensor = QTimer(self)
        self.tick_sensor.timeout.connect(self._sensor_tick)
        self.tick_sensor.start(100)

    # ------------------------------------------------------------------ optics
    def pixel_scale(self, halved: bool = False) -> float:
        """Arcsec per pixel at the currently selected binning."""
        return self.settings.pixel_scale(int(self.cb_bin.currentText()),
                                         halved=halved)

    # ================================================================= layout
    def _build(self) -> None:
        root = QWidget()
        # The window background is declared here, not on the generic QWidget
        # selector.
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 6)
        outer.setSpacing(8)

        outer.addWidget(self._status_bar())
        self.alert = QLabel("")
        self.alert.setFont(T_H2())
        self.alert.setVisible(False)
        self.alert.setWordWrap(True)
        outer.addWidget(self.alert)

        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self._left())
        self.split.addWidget(self._right())
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([368, max(self.width() - 368, 520)])
        outer.addWidget(self.split, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(T_MONO())
        self.log.setMaximumHeight(120)
        outer.addWidget(self.log)
        # The log starts visible: almost every message here (camera gone, frame
        # rejected) is the only clue about what went wrong, and in the dark
        # nobody remembers to press L before needing it.
        self.btn_log.setChecked(True)
        self.setCentralWidget(root)

    # ------------------------------------------------------------- status bar
    def _status_bar(self) -> QWidget:
        """Vital signs. Every item has the same shape — small label on top,
        large value underneath — the exposure bar included, so the row reads as
        a row and not as a collage of boxes."""
        card = Card()
        top = QHBoxLayout()
        top.setSpacing(22)
        top.setContentsMargins(2, 0, 2, 0)

        self.st_state = Stat(_("state"), "● " + _("IDLE"), big=True,
                             min_width=168)
        self.st_target = Stat(_("target"), "—", big=True, min_width=150)
        self.st_integ = Stat(_("integration"), "—", big=True)
        self.st_frames = Stat(_("frames"), "—", big=True)
        self.st_hfr = Stat(_("HFR"), "—", big=True, min_width=88)

        expo = QWidget()
        ev = QVBoxLayout(expo)
        ev.setContentsMargins(0, 1, 0, 1)
        ev.setSpacing(3)
        # Elided in the middle: this text changes on every frame — "EXPOSURE"
        # (8 chars) becomes "READING AND PROCESSING  +7.9s" (29) — and as a
        # plain label that took the window's minimum width from 1271 px to
        # 1364 px and back, so the window grew by itself between frames. Middle
        # rather than right because the seconds at the end are the point.
        self.phase = ElidedLabel(_("EXPOSURE"), mode=Qt.ElideMiddle,
                                 min_chars=6)
        self.phase.setObjectName("statLabel")
        self.phase.setFont(label_font())
        self.prog = QProgressBar()
        self.prog.setTextVisible(False)
        self.prog.setFixedHeight(10)
        self.prog.setMinimumWidth(150)
        ev.addWidget(self.phase)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.prog)
        box = QWidget()
        box.setLayout(row)
        box.setMinimumHeight(T_XL().pointSize() + 12)
        ev.addWidget(box)
        ev.addStretch(1)

        for st in (self.st_state, self.st_target, self.st_integ,
                   self.st_frames, self.st_hfr):
            top.addWidget(st)
        top.addWidget(expo)
        top.addStretch(1)

        # Buttons aligned with the row of values, not with the row of labels.
        btns = QWidget()
        bv = QVBoxLayout(btns)
        bv.setContentsMargins(0, 1, 0, 1)
        bv.setSpacing(3)
        spacer = QLabel(" ")
        spacer.setFont(label_font())
        bv.addWidget(spacer)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.btn_start = QPushButton(_("Start"))
        self._ic(self.btn_start, "play")
        self.btn_start.setToolTip(_(
            "Opens the camera. What happens to the frames depends on the mode:\n"
            "Frame and Focus only capture and measure stars; Integrate stacks,\n"
            "records the subs and measures the residual rotation."))
        self.btn_start.clicked.connect(self.start)

        self.btn_pause = QPushButton(_("Pause"))
        self._ic(self.btn_pause, "pause")
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip(_(
            "Stops pulling frames without closing the camera.\n"
            "The stack and the recording stay intact — Resume continues on the\n"
            "same accumulator."))
        self.btn_pause.clicked.connect(self.toggle_pause)

        self.btn_finish = QPushButton(_("Finish"))
        self._ic(self.btn_finish, "stop")
        self.btn_finish.setEnabled(False)
        self.btn_finish.setToolTip(_(
            "Ends the session and closes the camera.\n"
            "The final stack is always written to disk before finishing."))
        self.btn_finish.clicked.connect(self.finish)

        # Reset lives here, not on the dark and flat card: it has nothing to do
        # with calibration — it discards the integration — and next to those
        # buttons it looked like it would clear the loaded dark.
        self.btn_reset = QPushButton(_("Reset"))
        self._ic(self.btn_reset, "trash")
        self.btn_reset.setToolTip(_("Discards the accumulated integration. Subs "
                                    "already written to disk are not deleted."))
        self.btn_reset.clicked.connect(self.reset_stack)

        self._session_buttons = (self.btn_start, self.btn_pause,
                                 self.btn_finish, self.btn_reset)
        for b in self._session_buttons:
            # On macOS Qt promotes the window's first button to "default" and
            # paints it with the native style, ignoring part of the stylesheet.
            b.setAutoDefault(False)
            b.setDefault(False)
            row.addWidget(b)
        bv.addLayout(row)
        bv.addStretch(1)
        top.addWidget(btns)

        card.add_layout(top)
        self.health = HealthStrip()
        # No blanket tooltip: each mark already shows its own frame's detail on
        # hover, and fixed text only covered the strip.
        self.health.chosen.connect(self._show_frame)
        self.health.has_image = self._hist.__contains__
        card.add(self.health)
        return card

    # ------------------------------------------------------------------- left
    def _left(self) -> QWidget:
        col = QWidget()
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        self.rail = ModeRail([(k, _(lab), _(hint), ic)
                              for k, lab, hint, ic in MODES])
        self.rail.changed.connect(self.set_mode)
        v.addWidget(self.rail)

        self.panels = QStackedWidget()
        self._panel_index = {}
        for key, build in (("frame", self._panel_frame),
                           ("targets", self._panel_targets),
                           ("stack", self._panel_stack),
                           ("config", self._panel_config)):
            sa = QScrollArea()
            sa.setWidget(build())
            sa.setWidgetResizable(True)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            sa.setFrameShape(QScrollArea.NoFrame)
            self._panel_index[key] = self.panels.addWidget(sa)
        v.addWidget(self.panels, 1)

        v.addWidget(self._capture_strip())
        col.setMinimumWidth(348)
        col.setMaximumWidth(452)
        self._left_col = col
        return col

    def _capture_strip(self) -> QWidget:
        """Always visible, in every mode: the four controls you touch all night.
        Two rows instead of four — the column's height is the scarce resource."""
        c = Card(_("capture"))
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        self.sp_exp = QDoubleSpinBox()
        self.sp_exp.setRange(0.001, 600.0)
        self.sp_exp.setDecimals(3)
        self.sp_exp.setValue(self.settings.exposure_s)
        self.sp_exp.setSuffix(" s")
        self.sp_exp.valueChanged.connect(self._exposure_changed)
        self.sp_gain = QSpinBox()
        self.sp_gain.setRange(0, 570)
        self.sp_gain.setValue(self.settings.gain)
        self.sp_gain.valueChanged.connect(lambda x: self._req(gain=x))
        r1.addWidget(_tag(_("exp")))
        r1.addWidget(self.sp_exp, 1)
        r1.addWidget(_tag(_("gain")))
        r1.addWidget(self.sp_gain, 1)
        c.add_layout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.sp_offset = QSpinBox()
        self.sp_offset.setRange(0, 80)
        self.sp_offset.setValue(self.settings.offset)
        self.sp_offset.setToolTip(
            _("offset 0 truncates the left tail of the noise"))
        self.sp_offset.valueChanged.connect(lambda x: self._req(offset=x))
        self.cb_bin = QComboBox()
        self.cb_bin.addItems(["1", "2", "3", "4"])
        self.cb_bin.setCurrentText(str(self.settings.binning))
        self.cb_bin.setToolTip(
            _("bin2 is the only bin >1 without loss on this camera;\n"
              "bin3 and bin4 clip the highlights"))
        self.cb_bin.currentTextChanged.connect(self._bin_changed)
        r2.addWidget(_tag(_("offset")))
        r2.addWidget(self.sp_offset, 1)
        r2.addWidget(_tag(_("bin")))
        r2.addWidget(self.cb_bin, 1)
        c.add_layout(r2)

        # Cooling lives here, always visible, rather than in a mode panel:
        # cooling takes fifteen to twenty minutes, and you want to switch it on
        # while still framing so it is stable by the time you integrate.
        r3 = QHBoxLayout()
        r3.setSpacing(6)
        self.btn_cooler = QPushButton(_("Cooler"))
        self.btn_cooler.setCheckable(True)
        self.btn_cooler.setMaximumWidth(88)
        self.btn_cooler.setToolTip(_(
            "Switches cooling on with a controlled ramp.\n"
            "Sending the final target in one go makes the TEC pull 100% and the\n"
            "temperature plunge, which stresses the sensor joint and encourages\n"
            "condensation."))
        self.btn_cooler.toggled.connect(self._cooler_toggled)
        self.sp_temp = QDoubleSpinBox()
        self.sp_temp.setRange(-40, 30)
        # Always opens at -5 C, and the field is deliberately not persisted: a
        # value inherited from last night would make the TEC chase a temperature
        # that may be unreachable today.
        self.sp_temp.setValue(-5)
        self.sp_temp.setSuffix(" °C")
        self.sp_temp.valueChanged.connect(lambda x: self._req(target_temp=x))
        r3.addWidget(self.btn_cooler)
        r3.addWidget(_tag(_("target")))
        r3.addWidget(self.sp_temp, 1)
        c.add_layout(r3)
        self.lbl_cool = QLabel(_("cooler off"))
        self.lbl_cool.setFont(T_MONO())
        self.lbl_cool.setWordWrap(True)
        c.add(self.lbl_cool)
        return c

    # --------------------------------------------------------- panel: FRAME
    def _panel_frame(self) -> QWidget:
        """The FRAME panel, in the order the night happens: switch the sensor
        on, align, then the camera. Choosing *what* to point at happens one mode
        earlier, in TARGETS, which is also where the search box lives.

        The order matters more than it seems: the column scrolls, and whatever
        falls below the fold does not exist for someone in the dark with a hand
        on the tube.
        """
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        c = Card(_("1 · sensor on the tube"))
        self.btn_sensor = QPushButton(_("Switch the sensor on"))
        self._ic(self.btn_sensor, "target")
        self.btn_sensor.setCheckable(True)
        self.btn_sensor.toggled.connect(self.toggle_sensor)
        c.add(self.btn_sensor)
        self.lbl_qr = QLabel()
        self.lbl_qr.setAlignment(Qt.AlignCenter)
        self.lbl_qr.setVisible(False)
        c.add(self.lbl_qr)
        self.lbl_url = QLabel("")
        self.lbl_url.setFont(T_MONO())
        self.lbl_url.setWordWrap(True)
        self.lbl_url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_url.setVisible(False)
        c.add(self.lbl_url)
        self.lbl_sensor = QLabel(_("off"))
        self.lbl_sensor.setFont(T_MONO())
        self.lbl_sensor.setWordWrap(True)
        c.add(self.lbl_sensor)
        self.lbl_altaz = QLabel("—")
        self.lbl_altaz.setFont(T_L())
        c.add(self.lbl_altaz)
        # Which star to align on is a question with a computable answer, and
        # hunting one in a list of 179 names in the dark is what makes people
        # give up. The criteria are in `brightstars.for_alignment`.
        c.add(_tag(_("star to align on")))
        self.lbl_align_pick = QLabel("—")
        self.lbl_align_pick.setFont(T_MONO())
        self.lbl_align_pick.setWordWrap(True)
        self.lbl_align_pick.setToolTip(_(
            "The star worth aligning on right now: bright, unmistakable "
            "(no similar star beside it), comfortable to reach, and as close to "
            "the target as possible — one star corrects two axes and is most "
            "accurate around itself."))
        c.add(self.lbl_align_pick)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_align_pick = QPushButton(_("aim at it and align"))
        self._ic(self.btn_align_pick, "star")
        self.btn_align_pick.setToolTip(_("point the tube at this star first — "
                                         "the alignment reads the sensor now"))
        self.btn_align_pick.clicked.connect(self._align_on_pick)
        self.btn_align_next = QPushButton("↻")
        self.btn_align_next.setMaximumWidth(38)
        self.btn_align_next.setToolTip(_("another star — this one may be behind "
                                         "a tree"))
        self.btn_align_next.clicked.connect(self._align_next_pick)
        self.btn_align_map = QPushButton("☆")
        self.btn_align_map.setMaximumWidth(38)
        self.btn_align_map.setToolTip(_("show it on the sky map"))
        self.btn_align_map.clicked.connect(self._align_pick_on_map)
        row.addWidget(self.btn_align_pick, 1)
        row.addWidget(self.btn_align_next)
        row.addWidget(self.btn_align_map)
        c.add_layout(row)

        b = QPushButton(_("open the map and align   (M)"))
        self._ic(b, "target")
        b.clicked.connect(lambda: self._set_view("map"))
        c.add(b)
        self.btn_reset_align = QPushButton(_("clear the alignment"))
        self._ic(self.btn_reset_align, "refresh")
        self.btn_reset_align.clicked.connect(self.reset_align)
        self.btn_reset_align.setEnabled(False)
        c.add(self.btn_reset_align)
        v.addWidget(c)

        c = Card(_("2 · camera"))
        row = QHBoxLayout()
        self.cb_source = QComboBox()
        self.cb_source.addItems([_("camera"), _("replay")])
        self.cb_source.currentIndexChanged.connect(self._source_changed)
        row.addWidget(self.cb_source)
        self.cb_camera = QComboBox()
        # Camera names are long ("SVBONY SV405CC") and QComboBox asks for the
        # width of the whole item: on its own it demanded 200 px and pushed the
        # column past what it has, creating a horizontal scrollbar.
        self.cb_camera.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.cb_camera.setMinimumContentsLength(8)
        row.addWidget(self.cb_camera, 1)
        self.btn_refresh = QPushButton()
        self.btn_refresh.setMaximumWidth(38)
        self.btn_refresh.setToolTip(_("look for cameras again"))
        self._ic(self.btn_refresh, "refresh")
        self.btn_refresh.clicked.connect(self.refresh_cameras)
        row.addWidget(self.btn_refresh)
        self.btn_folder = QPushButton(_("folder…"))
        self._ic(self.btn_folder, "folder")
        self.btn_folder.clicked.connect(self.pick_replay)
        self.btn_folder.setVisible(False)
        row.addWidget(self.btn_folder)
        c.add_layout(row)

        # Replay speed: without it a replay runs as fast as it can and a whole
        # session goes by in seconds — before you reach Integrate there is no
        # frame left to stack.
        self.rep_row = QWidget()
        rr = QHBoxLayout(self.rep_row)
        rr.setContentsMargins(0, 0, 0, 0)
        self.cb_speed = QComboBox()
        for name in REPLAY_SPEEDS:
            self.cb_speed.addItem(_(name), name)
        self.cb_speed.setCurrentIndex(2)          # 4x
        self.chk_loop = QCheckBox(_("loop"))
        rr.addWidget(QLabel(_("speed")))
        rr.addWidget(self.cb_speed, 1)
        rr.addWidget(self.chk_loop)
        self.rep_row.setVisible(False)
        c.add(self.rep_row)
        row = QHBoxLayout()
        for s in (0.2, 0.5, 1.0, 2.0):
            b = QPushButton(f"{s:g}s")
            b.clicked.connect(lambda _checked=False, x=s: self.sp_exp.setValue(x))
            row.addWidget(b)
        c.add_layout(row)
        v.addWidget(c)

        v.addStretch(1)
        return w

    # ------------------------------------------------------- panel: TARGETS
    def _panel_targets(self) -> QWidget:
        """Filters on the left, the ranked list in the place of the image.

        The hour is a field and not just "now" because the decision is almost
        always made before the sky is ready: you are setting up at dusk and what
        you want to know is what will be well placed at eleven, not what is well
        placed while you are still carrying the tube outside.
        """
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        c = Card(_("1 · when"))
        row = QHBoxLayout()
        self.dt_when = QDateTimeEdit(QDateTime.currentDateTime())
        self.dt_when.setDisplayFormat("dd/MM  HH:mm")
        self.dt_when.setCalendarPopup(True)
        self.dt_when.dateTimeChanged.connect(self._when_edited)
        self.btn_now = QPushButton(_("now"))
        self.btn_now.setCheckable(True)
        self.btn_now.setChecked(True)
        self.btn_now.setMaximumWidth(70)
        self.btn_now.setToolTip(_("follow the clock — untick to plan another "
                                  "hour"))
        self.btn_now.toggled.connect(self._now_toggled)
        row.addWidget(self.dt_when, 1)
        row.addWidget(self.btn_now)
        c.add_layout(row)
        row = QHBoxLayout()
        row.setSpacing(4)
        for label, hours in ((_("−1h"), -1.0), (_("+1h"), 1.0),
                             (_("+2h"), 2.0), (_("+4h"), 4.0)):
            b = QPushButton(label)
            b.clicked.connect(lambda _checked=False, h=hours: self._shift_when(h))
            row.addWidget(b)
        c.add_layout(row)
        self.lbl_sky = QLabel("—")
        self.lbl_sky.setFont(T_MONO())
        self.lbl_sky.setWordWrap(True)
        self.lbl_sky.setToolTip(_("the two things that limit every target "
                                  "tonight: how dark it is and where the Moon "
                                  "is"))
        c.add(self.lbl_sky)
        v.addWidget(c)

        c = Card(_("2 · filters"))
        self.cb_family = QComboBox()
        for key, (label, _types) in tonight.FAMILIES.items():
            self.cb_family.addItem(_(label), key)
        i = self.cb_family.findData(self.settings.target_family)
        self.cb_family.setCurrentIndex(max(i, 0))
        self.cb_family.currentIndexChanged.connect(self._refresh_targets)
        c.field(_("type"), self.cb_family)

        self.sp_min_alt = QDoubleSpinBox()
        self.sp_min_alt.setRange(0.0, 85.0)
        self.sp_min_alt.setDecimals(0)
        self.sp_min_alt.setSuffix("°")
        self.sp_min_alt.setValue(self.settings.target_min_alt)
        self.sp_min_alt.valueChanged.connect(self._refresh_targets)
        c.field(_("minimum altitude"), self.sp_min_alt,
                _("what your horizon actually clears — trees, the neighbour's "
                  "wall, the worst of the light dome"))

        self.sp_max_mag = QDoubleSpinBox()
        self.sp_max_mag.setRange(3.0, 16.0)
        self.sp_max_mag.setDecimals(1)
        self.sp_max_mag.setSingleStep(0.5)
        self.sp_max_mag.setValue(self.settings.target_max_mag)
        self.sp_max_mag.valueChanged.connect(self._refresh_targets)
        c.field(_("faintest magnitude"), self.sp_max_mag)

        self.chk_fits = QCheckBox(_("only what fits in the frame"))
        self.chk_fits.setChecked(self.settings.target_fits_only)
        self.chk_fits.toggled.connect(self._refresh_targets)
        c.add(self.chk_fits)

        self.chk_previews = QCheckBox(_("show a photo of the object"))
        self.chk_previews.setChecked(self.settings.previews_enabled)
        self.chk_previews.setToolTip(_(
            "DSS survey images, downloaded once and kept on disk. Untick to "
            "stop the program touching the network at all."))
        self.chk_previews.toggled.connect(self._previews_toggled)
        c.add(self.chk_previews)
        self.btn_prefetch = QPushButton(_("cache the photos of this list"))
        self._ic(self.btn_prefetch, "save")
        self.btn_prefetch.setToolTip(_(
            "Fetch every suggestion's picture now, while there is internet — "
            "in the field there is none, and only the cache answers."))
        self.btn_prefetch.clicked.connect(self._prefetch_previews)
        c.add(self.btn_prefetch)
        self.lbl_fov = QLabel("")
        self.lbl_fov.setFont(T_SMALL())
        self.lbl_fov.setObjectName("statLabel")
        c.add(self.lbl_fov)
        v.addWidget(c)

        c = Card(_("3 · target"))
        row = QHBoxLayout()
        self.ed_goto = QLineEdit()
        self.ed_goto.setPlaceholderText("M8, NGC 5128, Antares…")
        self.ed_goto.returnPressed.connect(self.set_goto)
        b = QPushButton(_("go"))
        b.setMaximumWidth(62)
        self._ic(b, "arrow")
        b.clicked.connect(self.set_goto)
        self.btn_clear_target = QPushButton("✕")
        self.btn_clear_target.setMaximumWidth(38)
        self.btn_clear_target.setToolTip(_("forget the target"))
        self.btn_clear_target.clicked.connect(self.clear_target)
        self.btn_clear_target.setEnabled(False)
        row.addWidget(self.ed_goto, 1)
        row.addWidget(b)
        row.addWidget(self.btn_clear_target)
        c.add_layout(row)
        self.lbl_goto = QLabel(_("no target"))
        self.lbl_goto.setWordWrap(True)
        self.lbl_goto.setFont(T_MONO())
        c.add(self.lbl_goto)
        c.add(_hint(_("a name you already know; the list is for the rest.")))
        v.addWidget(c)

        v.addStretch(1)
        return w

    # ----------------------------------------------------- panel: INTEGRATE
    def _panel_stack(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        c = Card(_("integration"))
        self.btn_integrate = QPushButton(_("Start integrating"))
        self.btn_integrate.setCheckable(True)
        self.btn_integrate.setMinimumHeight(38)
        self.btn_integrate.setToolTip(_(
            "Starts stacking, recording the subs and measuring the platform.\n"
            "Switching modes does not trigger this on its own: coming in here\n"
            "only shows the panel. Writing to disk and consuming platform travel\n"
            "is your decision, not a side effect of clicking a tab."))
        self._ic(self.btn_integrate, "play")
        self.btn_integrate.toggled.connect(self._integrate_toggled)
        c.add(self.btn_integrate)
        v.addWidget(c)

        # Ordered by how often you touch it during the hours a stack is growing,
        # not by the order of a checklist. The column scrolls, and what falls
        # below the fold does not exist for someone in the dark with a hand on
        # the tube — so the display controls come before the pre-flight ones,
        # which are set once and cost a single scroll at the start of a session.
        v.addWidget(self._card_stretch())
        v.addWidget(self._card_channel_gain())
        v.addWidget(self._card_gradient())

        c = Card(_("stacking"))
        self.chk_weight = QCheckBox(_("give better frames more weight"))
        self.chk_weight.setChecked(True)
        self.chk_weight.setToolTip(_(
            "Weights each frame by flux/(noise²·FWHM²). It matters alongside\n"
            "vote-based registration, which lets marginal frames in: the weight\n"
            "keeps them from dragging the stack down."))
        c.add(self.chk_weight)
        self.sp_sigma = QDoubleSpinBox()
        self.sp_sigma.setRange(0.0, 6.0)
        self.sp_sigma.setValue(3.0)
        self.sp_sigma.setSingleStep(0.5)
        self.sp_sigma.setSuffix(" σ")
        self.sp_sigma.valueChanged.connect(
            lambda x: self._req(sigma_clip=(x if x > 0 else None)))
        c.field(_("outlier rejection"), self.sp_sigma,
                _("removes satellites and aircraft from the stack. 0 disables it"))
        self.cb_strictness = QComboBox()
        for level in STRICTNESS_LEVELS:
            self.cb_strictness.addItem(_(level), level)
        self.cb_strictness.setCurrentIndex(1)
        self.cb_strictness.setToolTip(_(
            "How much a frame has to be worth, compared to what tonight is\n"
            "delivering, to enter the stack. Lenient uses more frames and gains\n"
            "signal; strict uses fewer and gains sharpness."))
        self.cb_strictness.currentIndexChanged.connect(
            lambda i: self._req(strictness=self.cb_strictness.itemData(i)))
        c.field(_("accept frames"), self.cb_strictness,
                _("how much to demand of each frame for it to be used"))
        self.btn_segment = QPushButton(_("New segment   (space)"))
        self._ic(self.btn_segment, "segment")
        self.btn_segment.setToolTip(_(
            "After resetting the platform or recentring the target: the stack\n"
            "continues, the thresholds relax for a few frames and the reference\n"
            "is re-extracted."))
        self.btn_segment.clicked.connect(lambda: self._flag("new_segment"))
        c.add(self.btn_segment)
        v.addWidget(c)

        c = Card(_("target and recording"))
        self.chk_record = QCheckBox(_("record raw subs"))
        self.chk_record.setChecked(True)
        c.add(self.chk_record)
        row = QHBoxLayout()
        self.chk_compress = QCheckBox("RICE")
        self.chk_compress.setChecked(True)
        self.chk_compress.setToolTip(_("lossless compression, typically 2x"))
        self.sp_every = QSpinBox()
        self.sp_every.setRange(1, 20)
        self.sp_every.setValue(1)
        self.sp_every.setToolTip(_("writes 1 in every N subs"))
        self.sp_every.valueChanged.connect(lambda x: self._req(record_every=x))
        row.addWidget(self.chk_compress)
        row.addWidget(QLabel(_("1 in every")))
        row.addWidget(self.sp_every)
        c.add_layout(row)
        # Starts hidden: until recording begins it only showed a dash.
        self.lbl_disk = QLabel("")
        self.lbl_disk.setFont(T_MONO())
        self.lbl_disk.setWordWrap(True)
        self.lbl_disk.setVisible(False)
        c.add(self.lbl_disk)
        v.addWidget(c)

        # Two cards, not one called "stack": calibration is what you load from
        # outside (dark, flat) and set once a night; stacking is how frames
        # enter the accumulator, and is adjusted during the session.
        c = Card(_("calibration"))
        row = QHBoxLayout()
        self.btn_dark = QPushButton(_("Load dark"))
        self._ic(self.btn_dark, "dark")
        self.btn_dark.clicked.connect(self.pick_dark)
        self.btn_capture_dark = QPushButton(_("Record dark"))
        self._ic(self.btn_capture_dark, "dark")
        self.btn_capture_dark.setToolTip(_(
            "Records a master dark with the exposure, gain, offset, bin and\n"
            "temperature of the session in progress — which is exactly what a\n"
            "dark has to match. Cap the sensor first."))
        self.btn_capture_dark.clicked.connect(self.capture_dark)
        row.addWidget(self.btn_dark)
        row.addWidget(self.btn_capture_dark)
        c.add_layout(row)
        self.lbl_dark = QLabel(_("dark: none"))
        self.lbl_dark.setFont(T_MONO())
        self.lbl_dark.setWordWrap(True)
        c.add(self.lbl_dark)
        row = QHBoxLayout()
        self.btn_flat = QPushButton(_("Load flat"))
        self._ic(self.btn_flat, "grid")
        self.btn_flat.setToolTip(_(
            "Corrects vignetting and dust; without one the corners go dark and\n"
            "the autostretch gives it away."))
        self.btn_flat.clicked.connect(self.pick_flat)
        self.btn_capture_flat = QPushButton(_("Record flat"))
        self._ic(self.btn_capture_flat, "grid")
        self.btn_capture_flat.setToolTip(_(
            "Records a master flat with the gain and bin of the session in\n"
            "progress. Point at an evenly illuminated surface — twilight sky, a\n"
            "flat panel, a stretched white shirt — and set the exposure so the\n"
            "histogram lands near half scale."))
        self.btn_capture_flat.clicked.connect(self.capture_flat)
        row.addWidget(self.btn_flat)
        row.addWidget(self.btn_capture_flat)
        c.add_layout(row)
        self.lbl_flat = QLabel(_("flat: none"))
        self.lbl_flat.setFont(T_MONO())
        self.lbl_flat.setWordWrap(True)
        c.add(self.lbl_flat)
        v.addWidget(c)

        c = Card(_("equatorial platform"))
        self.lbl_advice = QLabel("—")
        self.lbl_advice.setWordWrap(True)
        self.lbl_advice.setFont(T_MONO())
        c.add(self.lbl_advice)
        v.addWidget(c)
        v.addStretch(1)
        return w

    # ------------------------------------------------- image cards (INTEGRATE)
    # These live in INTEGRATE because they are what you touch for the hours the
    # stack is growing. They used to sit in a panel of their own alongside the
    # program's fixed settings, which mixed "adjust the image now" with
    # "configure the program once".
    def _card_stretch(self) -> Card:
        c = Card(_("stretch"))
        row = QHBoxLayout()
        self.cb_algo = QComboBox()
        self.cb_algo.addItems([_("MTF (default)"), _("arcsinh")])
        self.cb_algo.setToolTip(_(
            "MTF is the default, the same family as PixInsight and SharpCap.\n"
            "arcsinh is gentler on the highlights and preserves the colour of a\n"
            "bright star's core, of a globular and of M42 — where the MTF blows\n"
            "out to white."))
        self.cb_algo.currentIndexChanged.connect(lambda: self._render(True))
        row.addWidget(QLabel(_("algorithm")))
        row.addWidget(self.cb_algo, 1)
        c.add_layout(row)
        row = QHBoxLayout()
        for name in STRETCH_PRESETS:
            b = QPushButton(_(name))
            b.clicked.connect(lambda _checked=False, n=name: self.apply_preset(n))
            row.addWidget(b)
        c.add_layout(row)
        self.chk_auto = QCheckBox(_("autostretch"))
        self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(lambda: self._render(True))
        c.add(self.chk_auto)
        self.sl_bg, wbg = self._slider(_("background"), 5, 60, 25, 100.0)
        self.sl_clip, wcl = self._slider(_("shadows"), 10, 60, 28, 10.0)
        c.add(wbg)
        c.add(wcl)
        self.chk_linked = QCheckBox(_("linked across channels"))
        self.chk_linked.setToolTip(_(
            "Off: each channel separately, which neutralises the background —\n"
            "better under light pollution. On: preserves the colour ratios."))
        self.chk_linked.toggled.connect(lambda: self._render(True))
        c.add(self.chk_linked)
        self.sl_white, wwh = self._slider(_("whites"), 20, 100, 100, 100.0)
        self.sl_white.valueChanged.connect(lambda: self._render(True))
        self.sl_white.setToolTip(_(
            "White point: the value that comes out as 255. Lowering it saturates\n"
            "the highlights on purpose — that is how faint nebulosity is brought\n"
            "up without waiting for the core to behave. 1.00 uses the full scale."))
        c.add(wwh)
        self.sl_sat, wsat = self._slider(_("saturation"), 0, 25, 10, 10.0)
        self.sl_sat.setToolTip(_(
            "Stretching compresses the distance between channels and the nebula\n"
            "goes pale; this gives the colour back without touching brightness."))
        c.add(wsat)
        return c

    def _card_channel_gain(self) -> Card:
        c = Card(_("per-channel gain"))
        self.sl_r, wr = self._slider(_("red"), 30, 300, 100, 100.0)
        self.sl_g, wg = self._slider(_("green"), 30, 300, 100, 100.0)
        self.sl_b, wb = self._slider(_("blue"), 30, 300, 100, 100.0)
        for sl in (self.sl_r, self.sl_g, self.sl_b):
            sl.valueChanged.connect(lambda: self._render(True))
        for wid in (wr, wg, wb):
            c.add(wid)
        row = QHBoxLayout()
        b = QPushButton(_("equalise background"))
        b.setToolTip(_(
            "Matches the median of the three channels, taking green as the\n"
            "reference — a white balance measured on the sky, which is the grey\n"
            "surface always in the frame."))
        b.clicked.connect(self.equalize_channels)
        row.addWidget(b)
        b = QPushButton(_("neutral"))
        b.setToolTip(_("returns the three gains to 1.00"))
        b.clicked.connect(lambda: [sl.setValue(100)
                                   for sl in (self.sl_r, self.sl_g, self.sl_b)])
        row.addWidget(b)
        c.add_layout(row)
        c.add(_hint(_("multiplies each channel before the stretch, like a white "
                      "balance. Applies with the stretch linked across channels; "
                      "unlinked, each channel is already normalised on its own")))
        return c

    def _card_gradient(self) -> Card:
        c = Card(_("background gradient"))
        self.chk_bg = QCheckBox(_("remove gradient"))
        self.chk_bg.setToolTip(_(
            "Fits a smooth surface to the background and subtracts it. Corrects\n"
            "light pollution and the residual amp glow left over from the dark —\n"
            "which is exactly what the autostretch amplifies best.\n"
            "Affects the display only; the accumulator is not modified."))
        self.chk_bg.toggled.connect(lambda: self._render(True))
        c.add(self.chk_bg)
        row = QHBoxLayout()
        self.sp_bg_deg = QSpinBox()
        self.sp_bg_deg.setRange(1, 2)
        self.sp_bg_deg.setValue(2)
        self.sp_bg_deg.setToolTip(_(
            "1 corrects tilt; 2 catches amp glow, which is a corner blob.\n"
            "A higher degree starts eating extended nebulosity."))
        self.sp_bg_deg.valueChanged.connect(lambda: self._render(True))
        row.addWidget(QLabel(_("degree")))
        row.addWidget(self.sp_bg_deg)
        self.lbl_bg = QLabel("")
        self.lbl_bg.setFont(T_MONO())
        row.addWidget(self.lbl_bg, 1)
        c.add_layout(row)
        return c

    # -------------------------------------------------------- panel: CONFIG
    def _panel_config(self) -> QWidget:
        """What you set once — where files go, where you are, what optics, which
        language — as opposed to what you touch all night.

        Deliberately short: no scrolling, so nothing falls below the fold.
        """
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(self._card_folders())
        v.addWidget(self._card_site())
        v.addWidget(self._card_display())
        v.addStretch(1)
        return w

    def _card_folders(self) -> Card:
        """Where captures go. Defaults live under ~/Astrodoro so a fresh clone
        never writes session data into the repository."""
        c = Card(_("folders"))
        self._dir_labels = {}
        for key, label, hint in (
                ("capture_dir", _("sessions"),
                 _("root of the recorded sessions: subs, stacks and previews")),
                ("dark_dir", _("darks"), _("where master darks are written")),
                ("flat_dir", _("flats"), _("where master flats are written")),
                ("export_dir", _("exports"),
                 _("images saved outside a recording session"))):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name = QLabel(label)
            name.setFont(T_BODY())
            name.setMinimumWidth(72)
            name.setToolTip(hint)
            value = QLabel(_shorten(getattr(self.settings, key)))
            value.setFont(T_MONO())
            value.setToolTip(getattr(self.settings, key))
            btn = QPushButton("…")
            btn.setMaximumWidth(38)
            btn.clicked.connect(lambda _checked=False, k=key: self.pick_dir(k))
            row.addWidget(name)
            row.addWidget(value, 1)
            row.addWidget(btn)
            c.add_layout(row)
            self._dir_labels[key] = value
        b = QPushButton(_("open the sessions folder"))
        self._ic(b, "folder")
        b.clicked.connect(lambda: self._reveal(self.settings.path("capture_dir",
                                                                 create=True)))
        c.add(b)
        c.add(_hint(_("changes apply to the next session; the one in progress "
                      "keeps writing where it started")))
        return c

    def _card_site(self) -> Card:
        c = Card(_("observing site and optics"))
        self.sp_lat = QDoubleSpinBox()
        self.sp_lat.setRange(-90, 90)
        self.sp_lat.setDecimals(4)
        self.sp_lat.setValue(self.settings.latitude)
        self.sp_lat.setSuffix("°")
        self.sp_lon = QDoubleSpinBox()
        self.sp_lon.setRange(-180, 180)
        self.sp_lon.setDecimals(4)
        self.sp_lon.setValue(self.settings.longitude)
        self.sp_lon.setSuffix("°")
        self.sp_elev = QDoubleSpinBox()
        self.sp_elev.setRange(-500, 6000)
        self.sp_elev.setDecimals(0)
        self.sp_elev.setValue(self.settings.elevation_m)
        self.sp_elev.setSuffix(" m")
        c.field(_("latitude"), self.sp_lat,
                _("latitude feeds straight into the altitude of the pole: 0.1° "
                  "of error becomes 6' of alignment error"))
        c.field(_("longitude"), self.sp_lon)
        c.field(_("elevation"), self.sp_elev)
        self.sp_focal = QDoubleSpinBox()
        self.sp_focal.setRange(50, 10000)
        self.sp_focal.setDecimals(0)
        self.sp_focal.setValue(self.settings.focal_length_mm)
        self.sp_focal.setSuffix(" mm")
        self.sp_focal.valueChanged.connect(self._optics_changed)
        self.sp_pixel = QDoubleSpinBox()
        self.sp_pixel.setRange(0.5, 30.0)
        self.sp_pixel.setDecimals(2)
        self.sp_pixel.setValue(self.settings.pixel_size_um)
        self.sp_pixel.setSuffix(" µm")
        self.sp_pixel.valueChanged.connect(self._optics_changed)
        c.field(_("focal length"), self.sp_focal)
        c.field(_("pixel"), self.sp_pixel)
        self.lbl_scale = QLabel("")
        self.lbl_scale.setFont(T_MONO())
        c.add(self.lbl_scale)
        return c

    def _card_display(self) -> Card:
        c = Card(_("display"))
        row = QHBoxLayout()
        self.cb_lang = QComboBox()
        for tag, name in available_languages().items():
            self.cb_lang.addItem(name, tag)
        idx = self.cb_lang.findData(self.settings.language)
        if idx >= 0:
            self.cb_lang.setCurrentIndex(idx)
        self.cb_lang.currentIndexChanged.connect(self._language_changed)
        row.addWidget(QLabel(_("language")))
        row.addWidget(self.cb_lang, 1)
        c.add_layout(row)

        self.btn_night = QPushButton(_("night mode   (N)"))
        self._ic(self.btn_night, "moon")
        self.btn_night.setCheckable(True)
        self.btn_night.toggled.connect(self.toggle_night)
        c.add(self.btn_night)
        self.sl_night, wn = self._slider(_("night brightness"), 0, 2, 1, 1.0)
        self.sl_night.valueChanged.connect(self._night_changed)
        c.add(wn)
        self.chk_touch = QCheckBox(_("large click targets"))
        self.chk_touch.setToolTip(_("in the dark, cold and without reading "
                                    "glasses, a small target is expensive"))
        self.chk_touch.toggled.connect(self._touch_changed)
        c.add(self.chk_touch)
        self.btn_full = QPushButton(_("image only   (F)"))
        self._ic(self.btn_full, "expand")
        self.btn_full.setCheckable(True)
        self.btn_full.toggled.connect(self.toggle_full)
        c.add(self.btn_full)
        b = QPushButton(_("show the log   (L)"))
        self._ic(b, "list")
        b.setCheckable(True)
        # Deferred lookup: the log is created after the panels.
        b.toggled.connect(lambda on: self.log.setVisible(on))
        c.add(b)
        self.btn_log = b
        return c

    # ------------------------------------------------------------------ right
    def _right(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        # Stack / last frame toggle, always reachable. It used to be buried in a
        # settings panel, useless during integration — which is exactly when you
        # need to look at the individual sub to see cloud, a knocked tube or a
        # trailed star. The stack averages and hides that.
        bar = QWidget()
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(2, 0, 2, 0)
        hb.setSpacing(6)
        self.btn_view_stack = QPushButton(_("Stack"))
        self.btn_view_live = QPushButton(_("Last frame"))
        self.btn_view_map = QPushButton(_("Sky map   (M)"))
        for b, mode in ((self.btn_view_stack, "stack"),
                        (self.btn_view_live, "live"),
                        (self.btn_view_map, "map")):
            b.setCheckable(True)
            b.clicked.connect(lambda _checked=False, m=mode: self._set_view(m))
            hb.addWidget(b)
        self._ic(self.btn_view_stack, "stack", 15)
        self._ic(self.btn_view_live, "camera", 15)
        self._ic(self.btn_view_map, "target", 15)

        # Zoom on the image bar itself rather than buried in Adjust: zooming is
        # the gesture of someone looking at the image right now.
        hb.addSpacing(10)
        for label, hint, fn in (
                ("−", _("zoom out"), self.zoom_out),
                ("+", _("zoom in"), self.zoom_in),
                (_("fit"), _("fit everything on screen"), self.fit_view),
                ("1:1", _("one sensor pixel per screen pixel"), self.zoom_one),
                (_("save"), _("saves the image as it looks right now (Ctrl+S) — "
                              "a new file each time"), self.save)):
            b = QPushButton(label)
            b.setToolTip(hint)
            if len(label) == 1:
                b.setMaximumWidth(34)
            b.clicked.connect(fn)
            hb.addWidget(b)
        # The loupe is a button on the image bar and not a mode: focus is not a
        # phase of the night, it is something you redo whenever the temperature
        # drifts, in the middle of whatever you were doing.
        self.btn_loupe = QPushButton(_("loupe"))
        self.btn_loupe.setCheckable(True)
        self.btn_loupe.setToolTip(_("5x view of a star over the image, to focus "
                                    "without leaving what you are doing (Z)"))
        self._ic(self.btn_loupe, "focus", 15)
        self.btn_loupe.toggled.connect(self.toggle_loupe)
        hb.addWidget(self.btn_loupe)

        # Elided, not plain: this line grows with the frame's measurements and
        # with the night's summary, and a plain label reports its whole text as
        # a minimum width — that is what widened the window past the screen when
        # TARGETS opened and again on every accepted frame.
        self.lbl_view = ElidedLabel("—")
        self.lbl_view.setFont(T_MONO())
        hb.addSpacing(10)
        hb.addWidget(self.lbl_view, 1)
        hint = QLabel(_("V toggles"))
        hint.setObjectName("statLabel")
        hint.setFont(T_SMALL())
        hb.addWidget(hint)
        v.addWidget(bar)

        self.view = pg.GraphicsLayoutWidget()
        self.vb = self.view.addViewBox(lockAspect=True, invertY=True)
        self.img = pg.ImageItem()
        self.vb.addItem(self.img)

        # The map takes the same space as the image, not a corner: looking for a
        # target is done by looking at the whole sky, and 210 px of context does
        # not fit even one constellation.
        self.skymap = SkyMap()
        self.skymap.align_requested.connect(lambda m: self.align_on(m.obj))
        self.skymap.az_dragged.connect(self._drag_sky)
        self.skymap.searched.connect(self.search)

        # The ranked list takes the whole image area, like the map: choosing a
        # target is reading a list, and a list read in a 210 px strip is a list
        # nobody reads.
        self.targets = TargetTable()
        self.targets.chosen.connect(self._suggestion_chosen)
        self.targets.activated_target.connect(self._use_suggestion)

        self.canvas = QStackedWidget()
        self.canvas.addWidget(self.view)
        self.canvas.addWidget(self.skymap)
        self.canvas.addWidget(self.targets)
        v.addWidget(self.canvas, 1)

        # The loupe floats over the image, so it is a child of the image widget
        # and not a row in any layout.
        self.loupe = Loupe(self.view)
        self.loupe.setVisible(False)
        self.loupe.closed.connect(lambda: self.btn_loupe.setChecked(False))
        self.loupe.beep_toggled.connect(
            lambda on: setattr(self.beeper, "enabled", on))
        self.loupe.reset_best.connect(lambda: self._flag("reset_focus_best"))
        self.loupe.auto_star.connect(lambda: self._pin_loupe(None))
        self.view.installEventFilter(self)
        self.vb.scene().sigMouseClicked.connect(self._image_clicked)

        self.context = QStackedWidget()
        self.context.setMaximumHeight(210)
        self._ctx_index = {
            "frame": self.context.addWidget(self._ctx_frame()),
            "targets": self.context.addWidget(self._ctx_targets()),
            "stack": self.context.addWidget(self._ctx_stack()),
            "config": self.context.addWidget(self._ctx_config()),
        }
        v.addWidget(self.context)
        self._right_col = w
        return w

    def _ctx_frame(self) -> QWidget:
        c = Card(_("target direction"))
        # The arrow is what you read at a glance, hand on the tube and eye at
        # the eyepiece — the text is to confirm, not to guide.
        self.lbl_goto_arrow = QLabel("")
        self.lbl_goto_arrow.setFont(T_DISPLAY())
        self.lbl_goto_arrow.setAlignment(Qt.AlignCenter)
        c.add(self.lbl_goto_arrow)
        self.lbl_goto_dir = QLabel(_("choose a target in TARGETS (2)"))
        self.lbl_goto_dir.setFont(T_BODY())
        self.lbl_goto_dir.setWordWrap(True)
        c.add(self.lbl_goto_dir)
        self.lbl_objects = QLabel("")
        self.lbl_objects.setFont(T_MONO())
        self.lbl_objects.setWordWrap(True)
        c.add(self.lbl_objects, 1)
        return c

    def _ctx_targets(self) -> QWidget:
        """The selected suggestion, factor by factor.

        The score alone would be an oracle. Every factor that produced it is on
        screen for the same reason a rejected frame shows its measurements: a
        verdict nobody can audit is a verdict nobody trusts — and here it is also
        how you learn that the object is fine and it is the Moon that is wrong.
        """
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        c = Card(_("what it looks like"))
        self.preview = PreviewView()
        self.preview.setToolTip(_(
            "DSS survey image of the object, with your frame drawn on it — "
            "the fastest way to see whether the target fits. Downloaded once "
            "and kept on disk, so it is there with no internet in the field."))
        c.add(self.preview, 1)
        c.setMaximumWidth(190)
        h.addWidget(c)

        c = Card(_("suggestion"))
        self.lbl_sug = QLabel(_("pick an object from the list"))
        self.lbl_sug.setFont(T_L())
        self.lbl_sug.setWordWrap(True)
        c.add(self.lbl_sug)
        self.lbl_sug_why = QLabel("")
        self.lbl_sug_why.setFont(T_MONO())
        self.lbl_sug_why.setWordWrap(True)
        c.add(self.lbl_sug_why, 1)
        row = QHBoxLayout()
        self.btn_use = QPushButton(_("make it the target"))
        self._ic(self.btn_use, "target")
        self.btn_use.setEnabled(False)
        self.btn_use.setToolTip(_("sets the target and opens FRAME, where the "
                                  "arrow says which way to push"))
        self.btn_use.clicked.connect(
            lambda: self._use_suggestion(self.targets.current()))
        self.btn_see = QPushButton(_("see on the map"))
        self._ic(self.btn_see, "star")
        self.btn_see.setEnabled(False)
        self.btn_see.clicked.connect(self._show_suggestion_on_map)
        row.addWidget(self.btn_use, 1)
        row.addWidget(self.btn_see)
        c.add_layout(row)
        h.addWidget(c, 1)

        c2 = Card(_("why it scored that"))
        # Two columns: the sixth factor did not fit in one, and the context
        # strip is capped at 210 px on purpose — it may not steal height from
        # the image.
        grid = QWidget()
        g = QGridLayout(grid)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(1)
        self.st_f_alt = Stat(_("altitude"), "—", min_width=132)
        self.st_f_window = Stat(_("time left"), "—", min_width=132)
        self.st_f_moon = Stat(_("Moon"), "—", min_width=132)
        self.st_f_size = Stat(_("size in frame"), "—", min_width=132)
        self.st_f_bright = Stat(_("surface brightness"), "—", min_width=132)
        # The sixth factor is on screen for the same reason as the other five:
        # it is the one that surprises, because it is about the catalogue rather
        # than about the sky.
        self.st_f_fame = Stat(_("named object"), "—", min_width=132)
        for i, st in enumerate((self.st_f_alt, self.st_f_window, self.st_f_moon,
                                self.st_f_size, self.st_f_bright,
                                self.st_f_fame)):
            g.addWidget(st, i % 3, i // 3)
        c2.add(grid)
        c2.setMaximumWidth(420)
        h.addWidget(c2)
        return w

    def _ctx_stack(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        c = Card(_("histogram"))
        self.hist = pg.PlotWidget()
        self.hist.setLogMode(False, True)
        # One curve per channel: on a colour sensor the mean hides exactly what
        # matters — a channel saturating alone, a colour imbalance, a gradient
        # only in the red.
        self.hist_rgb = [self.hist.plot() for _ in range(3)]
        self.hist_curve = self.hist_rgb[0]
        self.hist_black = pg.InfiniteLine(angle=90)
        self.hist.addItem(self.hist_black)
        self.hist_white = pg.InfiniteLine(angle=90)
        self.hist.addItem(self.hist_white)
        c.add(self.hist, 1)
        h.addWidget(c, 1)

        c2 = Card(_("discarded frames"))
        self.lbl_rej = QLabel(_("none"))
        self.lbl_rej.setFont(T_MONO())
        self.lbl_rej.setWordWrap(True)
        self.lbl_rej.setToolTip(_("why frames are being dropped — what tells you "
                                  "whether to change strictness, refocus or wait "
                                  "out the cloud"))
        c2.add(self.lbl_rej, 1)
        c2.setMaximumWidth(210)
        h.addWidget(c2)

        c2 = Card(_("residual rotation"))
        grid = QWidget()
        g = QVBoxLayout(grid)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(1)
        self.st_rot = Stat(_("rotation"), "—")
        self.st_useful = Stat(_("useful integration"), "—")
        self.st_smear = Stat(_("corner smear"), "—")
        for s in (self.st_rot, self.st_useful, self.st_smear):
            g.addWidget(s)
        c2.add(grid)
        c2.setMaximumWidth(230)
        h.addWidget(c2)
        return w

    def _ctx_config(self) -> QWidget:
        """A full-width histogram.

        The image stays on screen while you configure, and the histogram is the
        one readout that is relevant regardless of which panel is open.
        """
        c = Card(_("histogram"))
        self.hist2 = pg.PlotWidget()
        self.hist2.setLogMode(False, True)
        self.hist2_rgb = [self.hist2.plot() for _ in range(3)]
        self.hist2_curve = self.hist2_rgb[0]
        self.hist2_black = pg.InfiniteLine(angle=90)
        self.hist2.addItem(self.hist2_black)
        self.hist2_white = pg.InfiniteLine(angle=90)
        self.hist2.addItem(self.hist2_white)
        c.add(self.hist2, 1)
        return c

    def _slider(self, name, lo, hi, val, div):
        hold = QWidget()
        h = QHBoxLayout(hold)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(name)
        lab.setFont(T_BODY())
        lab.setMinimumWidth(96)
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(val)
        out = QLabel(f"{val/div:.2f}")
        out.setFont(T_MONO())
        out.setMinimumWidth(40)
        s.valueChanged.connect(
            lambda x: (out.setText(f"{x/div:.2f}"), self._render()))
        h.addWidget(lab)
        h.addWidget(s, 1)
        h.addWidget(out)
        s._div = div
        return s, hold

    def _shortcuts(self) -> None:
        def typing() -> bool:
            """A letter shortcut must not steal a key from someone writing.

            Without this, searching for "Mimosa" on the map opens map mode on
            the M, turns night mode on with the N and types nothing.
            """
            f = QApplication.focusWidget()
            return isinstance(f, (QLineEdit, QSpinBox, QDoubleSpinBox))

        def guard(fn):
            return lambda: None if typing() else fn()

        for i, mode in enumerate(MODES):
            QShortcut(QKeySequence(str(i + 1)), self,
                      guard(lambda k=mode[0]: self.rail.select(k)))
        for key, fn in (("+", self.zoom_in), ("=", self.zoom_in),
                        ("-", self.zoom_out), ("0", self.fit_view),
                        ("V", self.toggle_view),
                        ("Z", lambda: self.btn_loupe.toggle()),
                        ("M", lambda: self._set_view(
                            "live" if self._view == "map" else "map")),
                        ("F", lambda: self.btn_full.toggle()),
                        ("N", lambda: self.btn_night.toggle()),
                        ("L", lambda: self.btn_log.toggle()),
                        ("Escape", self._exit_review),
                        ("Space", lambda: self._flag("new_segment")),
                        ("Ctrl+S", self.save)):
            QShortcut(QKeySequence(key), self,
                      fn if key.startswith("Ctrl") else guard(fn))

    def _block_wheel(self) -> None:
        """Install the wheel guard on every value field. Plots and the image are
        deliberately excluded: there the wheel zooms, which is expected."""
        self._wheel_guard = _WheelGuard(self)
        for kind in (QAbstractSpinBox, QComboBox, QSlider):
            for w in self.findChildren(kind):
                w.installEventFilter(self._wheel_guard)
                # StrongFocus: without it the wheel would still focus the field.
                w.setFocusPolicy(Qt.StrongFocus)

    # ------------------------------------------------------------------ icons
    def _pin_button_widths(self) -> None:
        """Floor the session buttons' width at what they ask for.

        Without it the layout squeezes them when the bar gets tight and the
        labels turn into "Start cap…". Redone on every theme change because the
        natural size depends on the stylesheet padding and the font, which only
        exist once the theme is applied.
        """
        for b in getattr(self, "_session_buttons", ()):
            b.setMinimumWidth(0)
            b.setMinimumWidth(b.sizeHint().width())

    def _rgb_pens(self) -> list:
        """One pen per channel.

        In night mode there is no hue to spend: the three become shades of red
        and what separates them is the **stroke** — solid, dashed, dotted.
        """
        p = self.pal
        if p.image_red:
            return [pg.mkPen(p.bad, width=1),
                    pg.mkPen(p.warn, width=1, style=Qt.DashLine),
                    pg.mkPen(p.text_dim, width=1, style=Qt.DotLine)]
        return [pg.mkPen(p.bad, width=1), pg.mkPen(p.ok, width=1),
                pg.mkPen(p.accent, width=1)]

    def _ic(self, widget, name: str, size: int = 17):
        """Apply an icon and register the pair, to re-tint when the theme changes.

        Without the registry, switching to night mode would leave bright icons on
        a black background — more light on screen than any red button saves.
        """
        # Idempotent: reapplying on a widget replaces its entry, otherwise the
        # registry accumulates duplicates and re-tinting becomes ambiguous.
        self._iconed = [(w, n, sz) for w, n, sz in self._iconed if w is not widget]
        self._iconed.append((widget, name, size))
        widget.setIcon(icons.icon(name, self.pal.text, size))
        widget.setIconSize(QSize(size, size))
        return widget

    def _retint_icons(self) -> None:
        for widget, name, size in self._iconed:
            widget.setIcon(icons.icon(name, self.pal.text, size))
        self.rail.set_icon_provider(
            lambda n, checked: icons.icon(
                n, self.pal.bg if checked else self.pal.text))

    # ================================================================== modes
    def set_mode(self, key: str) -> None:
        self._mode = key
        self._req(mode=key)
        self.rail.mark(key)          # a mode may be entered from a button too
        self.panels.setCurrentIndex(self._panel_index[key])
        self.context.setCurrentIndex(self._ctx_index[key])
        # Each mode has a sensible default, but an explicit choice on the bar
        # holds until you change mode again.
        self._set_view({"frame": "live", "targets": "targets"}.get(key, "stack"))
        if key == "targets":
            self._refresh_targets()
        if key == "frame":
            self._refresh_align_pick()
        self._render(True)

    # ================================================================ session
    def _req(self, **kw) -> None:
        if self.worker:
            self.worker.request(**kw)

    def _flag(self, name: str) -> None:
        if self.worker:
            self.worker.flag(name)
            if name == "reset":
                self._exit_review()
                self.health.clear()
                self._hist.clear()

    def _exposure_changed(self, x: float) -> None:
        self._exposure = x
        self._req(exposure=x)

    def _cooler_toggled(self, on: bool) -> None:
        self._req(target_temp=self.sp_temp.value(), cooler=on)
        if not self.worker:
            self.lbl_cool.setText(_("cooler will start with the capture") if on
                                  else _("cooler off"))

    def _bin_changed(self, text: str) -> None:
        if int(text) > 2:
            self.on_log(_("warning: bin{bin} clips the highlights; bin2 is the "
                          "only lossless one on this camera").format(bin=text))
        self._req(bin=int(text))
        self._update_scale_label()

    def _optics_changed(self) -> None:
        self.settings.focal_length_mm = self.sp_focal.value()
        self.settings.pixel_size_um = self.sp_pixel.value()
        self._update_scale_label()

    def _update_scale_label(self) -> None:
        if not hasattr(self, "lbl_scale"):
            return
        s = self.pixel_scale()
        w = self._q.shape[1] if self._q is not None else 1920
        h = self._q.shape[0] if self._q is not None else 1080
        self.lbl_scale.setText(
            _("{scale:.3f}\"/px · field {w:.0f}' x {h:.0f}'").format(
                scale=s, w=s * w / 60.0, h=s * h / 60.0))

    def _language_changed(self, index: int) -> None:
        tag = self.cb_lang.itemData(index)
        if not tag or tag == self.settings.language:
            return
        self.settings.language = tag
        self.settings.save()
        if self.worker is not None:
            self.on_log(_("language will change when the session ends"))
            return
        self.language_changed.emit(tag)

    def _replay_speed(self) -> float:
        return REPLAY_SPEEDS.get(self.cb_speed.currentData(), 4.0)

    def _source_changed(self, i: int) -> None:
        replay = i == 1
        self.cb_camera.setVisible(not replay)
        self.btn_refresh.setVisible(not replay)
        self.btn_folder.setVisible(replay)
        self.rep_row.setVisible(replay)

    def refresh_cameras(self) -> None:
        self.cb_camera.clear()
        try:
            cams = list_cameras()
        except Exception as e:
            self.on_log(_("error listing cameras: {error}").format(error=e))
            return
        if not cams:
            self.cb_camera.addItem(_("no camera"))
            self.on_log(_("no camera"))
            return
        for c in cams:
            self.cb_camera.addItem(f"{c.name}  ({c.port})")
        self.on_log(_("{n} camera(s): {names}").format(
            n=len(cams), names=", ".join(c.name for c in cams)))

    def pick_replay(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, _("recorded session"), str(self.settings.path("capture_dir")))
        if d:
            self._replay_folder = d
            self.on_log(_("replay: {path}").format(path=d))

    def pick_dir(self, key: str) -> None:
        """Choose one of the output folders. Persisted immediately."""
        current = str(self.settings.path(key))
        d = QFileDialog.getExistingDirectory(self, _("choose a folder"), current)
        if not d:
            return
        setattr(self.settings, key, d)
        self.settings.save()
        lab = self._dir_labels.get(key)
        if lab is not None:
            lab.setText(_shorten(d))
            lab.setToolTip(d)
        self.on_log(_("{name} -> {path}").format(name=key, path=d))

    def _reveal(self, path: Path) -> None:
        """Open a folder in the system file manager."""
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def reset_stack(self) -> None:
        """Discard what has accumulated. Always asks first, if there is
        anything to lose.

        There is no undo: the accumulator is an array in memory. What survives
        on disk are the subs already written, and redoing the integration from
        them is a replay run — not a click.
        """
        from PySide6.QtWidgets import QMessageBox
        integ = self._last_stats.get("integration", 0.0)
        n = self._last_stats.get("n_stacked", 0)
        if not n:
            self.on_log(_("nothing accumulated to reset"))
            return
        r = QMessageBox.question(
            self, _("Reset"),
            _("Discard {time} of integration ({n} frames)?\n\n"
              "Subs already written to disk are not deleted.").format(
                  time=_hms(integ), n=n),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return
        self._flag("reset")
        self.on_log(_("stack reset"))

    def pick_dark(self) -> None:
        p, _sel = QFileDialog.getOpenFileName(
            self, _("master dark"), str(self.settings.path("dark_dir")),
            "FITS (*.fits)")
        if p:
            self._dark_path = p
            self.lbl_dark.setText(_("dark: {name}").format(name=Path(p).name))
            self._req(dark_path=p)

    def pick_flat(self) -> None:
        p, _sel = QFileDialog.getOpenFileName(
            self, _("master flat"), str(self.settings.path("flat_dir")),
            "FITS (*.fits)")
        if p:
            self._flat_path = p
            self.lbl_flat.setText(_("flat: {name}").format(name=Path(p).name))
            self._req(flat_path=p)

    def _ask_target_name(self) -> bool:
        """Ask for the target name when integration starts.

        It is the only moment the name matters: nothing goes to disk before
        this, and it is from here on that the session folder gains content. As a
        permanent field in the panel it stayed blank all night and every session
        ended up called "session".
        """
        from PySide6.QtWidgets import QInputDialog
        suggestion = self._target_name
        if not suggestion and self._target is not None:
            suggestion = self._target.label.split(" (")[0]
        name, ok = QInputDialog.getText(
            self, _("Integrate"),
            _("Target name (it will name the session folder):"),
            text=suggestion)
        if not ok:
            return False
        self._target_name = name.strip()
        self.st_target.set(self._target_name or "—")
        self._req(target_name=self._target_name)
        return True

    def _integrate_toggled(self, on: bool) -> None:
        if on and not self._ask_target_name():
            self.btn_integrate.setChecked(False)     # cancelled in the dialog
            return
        self.btn_integrate.setText(_("Stop integrating") if on
                                   else _("Start integrating"))
        self._ic(self.btn_integrate, "pause" if on else "play")
        self._flag("integrate_on" if on else "integrate_off")
        if on and not self.worker:
            self.on_log(_("start the capture first"))

    def capture_dark(self) -> None:
        if not self.worker:
            self.on_log(_("start the capture before recording a dark"))
            return
        from PySide6.QtWidgets import QInputDialog
        n, ok = QInputDialog.getInt(
            self, _("Record dark"),
            _("How many frames?\n\n"
              "CAP THE SENSOR before confirming.\n"
              "Capture pauses while recording."), 20, 5, 100)
        if not ok:
            return
        self._req(dark_frames=n)
        self._flag("capture_dark")
        self.on_log(_("recording a dark of {n} frames — keep the sensor capped"
                      ).format(n=n))

    def capture_flat(self) -> None:
        if not self.worker:
            self.on_log(_("start the capture before recording a flat"))
            return
        from PySide6.QtWidgets import QInputDialog
        n, ok = QInputDialog.getInt(
            self, _("Record flat"),
            _("How many frames?\n\n"
              "POINT at an evenly illuminated surface and set the exposure so\n"
              "the histogram lands near half scale. The loaded dark is\n"
              "subtracted if it matches. Capture pauses while recording."),
            20, 5, 100)
        if not ok:
            return
        self._req(flat_frames=n)
        self._flag("capture_flat")
        self.on_log(_("recording a flat of {n} frames — do not change the "
                      "lighting").format(n=n))

    def start(self) -> None:
        replay = self.cb_source.currentIndex() == 1
        cfg = Config.from_settings(
            self.settings,
            mode=self._mode,
            source="replay" if replay else "camera",
            camera_index=max(self.cb_camera.currentIndex(), 0),
            replay_folder=self._replay_folder,
            bin=int(self.cb_bin.currentText()),
            exposure=self.sp_exp.value(),
            gain=self.sp_gain.value(), offset=self.sp_offset.value(),
            target_temp=(self.sp_temp.value()
                         if self.btn_cooler.isChecked() else None),
            dark_path=self._dark_path, flat_path=self._flat_path,
            quality_weighting=self.chk_weight.isChecked(),
            strictness=self.cb_strictness.currentData(),
            sigma_clip=(self.sp_sigma.value()
                        if self.sp_sigma.value() > 0 else None),
            replay_speed=self._replay_speed(),
            replay_loop=self.chk_loop.isChecked(),
            record=self.chk_record.isChecked(), target_name=self._target_name,
            record_every=self.sp_every.value(),
            compress=self.chk_compress.isChecked(),
        )
        self._exposure = cfg.exposure
        self._exit_review()
        self.health.clear()
        self._hist.clear()
        self.alert.setVisible(False)
        self.worker = CaptureWorker(cfg)
        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.frame.connect(self.on_frame)
        self.worker.focus.connect(self.on_focus)
        self.worker.log.connect(self.on_log)
        self.worker.failed.connect(self.on_failed)
        self.worker.opened.connect(self.on_opened)
        self.worker.finished.connect(self.on_finished)
        self.worker.paused.connect(self.on_paused)
        self.worker.cooling.connect(self.on_cooling)
        self.worker.dark_saved.connect(self.on_dark_saved)
        self.worker.flat_saved.connect(self.on_flat_saved)
        # A star pinned before this session started stays pinned.
        self.worker.set_loupe(self._loupe_xy)
        self.thread.start()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_finish.setEnabled(True)
        self._paused = False
        self._set_state("exposing")
        self._t_frame = time.time()
        if self.btn_integrate.isChecked():
            # The button may have been switched on before the capture existed —
            # in a replay it is the only way not to lose the first frames.
            self._flag("integrate_on")

    def toggle_pause(self) -> None:
        if not self.worker:
            return
        self.worker.set_paused(not self._paused)

    def finish(self) -> None:
        if self.worker:
            self.worker.stop()
        self.btn_pause.setEnabled(False)
        self.btn_finish.setEnabled(False)
        self._set_state("stopping")

    def save(self) -> None:
        """Save what is on screen, now, without interrupting the integration.

        Each call produces a new file, stamped with the time and the frame
        count. It goes to the session folder when recording — that is where the
        subs and the `session.json` are, and a stray PNG elsewhere loses the
        context of how it was made.
        """
        if self._stack is None and self._live is None:
            self.on_log(_("nothing to save"))
            return
        import cv2
        from astropy.io import fits

        out = self._output_dir()
        st = self._last_stats
        if self._revisit is not None:
            # Under review, what is on screen is that sub, not the stack: the
            # name has to say so, and the accumulator's .fits has no business here.
            name = f"frame_{self._revisit['index']:05d}_{time.strftime('%H%M%S')}"
            cv2.imwrite(str(out / f"{name}.png"),
                        cv2.cvtColor(self._display(), cv2.COLOR_RGB2BGR))
            self.on_log(_("saved: {path}").format(path=out / f"{name}.png"))
            return
        n = st.get("n_stacked", 0)
        name = f"stack_{time.strftime('%H%M%S')}"
        if n:
            name += f"_{n}f_{_hms(st.get('integration', 0.0))}"
        cv2.imwrite(str(out / f"{name}.png"),
                    cv2.cvtColor(self._display(), cv2.COLOR_RGB2BGR))
        if self._stack is not None:
            fits.PrimaryHDU(self._stack.transpose(2, 0, 1)).writeto(
                out / f"{name}.fits", overwrite=True)
        self.on_log(_("saved: {path}").format(path=out / f"{name}.png"))

    def _output_dir(self) -> Path:
        rec = getattr(self.worker, "recorder", None) if self.worker else None
        d = getattr(rec, "session_dir", None) if rec else None
        if d:
            out = Path(d)
            out.mkdir(parents=True, exist_ok=True)
            return out
        return self.settings.path("export_dir", create=True)

    # =================================================================== theme
    def toggle_night(self, on: bool) -> None:
        self._theme = "night" if on else "dark"
        self.settings.theme = self._theme
        self.settings.save()
        self.apply_theme()

    def _night_changed(self, v: int) -> None:
        self._night_level = int(v)
        self.settings.night_level = self._night_level
        self.settings.save()
        if self._theme == "night":
            self.apply_theme()

    def _touch_changed(self, on: bool) -> None:
        self._large_targets = bool(on)
        self.settings.large_targets = self._large_targets
        self.settings.save()
        self.apply_theme()

    def apply_theme(self) -> None:
        p = palette(self._theme, self._night_level)
        self.pal = p
        QApplication.instance().setStyleSheet(stylesheet(p, self._large_targets))
        for plot, curve, line in (
                (self.hist, self.hist_curve, self.hist_black),
                (self.hist2, self.hist2_curve, self.hist2_black)):
            plot.setBackground(p.plot_bg)
            for ax in ("left", "bottom"):
                plot.getAxis(ax).setPen(p.text_dim)
                plot.getAxis(ax).setTextPen(p.text_dim)
            curve.setPen(pg.mkPen(p.curve, width=2))
            line.setPen(pg.mkPen(p.mark, style=Qt.DashLine))
        for curves in (self.hist_rgb, self.hist2_rgb):
            for curve, pen in zip(curves, self._rgb_pens(), strict=True):
                curve.setPen(pen)
        # Black dashed, white dotted: in night mode colour separates nothing.
        for lw in (self.hist_white, self.hist2_white):
            lw.setPen(pg.mkPen(p.text_dim, style=Qt.DotLine))
        self.view.setBackground(p.plot_bg)
        self.loupe.set_palette(p)
        self.health.set_palette(p)
        self.skymap.set_palette(p)
        self.targets.set_palette(p)
        self.preview.set_palette(p)
        self._pin_button_widths()
        self._style_completer()
        self._retint_icons()
        self._set_state(self._state)
        self._render(True)

    def toggle_full(self, on: bool) -> None:
        self.btn_view_stack.parentWidget().setVisible(not on)
        self._left_col.setVisible(not on)
        self.context.setVisible(not on)
        self.log.setVisible(not on and self.btn_log.isChecked())

    def zoom_in(self) -> None:
        self._zoom(1.35)

    def zoom_out(self) -> None:
        self._zoom(1 / 1.35)

    def _zoom(self, factor: float) -> None:
        """Zoom whatever is on display.

        On the map the zoom is of the field, not of an image — the same buttons
        serve all three views, otherwise each would need its own.
        """
        if self._view == "map":
            self.skymap.fov = float(np.clip(self.skymap.fov / factor, 2.0, 110.0))
            self.skymap.update()
            return
        self.vb.scaleBy((1 / factor, 1 / factor),
                        center=self.vb.viewRect().center())

    def fit_view(self) -> None:
        if self._view == "map":
            self.skymap.fov = 40.0
            self.skymap.update()
            return
        self.vb.autoRange()

    def zoom_one(self) -> None:
        """One sensor pixel per screen pixel. On the map, the equivalent is
        framing exactly the camera's field."""
        if self._view == "map":
            self.skymap.fov = max(self.skymap.cam_fov[0] * 1.4, 0.4)
            self.skymap.update()
            return
        if self._q is None:
            return
        c = self.vb.viewRect().center()
        vw, vh = self.vb.width() or 800, self.vb.height() or 600
        self.vb.setRange(xRange=(c.x() - vw / 2, c.x() + vw / 2),
                         yRange=(c.y() - vh / 2, c.y() + vh / 2), padding=0)

    def apply_preset(self, name: str) -> None:
        bg, clip = STRETCH_PRESETS[name]
        self.chk_auto.setChecked(True)
        self.sl_bg.setValue(int(round(bg * 100)))
        self.sl_clip.setValue(int(round(clip * 10)))

    # ======================================================== alignment star
    def _refresh_align_pick(self, force: bool = False) -> None:
        """Recompute which star is worth aligning on.

        Every 30 s while FRAME is open, and immediately when the target changes:
        the answer depends on the target, and 7 ms of astropy is not something
        to spend on the 120 ms tick.
        """
        if not hasattr(self, "lbl_align_pick"):
            return
        now = time.monotonic()
        # `force` is "the target changed, answer again". Everything else waits
        # for the throttle, and for the window to be on screen: the first
        # astropy call of the process costs ~0.5 s (imports and tables), and
        # paying it inside the constructor delayed the window by that much for
        # a label nobody was looking at yet — 570 ms to open, against 203 now.
        if not force and (not self.isVisible() or now - self._align_t < 30.0):
            return
        self._align_t = now
        target = ((self._target.ra, self._target.dec)
                  if self._target is not None else None)
        # The floor is the user's own horizon — the same wall and trees the
        # target list already knows about — never below what refraction allows.
        floor = max(self.sp_min_alt.value(), brightstars.FLOOR_ALT)
        try:
            self._align_picks = brightstars.for_alignment(
                self.sp_lat.value(), self.sp_lon.value(), target=target,
                min_alt=floor, elevation_m=self.sp_elev.value())
        except Exception as e:                # ephemeris or clock trouble
            self.on_log(_("could not choose an alignment star: {error}").format(
                error=e))
            return
        self._align_i = 0
        self._show_align_pick()

    def _show_align_pick(self) -> None:
        pick = self._align_pick()
        has = pick is not None
        self.btn_align_pick.setEnabled(has)
        self.btn_align_next.setEnabled(len(self._align_picks) > 1)
        self.btn_align_map.setEnabled(has)
        if not has:
            self.lbl_align_pick.setText(
                _("no alignment star above {alt:.0f}°").format(
                    alt=max(self.sp_min_alt.value(), brightstars.FLOOR_ALT)))
            return
        # The magnitude is on the line, not only in the score: it is what tells
        # you whether to expect the star in the finder or in the naked eye.
        text = _("{star} · mag {mag:.1f}\n{alt:.0f}° up, {dir}").format(
            star=pick.star.full, mag=pick.star.mag, alt=pick.alt,
            dir=compass_point(pick.az))
        if np.isfinite(pick.target_sep):
            text += "\n" + _("{deg:.0f}° from the target").format(
                deg=pick.target_sep)
        self.lbl_align_pick.setText(text)

    def _align_pick(self):
        if not self._align_picks:
            return None
        return self._align_picks[self._align_i % len(self._align_picks)]

    def _align_next_pick(self) -> None:
        if self._align_picks:
            self._align_i = (self._align_i + 1) % len(self._align_picks)
            self._show_align_pick()

    def _align_on_pick(self) -> None:
        pick = self._align_pick()
        if pick is not None:
            self.align_on(pick.star)

    def _align_pick_on_map(self) -> None:
        pick = self._align_pick()
        if pick is None:
            return
        self._found = pick.star
        self._set_view("map")
        self._update_map()

    # ================================================================= targets
    def _targets_when(self) -> datetime:
        """The instant the list is computed for, as an *aware* datetime.

        Aware on purpose: astropy reads a naive datetime as UTC, the field on
        screen is local time, and three hours of silent error puts every object
        in the wrong half of the sky.
        """
        if self.btn_now.isChecked():
            return datetime.now().astimezone()
        return self.dt_when.dateTime().toPython().astimezone()

    def _now_toggled(self, on: bool) -> None:
        self.dt_when.setEnabled(not on)
        if on:
            self._when_guard = True
            self.dt_when.setDateTime(QDateTime.currentDateTime())
            self._when_guard = False
        self._refresh_targets()

    def _when_edited(self, *_args) -> None:
        if self._when_guard:
            return
        # Typing an hour is asking for that hour: staying on "now" would undo
        # the edit on the next tick.
        if self.btn_now.isChecked():
            self.btn_now.setChecked(False)     # this refreshes on its own
            return
        self._refresh_targets()

    def _shift_when(self, hours: float) -> None:
        base = self.dt_when.dateTime() if not self.btn_now.isChecked() \
            else QDateTime.currentDateTime()
        self.btn_now.setChecked(False)
        self._when_guard = True
        self.dt_when.setDateTime(base.addSecs(int(hours * 3600)))
        self._when_guard = False
        self._refresh_targets()

    def _fov_arcmin(self) -> tuple[float, float]:
        """The frame in arcminutes, from the optics and the selected binning."""
        w = self._q.shape[1] if self._q is not None else 1920
        h = self._q.shape[0] if self._q is not None else 1080
        e = self.pixel_scale() / 60.0
        return e * w, e * h

    def _refresh_targets(self, *_args) -> None:
        """Recompute the whole list, from the sky down.

        Cheap enough to run on every filter change and every minute of the
        clock: 12 ms measured over the 12036 objects of OpenNGC — 9 ms of
        astropy (sidereal time, Sun, Moon) and 3 ms for the ranking itself,
        which is one vectorised pass.
        """
        if not hasattr(self, "targets"):
            return                          # still building the window
        cat = self._cat()
        if cat is None:
            self.lbl_sky.setText(_("no catalogue — run `astrodoro catalog`"))
            self.targets.set_rows([])
            return
        when = self._targets_when()
        try:
            sky = tonight.sky_at(self.sp_lat.value(), self.sp_lon.value(),
                                 when, elevation_m=self.sp_elev.value())
        except Exception as e:                # ephemeris or clock trouble
            self.on_log(_("could not read the sky: {error}").format(error=e))
            return
        self._sky = sky
        # Only the lines that say something: `twilight_text` is empty once the
        # sky is dark, and a leading blank line reads as a missing reading.
        self.lbl_sky.setText("\n".join(t for t in (sky.twilight_text(),
                                                   sky.moon_text()) if t))

        fov = self._fov_arcmin()
        self.lbl_fov.setText(_("frame {w:.0f}' x {h:.0f}'").format(w=fov[0],
                                                                  h=fov[1]))
        rows = tonight.rank(
            cat.objs, sky, self.sp_lat.value(), fov_arcmin=fov,
            min_alt=self.sp_min_alt.value(), max_mag=self.sp_max_mag.value(),
            family=self.cb_family.currentData(),
            fits_only=self.chk_fits.isChecked())
        self.targets.set_rows(rows)
        self._targets_t = time.monotonic()
        if self._view == "targets":
            self._update_view_label()
        if not rows:
            self.lbl_sug.setText(_("nothing passes these filters at this hour"))
            self.lbl_sug_why.setText(
                _("lower the minimum altitude, allow fainter objects, or try "
                  "another hour."))

    def _previews_toggled(self, on: bool) -> None:
        self.btn_prefetch.setEnabled(on)
        cur = self.targets.current()
        if not on:
            self.preview.clear(_("previews are off"))
        elif cur is not None:
            self._request_preview(cur.obj)

    def _suggestion_chosen(self, s) -> None:
        self.btn_use.setEnabled(s is not None)
        self.btn_see.setEnabled(s is not None)
        if s is None:
            self.preview.clear(_("pick an object"))
            return
        o = s.obj
        self.lbl_sug.setText(f"{o.label} · {o.kind_label}")
        self.lbl_sug_why.setText(" · ".join(s.reasons()))
        # The factor is the value and the measurement is the label, not the
        # other way round: six factors on one line each only stay readable if
        # every value is the same three characters wide, and it is the factors
        # that are meant to be compared with one another.
        f = s.factors
        frac, sb = s.field_fraction, s.surface_brightness
        left = (_("all night") if np.isinf(s.minutes_left)
                else _("{min:.0f} min").format(min=s.minutes_left))
        fame = (_("Messier") if o.messier
                else (_("has a name") if o.common else _("catalogue number")))
        for st, label, factor in (
                (self.st_f_alt, _("altitude {alt:.0f}°").format(alt=s.alt),
                 f["altitude"]),
                (self.st_f_window, left, f["window"]),
                (self.st_f_moon, _("Moon {deg:.0f}°").format(deg=s.moon_sep),
                 f["moon"]),
                (self.st_f_size, _("frame {pct:.0f}%").format(pct=frac * 100)
                 if frac else _("size unknown"), f["size"]),
                (self.st_f_bright,
                 _("{sb:.1f} mag/arcsec²").format(sb=sb) if np.isfinite(sb)
                 else _("brightness unknown"), f["brightness"]),
                (self.st_f_fame, fame, f["fame"])):
            st.set_label(label)
            st.set(f"{factor:.2f}", self._factor_colour(factor))
        self._request_preview(o)

    # ------------------------------------------------------------- previews
    def _preview_key(self, o) -> tuple[str, float]:
        """Which cutout this object needs: its name and the field to ask for."""
        return o.name, previews.cutout_fov(o.major_arcmin, self._fov_arcmin())

    def _request_preview(self, o) -> None:
        if not self.chk_previews.isChecked():
            self.preview.clear(_("previews are off"))
            return
        name, fov = self._preview_key(o)
        self._preview_want = name
        hit = self.preview_loader.request(name, o.ra, o.dec, fov)
        if hit is not None:
            self._show_preview(o, str(hit))
        else:
            self.preview.clear(_("fetching…"))

    def _show_preview(self, o, path: str) -> None:
        _name, fov = self._preview_key(o)
        frame = previews.frame_fraction(self._fov_arcmin(), fov)
        # A rectangle bigger than the picture is not a rectangle, it is four
        # lines outside the frame. When the field is wider than the cutout the
        # answer is "it fits with room to spare", which the reasons already say.
        if max(frame) > 0.98:
            frame = None
        # The label carries the attribution the survey asks for, and doubles as
        # the scale of the picture.
        self.preview.show_image(path, frame,
                                _("DSS2 · {fov:.0f}'").format(fov=fov))

    @Slot(str, str)
    def _preview_ready(self, name: str, path: str) -> None:
        # The list may have moved on while this was in flight.
        s = self.targets.current()
        if s is None or s.obj.name != name or self._preview_want != name:
            return
        self._show_preview(s.obj, path)

    @Slot(str)
    def _preview_failed(self, name: str) -> None:
        if self._preview_want == name:
            self.preview.clear(_("no preview — offline?"))

    def _prefetch_previews(self) -> None:
        """Cache every suggestion on screen, for a night with no signal.

        This is the button that makes the feature work where the telescope is:
        the pictures are fetched at home, over the kitchen wifi, and the field
        only ever reads the disk.
        """
        if not self.chk_previews.isChecked():
            self.on_log(_("previews are off — tick the box first"))
            return
        rows = self.targets._rows
        want = [(s.obj, *self._preview_key(s.obj)) for s in rows]
        missing = [(o, n, f) for o, n, f in want
                   if self.preview_loader.cached(n, f) is None]
        for o, name, fov in missing:
            self.preview_loader.request(name, o.ra, o.dec, fov)
        self.on_log(_("previews: {n} already cached, fetching {m}").format(
            n=len(want) - len(missing), m=len(missing)))

    def _factor_colour(self, factor: float) -> str:
        """Which factor cost the object its score, at a glance."""
        p = self.pal
        return p.ok if factor >= 0.85 else (p.warn if factor >= 0.5 else p.bad)

    def _use_suggestion(self, s) -> None:
        """Chosen from the list: it becomes the target and FRAME opens.

        Switching mode is the point — the next gesture is pushing the tube, and
        that is the screen with the arrow on it.
        """
        if s is None:
            return
        self._apply_target(s.obj)
        self.ed_goto.setText(s.obj.label.split(" (")[0])
        self.rail.select("frame")

    def _show_suggestion_on_map(self) -> None:
        s = self.targets.current()
        if s is None:
            return
        self._found = s.obj
        self._set_view("map")
        self._update_map()

    # =================================================================== loupe
    @property
    def _loupe_on(self) -> bool:
        """Whether the loupe should be drawing.

        Asked of the button and the view rather than of `isVisible()`: a widget
        answers False while its window is still hidden, which is exactly the
        state the tests run in and would silently stop the crop from updating.
        """
        return self.btn_loupe.isChecked() and self._view in ("live", "stack")

    def toggle_loupe(self, on: bool) -> None:
        if on and self._view == "map":
            # The loupe magnifies the last frame, so it has no meaning over the
            # map: opening it there would show a frozen crop of nothing.
            self._set_view("live")
        self.loupe.setVisible(self._loupe_on)
        if self._loupe_on:
            self.loupe.set_palette(self.pal)
            self.loupe.set_pinned(self._loupe_xy)
            self.loupe.place()

    def _pin_loupe(self, xy) -> None:
        """Which star the loupe follows: a pinned one, or the brightest."""
        self._loupe_xy = xy
        if self.worker:
            self.worker.set_loupe(xy)
        self.loupe.set_pinned(xy)

    def _image_clicked(self, ev) -> None:
        if not self._loupe_on:
            return
        if self._q is None:
            return
        pos = self.vb.mapSceneToView(ev.scenePos())
        x, y = float(pos.x()), float(pos.y())
        h, w = self._q.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            return
        self._pin_loupe((x, y))

    def eventFilter(self, obj, ev) -> bool:
        # The loupe floats over the image, so nothing in a layout moves it.
        if obj is self.view and ev.type() == QEvent.Resize and self._loupe_on:
            self.loupe.place()
        return super().eventFilter(obj, ev)

    # ===================================================================== sky
    def _cat(self) -> Catalog | None:
        """The catalogue, loaded once.

        A failed load is remembered: TARGETS asks for it once a minute while the
        list follows the clock, and without this the log would fill with the same
        warning all night. A retry still happens if the file turns up — someone
        running `astrodoro catalog` in another terminal is exactly what the
        warning asks for.
        """
        if self._catalog is not None:
            return self._catalog
        if self._cat_missing and not self.settings.catalog_path().exists():
            return None
        try:
            self._catalog = Catalog(self.settings.catalog_path()).load()
            self._cat_missing = False
            self.on_log(_("catalogue: {n} objects").format(
                n=len(self._catalog.objs)))
        except Exception as e:
            self._cat_missing = True
            self.on_log(_("catalogue unavailable: {error}").format(error=e))
        return self._catalog

    # ================================================================== sensor
    def toggle_sensor(self, on: bool) -> None:
        if on:
            try:
                url = self.hs.start()
            except OSError as e:
                self.on_log(_("sensor: {error}").format(error=e))
                self.btn_sensor.setChecked(False)
                return
            self.btn_sensor.setText(_("Switch the sensor off"))
            self.lbl_url.setText(url)
            self.lbl_url.setVisible(True)
            self.lbl_qr.setPixmap(self._qr(url))
            self.lbl_qr.setVisible(True)
            self.lbl_sensor.setText(_("scan the QR code with the phone"))
            self.on_log(_("sensor at {url}").format(url=url))
        else:
            self.hs.stop()
            self.point.reset()
            self.btn_sensor.setText(_("Switch the sensor on"))
            self.lbl_qr.setVisible(False)
            self.lbl_url.setVisible(False)
            self.lbl_url.setText("")
            self.lbl_sensor.setText(_("off"))
            self.lbl_altaz.setText("—")
            self.btn_reset_align.setEnabled(False)

    def _qr(self, url: str) -> QPixmap:
        """QR code for the address — typing https://192.168.x.x:8443 on a phone
        keyboard, in the dark, is the kind of thing that makes people give up.

        Painted in the theme colours: in night mode a white QR on screen is a
        torch in your face, and the camera reads it just as well in red on black.
        """
        import cv2
        m = cv2.QRCodeEncoder.create().encode(url)
        n = 4
        big = np.kron(m, np.ones((n, n), dtype=np.uint8))
        big = np.pad(big, 4 * n, constant_values=255)
        light, dark = self._qr_colors()
        rgb = np.where(big[:, :, None] > 127, light, dark).astype(np.uint8)
        h, w = big.shape
        img = QImage(rgb.tobytes(), w, h, 3 * w, QImage.Format_RGB888)
        return QPixmap.fromImage(img)

    def _qr_colors(self) -> tuple[np.ndarray, np.ndarray]:
        from PySide6.QtGui import QColor
        a, b = QColor(self.pal.text), QColor(self.pal.bg)
        if a.valueF() < b.valueF():
            a, b = b, a
        return (np.array([a.red(), a.green(), a.blue()]),
                np.array([b.red(), b.green(), b.blue()]))

    @Slot(float, float, float, object, float)
    def _on_sample(self, a: float, b: float, g: float, c, t: float) -> None:
        self.point.feed(a, b, g, c, t)

    @Slot(int)
    def _on_handset_clients(self, n: int) -> None:
        # With the phone connected the QR code is no longer useful, and the
        # column is short: what stays on screen has to be what is still used.
        self.lbl_qr.setVisible(n == 0)
        self.lbl_url.setVisible(n == 0)
        if n == 0:
            self.lbl_sensor.setText(_("phone disconnected — scan the QR again"))

    def _sensor_state_text(self) -> str:
        p = self.point
        if p.aligned:
            return (_("aligned on {star}").format(star=p.star.label)
                    if p.star is not None else _("aligned"))
        return (_("compass — not aligned yet") if p.compass is not None
                else _("no compass — not aligned yet"))

    def _sensor_tick(self) -> None:
        """The sensor loop: 10 Hz, independent of the frame.

        Runs in every mode — the alignment is not lost when you go and focus —
        but only touches the FRAME screen, which is where those widgets live.
        """
        if not self.hs.running:
            return
        p = self.point
        # The site is editable on screen; without this the sensor would keep
        # computing with the coordinates from when the window opened.
        p.lat, p.lon = self.sp_lat.value(), self.sp_lon.value()
        p.elevation_m = self.sp_elev.value()
        if not p.live:
            if p.age > 3.0:
                self.lbl_sensor.setText(
                    _("no reading — the phone screen went dark or the page "
                      "closed"))
            return

        alt, az = p.altaz
        self.lbl_altaz.setText(
            f"alt {alt:+5.1f}°   az {az:5.1f}°  {compass_point(az)}")
        self.lbl_sensor.setText(self._sensor_state_text())

        now = time.monotonic()
        if now - getattr(self, "_hs_echo", 0.0) > 0.5:
            self._hs_echo = now
            self.hs.send({"alt": alt, "az": az})
        if now - getattr(self, "_goto_t", 0.0) > 0.2:
            self._goto_t = now
            self._update_goto()
        if now - getattr(self, "_field_t", 0.0) > 2.0:
            self._field_t = now
            self._objects_in_field()
        if self._view == "map":
            self._update_map()
            self._update_view_label()

    def reset_align(self) -> None:
        """Return to the unaligned state, manual sky nudge included.

        It exists because aligning on the wrong star is easy in the dark:
        clicking the right one would already replace the alignment, but only if
        you know which is right — if you are unsure, the way out is to clear it
        and start again from the map.
        """
        self.point.reset()
        self.lbl_sensor.setText(self._sensor_state_text())
        self.btn_reset_align.setEnabled(False)
        if self._view == "map":
            self._update_map()
        self._update_view_label()
        self._update_goto()
        self.on_log(_("alignment cleared"))

    def clear_target(self) -> None:
        self._target = None
        self.ed_goto.clear()
        self.lbl_goto.setText(_("no target"))
        self.lbl_goto_arrow.setText("")
        self.lbl_goto_dir.setFont(T_BODY())
        self.lbl_goto_dir.setText(_("choose a target in TARGETS (2)"))
        self.btn_clear_target.setEnabled(False)
        self._refresh_align_pick(force=True)
        if self._view == "map":
            self._update_map()
        self.on_log(_("target forgotten"))

    def _objects_in_field(self) -> None:
        """What the sensor says is inside the frame right now.

        Without a plate solver there is no field orientation — the sensor does
        not measure the tube's roll about its own sighting axis — so the
        question that can be answered is simpler: what falls inside a circle the
        size of the frame.
        """
        here = self._pos()
        cat = self._cat()
        if here is None or not cat:
            return
        radius = self._fov_deg() / 2.0
        near = cat.near(here[0], here[1], radius, limit=8)
        self.lbl_objects.setText(
            "\n".join(f"{o.label} — {o.kind_label}" for o in near)
            or _("nothing catalogued in the field"))

    # -------------------------------------------------------------------- map
    def _sky_marks(self) -> list:
        """Stars and catalogue objects as ENU vectors.

        Recomputed every 2 s: the sky moves 0.5' in that time, invisible on a
        map tens of degrees across, and recomputing at 10 Hz would burn 20 ms
        per second for nothing.
        """
        now = time.monotonic()
        if self._marks and now - self._marks_t < 2.0:
            return self._marks
        self._marks_t = now
        lat, lon = self.sp_lat.value(), self.sp_lon.value()
        elev = self.sp_elev.value()

        stars = brightstars.STARS
        vs = sky_vectors([s.ra for s in stars], [s.dec for s in stars],
                         lat, lon, elevation_m=elev)
        marks = [Mark(s.label, v, s.mag, "star", s)
                 for s, v in zip(stars, vs, strict=True) if v[2] > -0.09]

        cat = self._cat()
        if cat:
            if self._dsos is None:
                # The catalogue has 12 thousand objects and this is not an
                # armchair chart: above magnitude 10 none of it shows up in
                # short-exposure EAA.
                self._dsos = [o for o in cat.objs
                              if np.isfinite(o.mag) and o.mag <= 10.0]
            vs = sky_vectors([o.ra for o in self._dsos],
                             [o.dec for o in self._dsos], lat, lon,
                             elevation_m=elev)
            marks += [Mark(o.label.split(" (")[0], v, o.mag, "dso", o)
                      for o, v in zip(self._dsos, vs, strict=True) if v[2] > -0.09]
        ra, dec = constellations.endpoints()
        v = sky_vectors(ra, dec, lat, lon, elevation_m=elev)
        self._lines = v.reshape(-1, 2, 3)
        nv = sky_vectors([c[1] for c in constellations.NAMES],
                         [c[2] for c in constellations.NAMES], lat, lon,
                         elevation_m=elev)
        self._const_names = [
            (c[0], v2) for c, v2 in zip(constellations.NAMES, nv, strict=True)
            if v2[2] > 0.0]
        self._marks = marks
        return marks

    def _update_map(self) -> None:
        rays = self.point.rays()
        marks = self._sky_marks() if rays is not None else []
        lat, lon = self.sp_lat.value(), self.sp_lon.value()
        elev = self.sp_elev.value()
        target = None
        if self._target is not None:
            v = sky_vectors([self._target.ra], [self._target.dec],
                            lat, lon, elevation_m=elev)[0]
            target = Mark(getattr(self._target, "label", _("target")), v,
                          getattr(self._target, "mag", 99.0), "dso",
                          self._target)
            # Drop the copy that came from the catalogue: the target is drawn
            # from the chosen object itself, highlighted.
            base = target.label.split(" (")[0]
            marks = [m for m in marks
                     if m.label.split(" (")[0] != base] + [target]
        self.skymap.found = None
        if self._found is not None:
            v = sky_vectors([self._found.ra], [self._found.dec],
                            lat, lon, elevation_m=elev)[0]
            name = self._found.label.split(" (")[0]
            kind = "star" if isinstance(self._found, brightstars.Star) else "dso"
            m = Mark(name, v, getattr(self._found, "mag", 9.0), kind, self._found)
            self.skymap.found = m
            # An object too faint to be drawn still has to appear when you
            # search for it by name.
            if not any(x.label == name for x in marks):
                marks = [*marks, m]
        w = self._q.shape[1] if self._q is not None else 1920
        h = self._q.shape[0] if self._q is not None else 1080
        e = self.pixel_scale() / 3600.0
        self.skymap.cam_fov = (e * w, e * h)
        anchor = self.point.star.label if self.point.star is not None else ""
        self.skymap.set_state(rays, marks, target, self.point.aligned,
                              self.point.live, self._lines, self._const_names,
                              anchor)

    def _drag_sky(self, degrees: float) -> None:
        if self.point.aligned:
            return          # once aligned, the sky is not dragged by hand
        self.point.nudge_az(degrees)

    # ---------------------------------------------------------------- search
    def _build_completer(self) -> None:
        """Feed the map's search box with everything that has a name.

        Built once, on the first opening of the map: there are ~13 thousand
        labels, and doing it when the window opens would delay startup for a
        feature many nights never use.
        """
        if self._completer is not None:
            return
        names = [s.full for s in brightstars.STARS]
        cat = self._cat()
        if cat:
            names += [o.label for o in cat.objs]
        # Alphabetical order makes the shortest match rise: typing "m8" opens
        # the list on "M8 (Lagoon Nebula)" rather than "M81", which is what
        # someone who typed two characters expects.
        names.sort(key=str.lower)
        c = QCompleter(names, self)
        c.setCaseSensitivity(Qt.CaseInsensitive)
        # "contains", not "starts with": nobody remembers whether it is
        # "NGC 5139" or "Omega Centauri", and searching "centauri" has to find
        # both.
        c.setFilterMode(Qt.MatchContains)
        c.setMaxVisibleItems(8)
        self._completer = c
        self.skymap.search.setCompleter(c)
        self._style_completer()

    def _style_completer(self) -> None:
        if self._completer is None:
            return
        p = self.pal
        self._completer.popup().setStyleSheet(
            f"background: {p.surface2}; color: {p.text}; "
            f"border: 1px solid {p.border}; selection-background-color: "
            f"{p.accent}; selection-color: {p.bg}; padding: 2px;")

    def search(self, text: str) -> None:
        """The map's search box, and the action it implies.

        Searching a star by name only makes sense in two situations: you are
        sighting it and want to align, or you want to know where it is. Marking
        it and doing nothing serves only the second, and someone who types the
        name and presses Enter expects the first. So **a star aligns** and **a
        catalogue object becomes the target** — the map highlights it either way.

        The map does not move: it follows the tube, and shifting it on its own
        is disorienting. If the hit falls outside the field, the border arrow
        says which way it is.
        """
        text = (text or "").strip()
        if not text:
            self._found = None
            return
        cat = self._cat()
        obj = brightstars.find(text) or (cat.find(text) if cat else None)
        if obj is None and " (" in text:
            # Came from the suggestion list, in the form "Antares (α Sco)".
            short = text.split(" (")[0]
            obj = brightstars.find(short) or (cat.find(short) if cat else None)
        if obj is None:
            self.on_log(_("'{text}' not found").format(text=text))
            self._found = None
            return
        self._found = obj
        v = sky_vectors([obj.ra], [obj.dec], self.sp_lat.value(),
                        self.sp_lon.value(),
                        elevation_m=self.sp_elev.value())[0]
        alt = float(np.degrees(np.arcsin(np.clip(v[2], -1, 1))))
        az = float(np.degrees(np.arctan2(v[0], v[1])) % 360.0)
        where = (_("{alt:.0f}° up, {dir}").format(alt=alt,
                                                  dir=compass_point(az))
                 if alt > 0
                 else _("below the horizon ({alt:.0f}°)").format(alt=alt))
        self.on_log(f"{obj.label} — {where}")
        self._update_map()

        if isinstance(obj, brightstars.Star):
            self.align_on(obj)
        else:
            self.ed_goto.setText(text)
            self.set_goto()

    def align_on(self, star) -> None:
        p = self.point
        if not p.live:
            self.on_log(_("no sensor reading — is the phone connected?"))
            return
        tremor = p.steadiness()
        if tremor > 0.3:
            self.on_log(_("the tube is moving ({deg:.1f}° in the last half "
                          "second) — wait for it to settle and click again"
                          ).format(deg=tremor))
            return
        al = p.align_on(star)
        if al is None:
            self.on_log(_("could not align: no recent samples"))
            return
        self.lbl_sensor.setText(self._sensor_state_text())
        self.btn_reset_align.setEnabled(True)
        if self._view == "map":
            self._update_map()
        self._update_view_label()
        self._update_goto()
        self.on_log(_("aligned on {star}").format(star=star.full))

    def set_goto(self) -> None:
        text = self.ed_goto.text()
        cat = self._cat()
        o = cat.find(text) if cat else None
        if not o:
            # Bright stars work as targets too: they are the natural stepping
            # stone of star hopping, and they are what the sensor aligns on.
            o = brightstars.find(text)
        if not o:
            self.lbl_goto.setText(_("'{text}' not found").format(text=text))
            return
        self._apply_target(o)

    def _apply_target(self, o) -> None:
        """Adopt an object as the target, wherever it was chosen — the search
        box, the map or the ranked list."""
        self._target = o
        self.btn_clear_target.setEnabled(True)
        self.lbl_goto.setText(
            f"{o.label}\n{o.kind_label} · RA {o.ra:.3f}° Dec {o.dec:+.3f}°")
        self._update_goto()
        # The best alignment star is the one near where you are going, so the
        # answer changes with the target.
        self._refresh_align_pick(force=True)

    def _pos(self) -> tuple[float, float] | None:
        """Where the tube points, according to the phone sensor."""
        if self.point.aligned and self.point.live:
            return self.point.radec
        return None

    def _fov_deg(self) -> float:
        """The longer side of the field, in degrees, from the optics and binning."""
        return self._fov_arcmin()[0] / 60.0

    def _update_goto(self) -> None:
        if self._target is None:
            return
        here = self._pos()
        if here is None:
            self.lbl_goto_dir.setFont(T_BODY())
            self.lbl_goto_dir.setText(
                _("align the sensor to compute the direction"))
            return
        g = guide(here, (self._target.ra, self._target.dec),
                  self.sp_lat.value(), self.sp_lon.value(),
                  elevation_m=self.sp_elev.value(), fov_deg=self._fov_deg())
        self.lbl_goto_dir.setFont(T_XL())
        self.lbl_goto_dir.setText(g.text)
        color = self.pal.ok if g.on_target else self.pal.text
        self.lbl_goto_dir.setStyleSheet(f"color: {color}")
        self.lbl_goto_arrow.setText(_arrow(g))
        self.lbl_goto_arrow.setStyleSheet(f"color: {color}")

    # =================================================================== state
    def _set_state(self, state: str) -> None:
        self._state = state
        p = self.pal
        spec = {"idle": (_("IDLE"), p.idle),
                "framing": (_("FRAMING"), p.accent),
                "live": (_("LIVE"), p.accent),
                "exposing": (_("EXPOSING"), p.accent),
                "integrating": (_("INTEGRATING"), p.ok),
                "replay": (_("REPLAY"), p.accent),
                "ready": (_("READY"), p.accent),
                "paused": (_("PAUSED"), p.warn),
                "stopping": (_("STOPPING"), p.warn),
                "error": (_("ERROR"), p.bad)}
        label, color = spec.get(state, (state.upper(), p.text))
        self.st_state.set(f"● {label}", color)

    def _on_tick(self) -> None:
        """The exposure bar: without it the program looks frozen during a 10 s
        sub. It is also where the alerts are evaluated."""
        # The suggestions follow the clock while "now" is on. Once a minute is
        # plenty: the sky turns 0.25° in that time and the ranking's altitude
        # factor moves ~1% per degree. It lives here, not in the sensor tick,
        # because choosing a target does not require the phone to be connected.
        if (self._mode == "targets" and self.btn_now.isChecked()
                and time.monotonic() - self._targets_t > 60.0):
            self._refresh_targets()
        if self._mode == "frame":
            self._refresh_align_pick()        # throttled to 30 s inside
        if self.worker is None:
            self.prog.setValue(0)
            self.phase.setText(_("EXPOSURE"))
            if self._state != "error":
                self.alert.setVisible(False)
            return
        if self._paused:
            self.prog.setValue(0)
            self.phase.setText(_("PAUSED"))
            return
        elapsed = time.time() - self._t_frame
        exp = max(self._exposure, 0.001)
        if elapsed <= exp:
            self.prog.setValue(int(min(elapsed / exp, 1.0) * 100))
            self.phase.setText(_("EXPOSING  {elapsed:.1f} / {total:.1f}s").format(
                elapsed=elapsed, total=exp))
        else:
            self.prog.setValue(100)
            over = elapsed - exp
            self.phase.setText(
                _("READING AND PROCESSING  +{over:.1f}s").format(over=over)
                if over < 8
                else _("WAITING FOR A FRAME  +{over:.0f}s").format(over=over))
        self._check_alerts()

    def _check_alerts(self) -> None:
        p, st = self.pal, self._last_stats
        msgs = []
        streak = self.health.streak()
        if streak >= 5:
            msgs.append((_("{n} frames rejected in a row — cloud, dew or the "
                           "target left the frame").format(n=streak), p.bad))
        cool = self._cool
        if cool.get("phase") == "saturated":
            msgs.append((cool.get("message", _("cooler saturated")), p.bad))
        delta = cool.get("dark_delta")
        if delta is not None and abs(delta) > 3.0:
            msgs.append((_("dark taken {delta:+.1f} C away from the current "
                           "temperature — thermal residual in the stack"
                           ).format(delta=delta), p.warn))
        plat = st.get("platform", {})
        useful = plat.get("useful_s", float("inf"))
        integ = st.get("integration", 0.0)
        if np.isfinite(useful) and integ > useful:
            msgs.append((_("integration passed the rotation budget ({min:.0f} "
                           "min): the corner stars are already trailing"
                           ).format(min=useful / 60), p.warn))
        if msgs:
            text, color = msgs[0]
            self.alert.setText("⚠  " + text)
            self.alert.setStyleSheet(f"color: {color}")
            self.alert.setVisible(True)
        else:
            self.alert.setVisible(False)

    # =================================================================== slots
    @Slot(dict)
    def on_opened(self, info: dict) -> None:
        lo, hi = info.get("gain_range", (0, 570))
        self.sp_gain.setRange(lo, hi)
        has_cooler = bool(info.get("cooler"))
        self.btn_cooler.setEnabled(has_cooler)
        self.sp_temp.setEnabled(has_cooler)
        if not has_cooler:
            self.lbl_cool.setText(_("camera without cooling"))
        self._set_state("replay" if not info.get("live", True) else "exposing")
        self.on_log(f"{info['name']}  {info.get('port','')}  "
                    f"{info['width']}x{info['height']} bin{info['bin']}  "
                    f"Bayer {info['bayer']}  "
                    + _("scale 0..{full}").format(full=info['full_scale']))

    @Slot(object, object, object, dict)
    def on_frame(self, live, stack, cfa, st: dict) -> None:
        self._live = live
        if not st.get("n_stacked"):
            # A reset creates a new stacker and the worker starts emitting
            # stack=None. Without discarding here, the last accumulated image
            # would stay on screen and "reset" would look like it did nothing.
            self._stack = None
        elif stack is not None:
            self._stack = stack
        self._last_stats = st
        self._t_frame = time.time()
        if "exposure" in st:
            self._exposure = st["exposure"]
        acc = st.get("accepted")
        if st.get("stacking") and acc is not None:
            info = {
                "index": st.get("frame_index"),
                "reason": st.get("reason"),
                "n_stars": st.get("n_stars"),
                "hfr": st.get("hfr"),
                "fwhm": st.get("fwhm"),
                "elongation": st.get("elong"),
                "halo": st.get("halo"),
                "weight": st.get("weight"),
                # The thresholds in force, so each measurement comes with its
                # ruler: "elongation 2.03" explains nothing on its own.
                "limits": st.get("limits") or {},
                "time": time.strftime("%H:%M:%S"),
            }
            self.health.push(bool(acc), info)
            # Archive the mosaic, not the RGB: 5.8 MB against 35 MB per frame at
            # bin2, within the same memory budget. See ui/history.py.
            self._hist.push(st.get("frame_index"), cfa, st.get("bayer"), info,
                            accepted=bool(acc))
            self._set_state("integrating" if st.get("n_stacked") else "exposing")
        elif st.get("can_integrate"):
            # In Integrate mode but not integrating yet: the state has to make
            # that obvious, otherwise you think you are accumulating and are not.
            self._set_state("ready")
        else:
            self._set_state("framing" if st.get("mode") == "frame" else "live")

        p = self.pal
        self.st_integ.set(_hms(st.get("integration", 0.0)))
        n_ok, n_bad = st.get("n_stacked", 0), st.get("n_rejected", 0)
        self.st_frames.set(f"{n_ok}·{n_bad}",
                           p.bad if self.health.streak() >= 3 else None)
        fw = st.get("fwhm", float("nan"))
        if acc is False:
            self.on_log(_("rejected: {reason} ({n} stars, FWHM {fwhm:.2f})"
                          ).format(reason=st.get("reason", "?"),
                                   n=st.get("n_stars", 0), fwhm=fw))
        if st.get("recording"):
            self.lbl_disk.setText(st["recording"])
            self.lbl_disk.setVisible(True)
        self.lbl_advice.setText(st.get("platform_advice", "—"))
        rej = st.get("rejections") or {}
        if rej:
            total = sum(rej.values())
            lines = [f"{v:3d}  {kind_label(k)}"
                     for k, v in sorted(rej.items(), key=lambda kv: -kv[1])]
            self.lbl_rej.setText("\n".join(lines) + f"\n{'—' * 12}\n"
                                 + f"{total:3d}  " + _("total"))
        else:
            self.lbl_rej.setText(_("none"))
        plat = st.get("platform", {})
        rot = plat.get("rotation_deg_min", float("nan"))
        self.st_rot.set(f"{rot:+.4f}°/min" if np.isfinite(rot) else "—")
        u = plat.get("useful_s", float("inf"))
        self.st_useful.set(_("no limit") if not np.isfinite(u)
                           else _("{min:.0f} min").format(min=u / 60))
        sm = plat.get("corner_smear_px_min", float("nan"))
        self.st_smear.set(f"{sm:.2f} px/min" if np.isfinite(sm) else "—")
        self._update_view_label()
        self._render(True)
        self._update_goto()

    @Slot(object, dict)
    def on_focus(self, crop, d: dict) -> None:
        """The HFR reaches the vitals bar in every mode; the crop only reaches
        the loupe when it is open — magnifying an image nobody is looking at
        costs a percentile and a texture upload per frame."""
        hfr = d.get("hfr", float("nan"))
        ratio = d.get("ratio", float("nan"))
        p = self.pal
        color = None
        if np.isfinite(ratio):
            color = p.ok if ratio < 1.05 else (p.warn if ratio < 1.3 else p.bad)
        self.st_hfr.set(f"{hfr:.2f}" if np.isfinite(hfr) else "—", color)
        if self._loupe_on:
            self.loupe.set_focus(crop, d, p)
        # The beep follows its own checkbox rather than the mode: focusing by
        # ear is exactly what you do when you are not looking at the screen.
        self.beeper.beep_ratio(ratio)

    @Slot(str)
    def on_flat_saved(self, path: str) -> None:
        self._flat_path = path
        self.lbl_flat.setText(_("flat: {name}").format(name=Path(path).name))

    @Slot(str)
    def on_dark_saved(self, path: str) -> None:
        self._dark_path = path
        self.lbl_dark.setText(_("dark: {name}").format(name=Path(path).name))

    @Slot(dict)
    def on_cooling(self, st: dict) -> None:
        self._cool = st
        p = self.pal
        phase = st.get("phase", "off")
        color = {"stable": p.ok, "cooling": p.accent, "warming": p.warn,
                 "saturated": p.bad}.get(phase)
        current, power = st.get("current"), st.get("power", 0)
        eta = st.get("eta_s", float("nan"))
        txt = f"{current:+.1f} °C · TEC {power}%"
        if phase == "stable":
            txt += " · " + _("stable")
        elif phase in ("cooling", "warming") and np.isfinite(eta):
            txt += " · " + (_("cooling") if phase == "cooling"
                            else _("warming up"))
            txt += f" ~{eta/60:.0f} min"
        elif phase == "saturated":
            txt += " · " + _("target unreachable")
        delta = st.get("dark_delta")
        if delta is not None and abs(delta) > 2.0:
            txt += " · " + _("dark {delta:+.1f} C off").format(delta=delta)
        self.lbl_cool.setText(txt)
        self.lbl_cool.setStyleSheet(f"color: {color}" if color else "")

    @Slot(bool)
    def on_paused(self, on: bool) -> None:
        self._paused = on
        self.btn_pause.setText(_("Resume") if on else _("Pause"))
        self._ic(self.btn_pause, "play" if on else "pause")
        # On resume the state would only be corrected on the next frame — which
        # can be ten seconds away on a long sub, and until then the label lies.
        self._set_state("paused" if on else "exposing")

    @Slot(str)
    def on_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    @Slot(str)
    def on_failed(self, msg: str) -> None:
        self.on_log(f"ERROR: {msg}")
        self._set_state("error")
        self.alert.setText(f"⚠  {msg}")
        self.alert.setStyleSheet(f"color: {self.pal.bad}")
        self.alert.setVisible(True)
        self.btn_log.setChecked(True)

    @Slot()
    def on_finished(self) -> None:
        if self.thread:
            self.thread.quit()
            self.thread.wait(3000)
        self.worker = None
        self.thread = None
        self._paused = False
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText(_("Pause"))
        self._ic(self.btn_pause, "pause")
        self.btn_finish.setEnabled(False)
        self.btn_finish.setText(_("Finish"))
        if self.btn_integrate.isChecked():
            self.btn_integrate.setChecked(False)
        self._set_state("idle")
        self.on_log(_("session ended"))

    # ================================================================== review
    def _show_frame(self, i: int) -> None:
        """Freeze the view on the frame of the clicked health-strip mark.

        The strip says "frame 37 went out on trailing"; the next decision —
        change the strictness, refocus, wait for the cloud, or nothing — is
        taken by looking at the frame, not at the number. While the review is on,
        new frames keep arriving and stacking; only the screen holds still.
        """
        info = self.health.info[i] if 0 <= i < len(self.health.info) else {}
        idx = info.get("index")
        e = self._hist.get(idx)
        if e is None:
            self.on_log(_("frame {index} is no longer in memory — the history "
                          "keeps the last {n} ({mb:.0f} MB)").format(
                              index=idx if idx else "?", n=len(self._hist),
                              mb=self._hist.mb))
            return
        if self._view == "map":
            # A review is an image; the map has nowhere to show a frame.
            self._set_view("live")
        self._revisit = {"i": i, "index": e.index, "info": e.info,
                         "ok": bool(self.health.marks[i]), "rgb": e.rgb()}
        self.health.select(i)
        self._update_view_label()
        self._render(True)

    def _exit_review(self) -> None:
        """Back to the current frame. This is Esc, and any change of view."""
        if self._revisit is None:
            return
        self._revisit = None
        self.health.select(None)
        self._update_view_label()
        self._render(True)

    # ================================================================== render
    def _set_view(self, which: str) -> None:
        # Changing view is asking for what is happening now: leave the review.
        self._revisit = None
        self.health.select(None)
        self._view = which
        self.btn_view_stack.setChecked(which == "stack")
        self.btn_view_live.setChecked(which == "live")
        self.btn_view_map.setChecked(which == "map")
        self.canvas.setCurrentIndex({"map": 1, "targets": 2}.get(which, 0))
        # The loupe magnifies the last frame: over the map or the list there is
        # nothing for it to magnify.
        self.loupe.setVisible(self._loupe_on)
        if self._loupe_on:
            self.loupe.place()
        if which == "map":
            self._build_completer()
            self._update_map()
        self._update_view_label()
        self._render(True)

    def toggle_view(self) -> None:
        # The map and the target list stay out of the V cycle: switching
        # stack/frame is a checking gesture, opening either of the others
        # changes what you are doing.
        self._set_view("live" if self._view in ("stack", "map", "targets")
                       else "stack")

    def _update_view_label(self) -> None:
        st = self._last_stats
        p = self.pal
        if self._revisit is not None:
            d, ok = self._revisit["info"], self._revisit["ok"]
            txt = _("REVIEWING #{index}").format(index=self._revisit["index"])
            txt += " · " + (_("accepted") if ok
                            else _("rejected — {reason}").format(
                                reason=d.get("reason", "?")))
            if d.get("n_stars") is not None:
                txt += " · " + _("{n} stars").format(n=d["n_stars"])
            # The measurements with their rulers alongside, right on the bar:
            # without this, knowing why THIS frame passed and the other did not
            # meant hovering the strip and comparing from memory.
            txt += _criteria_inline(d)
            txt += "   ·   " + _("Esc returns to live")
            self.lbl_view.setText(txt)
            self.lbl_view.setStyleSheet(f"color: {p.ok if ok else p.bad}")
            return
        if self._view == "targets":
            n = self.targets.rowCount()
            when = self._targets_when()
            txt = _("{n} suggestions for {when}").format(
                n=n, when=when.strftime("%d/%m %H:%M"))
            if self._sky is not None:
                txt += " · " + self._sky.moon_text()
                twilight = self._sky.twilight_text()
                if twilight:
                    txt += " · " + twilight
            self.lbl_view.setText(txt)
            self.lbl_view.setStyleSheet(
                "" if self._sky is None or self._sky.dark
                else f"color: {p.warn}")
            return
        if self._view == "map":
            pt = self.point
            if not pt.live:
                txt, col = _("map · no sensor reading"), p.warn
            else:
                alt, az = pt.altaz
                txt = _("map · alt {alt:+.1f}° az {az:.1f}°").format(alt=alt,
                                                                     az=az)
                col = p.ok if pt.aligned else p.warn
                if pt.aligned and pt.star is not None:
                    txt += " · " + _("aligned on {star}").format(
                        star=pt.star.label)
                elif not pt.aligned:
                    txt += " · " + _("not aligned")
            self.lbl_view.setText(txt)
            self.lbl_view.setStyleSheet(f"color: {col}" if col else "")
            return
        if self._view == "live":
            acc = st.get("accepted")
            if acc is None:
                txt, col = _("current frame, not stacking"), None
            elif acc:
                txt = _("frame accepted · {n} stars").format(
                    n=st.get("n_stars", 0))
                col = p.ok
            else:
                txt = _("frame REJECTED · {reason}").format(
                    reason=st.get("reason", "?"))
                col = p.bad
            n = st.get("frame_index")
            if n:
                txt = f"#{n} · " + txt
        else:
            if self._stack is None:
                txt, col = _("no stack yet"), None
            else:
                txt = _("{n} frames · {time} of integration").format(
                    n=st.get("n_stacked", 0),
                    time=_hms(st.get("integration", 0.0)))
                col = None
        self.lbl_view.setText(txt)
        self.lbl_view.setStyleSheet(f"color: {col}" if col else "")

    def _src(self):
        if self._revisit is not None:
            return self._revisit["rgb"]
        if self._view == "live":
            return self._live
        return self._stack if self._stack is not None else self._live

    def _prepared(self):
        """The array to display, with the gradient already removed if asked.

        Cached by (source, enabled, degree): removing the gradient costs tens of
        milliseconds and cannot run on every slider move.
        """
        src = self._src()
        if src is None:
            return None
        key = (self.chk_bg.isChecked(), self.sp_bg_deg.value())
        # Compare the array by identity and hold the reference, rather than
        # keying on id(): CPython reuses the address of a freed array, and a new
        # frame would inherit the previous stack's preparation.
        if (self._prep_key == key and self._prep is not None
                and self._prep_src is src):
            return self._prep
        out = src
        if self.chk_bg.isChecked():
            out, ok = background.remove(src, degree=self.sp_bg_deg.value(),
                                        grid=12)
            self.lbl_bg.setText(_("applied") if ok else _("not enough samples"))
        else:
            self.lbl_bg.setText("")
        self._prep, self._prep_key, self._prep_src = out, key, src
        return out

    def _quantize(self, src) -> None:
        if self._q_src is src and self._q is not None:
            return
        self._q = (np.clip(src, 0.0, 1.0) * 65535.0 + 0.5).astype(np.uint16)
        self._q_src = src

    def _channel_gains(self) -> list[float]:
        return [sl.value() / sl._div for sl in (self.sl_r, self.sl_g, self.sl_b)]

    def equalize_channels(self) -> None:
        """Gains that match the channel medians, with green as the reference.

        The sky is the grey surface always in the frame: matching the three
        medians is a white balance measured on what is there, rather than
        guessing on a slider. Green stays at 1.00 because on a Bayer sensor it
        has twice the pixels and is the least noisy of the three.
        """
        src = self._prepared()
        if src is None or src.ndim != 3:
            self.on_log(_("no colour image to equalise"))
            return
        med = [float(np.median(src[..., k])) for k in range(3)]
        if min(med) <= 0:
            self.on_log(_("a channel has no signal — nothing to equalise"))
            return
        # Manual balance only makes sense with the stretch linked across
        # channels: unlinked, each channel is normalised on its own and undoes
        # what the gain just did.
        if not self.chk_linked.isChecked():
            self.chk_linked.setChecked(True)
            self.on_log(_("stretch linked across channels — that is what makes "
                          "the per-channel gain visible"))
        for sl, m in zip((self.sl_r, self.sl_g, self.sl_b), med, strict=True):
            sl.setValue(int(round(np.clip(med[1] / m, 0.3, 3.0) * 100)))
        self.on_log(_("gains: {values}").format(
            values="  ".join(
                f"{c}={g:.2f}"
                for c, g in zip("RGB", self._channel_gains(), strict=True))))

    def _stretch(self) -> np.ndarray:
        q, src = self._q, self._prepared()
        target = self.sl_bg.value() / self.sl_bg._div
        clip = -self.sl_clip.value() / self.sl_clip._div
        sat = self.sl_sat.value() / self.sl_sat._div
        white = self.sl_white.value() / self.sl_white._div
        gains = self._channel_gains()

        if not self.chk_auto.isChecked():
            out = (q >> 8).astype(np.uint8)
        elif self.cb_algo.currentIndex() == 1:
            # Colour-preserving arcsinh couples the channels, so it does not fit
            # in a per-channel LUT and the gain has to be applied to the image.
            base = src
            if src.ndim == 3 and any(abs(g - 1.0) > 1e-3 for g in gains):
                base = np.clip(src * np.array(gains, np.float32), 0.0, 1.0)
            img = stretch.auto_arcsinh(base, target_bg=target,
                                       shadows_clip=clip, preserve_color=True)
            src = base
            self._black, _m = stretch.estimate_params(
                src.mean(axis=2) if src.ndim == 3 else src, target, clip, 8)
            out = stretch.to_uint8(stretch.saturate(img, sat))
        else:
            out = np.empty(q.shape, np.uint8)
            if self.chk_linked.isChecked():
                c0, m = stretch.estimate_params(src.mean(axis=2), target, clip, 8)
                for k in range(3):
                    out[..., k] = stretch.build_lut(c0, m, white,
                                                    gains[k])[q[..., k]]
                self._black = c0
            else:
                blacks = []
                for k in range(3):
                    # The channel enters the estimate with its gain already
                    # applied: measuring the shadow clip on the ungained signal
                    # and applying it to the gained one cuts in the wrong place.
                    channel = (src[..., k] * gains[k]
                               if abs(gains[k] - 1) > 1e-3 else src[..., k])
                    c0, m = stretch.estimate_params(channel, target, clip, 8)
                    out[..., k] = stretch.build_lut(c0, m, white,
                                                    gains[k])[q[..., k]]
                    blacks.append(c0)
                self._black = float(np.mean(blacks))
            if abs(sat - 1.0) > 1e-3:
                out = stretch.to_uint8(
                    stretch.saturate(out.astype(np.float32) / 255.0, sat))

        lut = image_lut(self.pal)
        if lut is not None:
            out = lut[out.mean(axis=2).astype(np.uint8)]
        return out

    def _display(self) -> np.ndarray:
        src = self._prepared()
        if src is None:
            return np.zeros((1, 1, 3), np.uint8)
        self._quantize(src)
        return self._stretch()

    def _render(self, force: bool = False) -> None:
        src = self._prepared()
        if src is None:
            return
        self._quantize(src)
        self.img.setImage(self._stretch(), autoLevels=False, levels=(0, 255))
        if force:
            self._draw_histogram(src)
            self._update_scale_label()

    def _draw_histogram(self, src) -> None:
        """Per-channel histogram, sampled every 8th pixel.

        Sampling is what allows redrawing on every frame: a full bin2 frame has
        2.9 million pixels per channel, and nobody sees the difference in a
        256-bin histogram.
        """
        sample = src[::8, ::8]
        # With the gain applied: that is what makes the three curves move when
        # you touch the balance, which is the point of per-channel gain. The
        # stretch is deliberately left out — it would compress everything and
        # the histogram would stop being useful for judging exposure.
        gains = self._channel_gains()
        if sample.ndim == 3:
            channels = [sample[:, :, k] * gains[k]
                        for k in range(min(3, sample.shape[2]))]
        else:
            channels = [sample]
        top = max(max(float(c.max()) for c in channels), 1e-4)
        white = self.sl_white.value() / self.sl_white._div
        for lw in (self.hist_white, self.hist2_white):
            lw.setValue(white)
            lw.setVisible(white < top)
        for curves, line in ((self.hist_rgb, self.hist_black),
                             (self.hist2_rgb, self.hist2_black)):
            for k, curve in enumerate(curves):
                if k >= len(channels):
                    curve.setData([], [])
                    continue
                counts, edges = np.histogram(channels[k], bins=256,
                                             range=(0.0, top))
                curve.setData(edges[:-1], counts + 1)
            line.setValue(self._black)

    # ================================================================ settings
    def _restore(self) -> None:
        self.btn_night.setChecked(self._theme == "night")
        self.sl_night.setValue(self._night_level)
        self.chk_touch.setChecked(self._large_targets)
        self._exposure = self.sp_exp.value()
        self._update_scale_label()

    def closeEvent(self, ev) -> None:
        s = self.settings
        s.exposure_s = self.sp_exp.value()
        s.gain = self.sp_gain.value()
        s.offset = self.sp_offset.value()
        s.binning = int(self.cb_bin.currentText())
        s.latitude = self.sp_lat.value()
        s.longitude = self.sp_lon.value()
        s.elevation_m = self.sp_elev.value()
        s.focal_length_mm = self.sp_focal.value()
        s.pixel_size_um = self.sp_pixel.value()
        s.target_min_alt = self.sp_min_alt.value()
        s.target_max_mag = self.sp_max_mag.value()
        s.target_family = self.cb_family.currentData()
        s.target_fits_only = self.chk_fits.isChecked()
        s.previews_enabled = self.chk_previews.isChecked()
        try:
            s.save()
        except OSError:
            pass
        if self.worker:
            self.worker.stop()
        if self.thread:
            self.thread.quit()
            self.thread.wait(2000)
        self.hs.stop()
        self.preview_loader.shutdown()
        super().closeEvent(ev)


# -------------------------------------------------------------------- helpers
def _tag(text: str) -> QLabel:
    """A short label next to a field, at the width of its own text."""
    lab = QLabel(text)
    lab.setObjectName("statLabel")
    lab.setFont(T_SMALL())
    return lab


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setFont(T_SMALL())
    lab.setWordWrap(True)
    return lab


def _shorten(path: str, keep: int = 34) -> str:
    """Tail of a long path, so a folder row does not widen the column."""
    p = str(path)
    home = str(Path.home())
    if p.startswith(home):
        p = "~" + p[len(home):]
    return p if len(p) <= keep else "…" + p[-(keep - 1):]


def _arrow(g) -> str:
    """The push-to arrow: which way to push the tube.

    Two arrows when both corrections matter, one when the other is already
    within tolerance — with both always lit you cannot tell when one axis is done.
    """
    if g.target_alt_deg < 0:
        return "—"
    if g.on_target:
        return "✔"
    tol = 0.1
    vert = "↑" if g.delta_alt_deg > tol else ("↓" if g.delta_alt_deg < -tol else "")
    hori = "→" if g.delta_az_deg > tol else ("←" if g.delta_az_deg < -tol else "")
    return (hori + vert) or "·"


def _criteria_inline(d: dict) -> str:
    """`weight 0.67<0.40 · elongation 2.03>1.70 · FWHM 5.71/8.62`.

    `>` marks whatever exceeded its limit, `<` a weight below the minimum, `/`
    whatever passed. On one line because this is the image bar — the detail in
    columns is in the strip's tooltip.
    """
    limits = d.get("limits") or {}
    out = []
    for key, name in (("weight", "weight"), ("elongation", "elong"),
                      ("fwhm", "FWHM"), ("halo", "halo")):
        v, ceiling = d.get(key), limits.get(key)
        if (v is None or ceiling is None
                or not (np.isfinite(v) and np.isfinite(ceiling))):
            continue
        # Weight fails from below, a defect fails from above.
        if key == "weight":
            sign = "<" if v < ceiling else "/"
        else:
            sign = ">" if v > ceiling else "/"
        out.append(f"{name} {v:.2f}{sign}{ceiling:.2f}")
    return ("  ·  " + "  ·  ".join(out)) if out else ""


def _hms(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m{s%60:02d}"
    return f"{s//3600}h{(s%3600)//60:02d}"


def main() -> int:
    """Run the GUI. Rebuilds the window when the language changes."""
    app = QApplication.instance() or QApplication(sys.argv)
    # Without these, Qt falls back to the basename of argv[0] — which for a
    # console script or `python -m` is the interpreter, so the program reports
    # itself as "python3.14". This fixes every place Qt itself names the
    # application; the macOS Dock and menu bar read the name from a bundle
    # instead, which is what `make bundle` produces.
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME.lower())
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_NAME.lower())   # Wayland and Linux task bars
    app.setWindowIcon(app_icon())
    settings = Settings.load()
    set_language(settings.language)

    while True:
        win = MainWindow(settings)
        requested: list[str] = []

        def _switch(tag: str, window=win, sink=requested) -> None:
            sink.append(tag)
            window.close()

        win.language_changed.connect(_switch)
        win.show()
        code = app.exec()
        if not requested:
            return code
        # A language change rebuilds the whole window: Qt has no cheap way to
        # retranslate widgets that already exist, and rebuilding is honest.
        settings = Settings.load()
        set_language(requested[-1])
