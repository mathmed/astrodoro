from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import (
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
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
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
from ..core.catalog import Catalog
from ..drivers import list_cameras
from ..i18n import N_, set_language
from ..i18n import gettext as _
from ..pointing import Pointing, brightstars, constellations, sky_vectors
from ..pointing.handset import Handset
from ..settings import Settings
from . import icons
from .audio import Beeper
from .branding import app_icon
from .design import (
    T_BODY,
    T_H2,
    T_MONO,
    T_SMALL,
    T_XL,
    Card,
    ElidedLabel,
    HealthStrip,
    ModeRail,
    Palette,
    Stat,
    kind_label,
    label_font,
    palette,
    stylesheet,
    tag,
)
from .history import FrameHistory
from .panels.frame import REPLAY_SPEEDS, FramePanel
from .panels.integrate import IntegratePanel
from .panels.lucky import LuckyPanel
from .panels.targets import TargetsPanel
from .previews import PreviewLoader
from .skymap import Mark, SkyMap
from .targets import TargetTable
from .view import ImageView
from .windows.config import ConfigWindow
from .worker import CaptureWorker, Config

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)

APP_NAME = "Astrodoro"

MODES = [
    (
        "frame",
        N_("FRAME"),
        N_("find and centre the target — live frame, phone sensor and push-to"),
        "frame",
    ),
    (
        "targets",
        N_("TARGETS"),
        N_("what is worth imaging at this hour, ranked — altitude, Moon, size"),
        "list",
    ),
    (
        "stack",
        N_("DSO"),
        N_("deep sky: stack, record and follow the platform"),
        "stack",
    ),
    (
        "lucky",
        N_("PLANETS"),
        N_(
            "the Moon and the planets: nothing to stack — exposure guard, contrast "
            "focus and burst recording"
        ),
        "moon",
    ),
]

CONFIG_SHORTCUT = "Ctrl+,"

STRETCH_PRESETS = {
    N_("soft"): (0.15, 3.2),
    N_("medium"): (0.25, 2.8),
    N_("strong"): (0.40, 2.2),
}


SATURATED_FRACTION = 0.0005


class _WheelGuard(QObject):
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
    language_changed = Signal(str)

    def __init__(self, settings: Settings | None = None):
        super().__init__()
        self.setWindowTitle("Astrodoro")
        av = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(
            min(1500, int(av.width() * 0.98)), min(980, int(av.height() * 0.96))
        )

        self.settings = settings or Settings.load()
        self.worker: CaptureWorker | None = None
        self._thread: QThread | None = None
        self.beeper = Beeper()

        self._theme = self.settings.theme
        self._night_level = int(self.settings.night_level)
        self._large_targets = bool(self.settings.large_targets)
        self.pal: Palette = palette(self._theme, self._night_level)

        self._view = "stack"
        self._live = self._stack = None
        self._hist = FrameHistory()
        self._bias_path = None
        self._dark_path = None
        self._flat_path = None
        self._replay_folder = ""
        self._target = None
        self._catalog: Catalog | None = None
        self._cat_missing = False
        self.point = Pointing(
            self.settings.latitude, self.settings.longitude, self.settings.elevation_m
        )
        self.hs = Handset(port=self.settings.handset_port)
        self.hs.status.connect(self.on_log)
        self._found = None
        self._target_name = ""
        self.preview_loader = PreviewLoader(self)
        self._marks: list = []
        self._marks_t = 0.0
        self._lines: np.ndarray | None = None
        self._const_names: list = []
        self._dsos: list | None = None
        self._iconed: list[tuple] = []
        self._paused = False
        self._cool: dict = {}
        self._state = "idle"
        self._t_frame = 0.0
        self._exposure = self.settings.exposure_s
        self._last_stats: dict = {}
        self.lucky = LuckyPanel(self)
        self.targets_panel = TargetsPanel(self)
        self.integrate = IntegratePanel(self)
        self.frame = FramePanel(self)
        self.hs.sample.connect(self.frame._on_sample)
        self.hs.clients.connect(self.frame._on_handset_clients)
        self.preview_loader.ready.connect(self.targets_panel._preview_ready)
        self.preview_loader.failed.connect(self.targets_panel._preview_failed)

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

        self.tick_sensor = QTimer(self)
        self.tick_sensor.timeout.connect(self.frame.tick)
        self.tick_sensor.start(100)

    def pixel_scale(self, halved: bool = False) -> float:
        return self.settings.pixel_scale(int(self.cb_bin.currentText()), halved=halved)

    def _build(self) -> None:
        self.config_window = ConfigWindow(self)
        # Before the bars too: the health strip connects straight to it.
        self.image = ImageView(self)
        root = QWidget()
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 6)
        outer.setSpacing(8)

        outer.addWidget(self._top_bar())
        outer.addWidget(self._status_bar())
        self.alert = QLabel("")
        self.alert.setFont(T_H2())
        self.alert.setVisible(False)
        self.alert.setWordWrap(True)
        outer.addWidget(self.alert)

        self.split = QSplitter(Qt.Orientation.Horizontal)
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
        self.btn_log.setChecked(True)
        self.setCentralWidget(root)

    def _top_bar(self) -> QWidget:
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(4, 0, 2, 0)
        h.setSpacing(4)

        name = QLabel(APP_NAME.upper())
        name.setObjectName("sectionTitle")
        name.setFont(label_font())
        version = QLabel(__version__)
        version.setObjectName("statLabel")
        version.setFont(T_SMALL())
        h.addWidget(name)
        h.addWidget(version)
        h.addStretch(1)

        self.btn_night = self._top_toggle(
            _("night mode   (N)"), "moon", self.toggle_night
        )
        self.btn_full = self._top_toggle(
            _("image only   (F)"), "expand", self.toggle_full
        )
        self.btn_log = self._top_toggle(
            _("log   (L)"), "list", lambda on: self.log.setVisible(on)
        )
        for b in (self.btn_night, self.btn_full, self.btn_log):
            h.addWidget(b)

        self.btn_config = QPushButton(_("config"))
        self.btn_config.setObjectName("ghost")
        self._ic(self.btn_config, "adjust")
        self.btn_config.setAutoDefault(False)
        self.btn_config.setToolTip(
            _(
                "folders, observing site, optics and language, in a window of its "
                "own   ({key})"
            ).format(
                key=QKeySequence(CONFIG_SHORTCUT).toString(
                    QKeySequence.SequenceFormat.NativeText
                )
            )
        )
        self.btn_config.clicked.connect(self.config_window.open_it)
        h.addWidget(self.btn_config)
        return bar

    def _top_toggle(self, label: str, icon: str, slot) -> QPushButton:
        b = QPushButton(label)
        b.setObjectName("ghost")
        b.setCheckable(True)
        b.setAutoDefault(False)
        self._ic(b, icon)
        b.toggled.connect(slot)
        return b

    def _status_bar(self) -> QWidget:
        card = Card()
        top = QHBoxLayout()
        top.setSpacing(22)
        top.setContentsMargins(2, 0, 2, 0)

        self.st_state = Stat(_("state"), "● " + _("IDLE"), big=True, min_width=168)
        self.st_target = Stat(_("target"), "—", big=True, min_width=150)
        self.st_integ = Stat(_("integration"), "—", big=True)
        self.st_frames = Stat(_("frames"), "—", big=True)
        self.st_hfr = Stat(_("HFR"), "—", big=True, min_width=88)

        expo = QWidget()
        ev = QVBoxLayout(expo)
        ev.setContentsMargins(0, 1, 0, 1)
        ev.setSpacing(3)
        self.phase = ElidedLabel(
            _("EXPOSURE"), mode=Qt.TextElideMode.ElideMiddle, min_chars=6
        )
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

        for st in (
            self.st_state,
            self.st_target,
            self.st_integ,
            self.st_frames,
            self.st_hfr,
        ):
            top.addWidget(st)
        top.addWidget(expo)
        top.addStretch(1)

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
        self.btn_start.setToolTip(
            _(
                "Opens the camera. What happens to the frames depends on the mode:\n"
                "Frame and Focus only capture and measure stars; DSO stacks,\n"
                "records the subs and measures the residual rotation."
            )
        )
        self.btn_start.clicked.connect(self.start)

        self.btn_pause = QPushButton(_("Pause"))
        self._ic(self.btn_pause, "pause")
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip(
            _(
                "Stops pulling frames without closing the camera.\n"
                "The stack and the recording stay intact — Resume continues on the\n"
                "same accumulator."
            )
        )
        self.btn_pause.clicked.connect(self.toggle_pause)

        self.btn_finish = QPushButton(_("Finish"))
        self._ic(self.btn_finish, "stop")
        self.btn_finish.setEnabled(False)
        self.btn_finish.setToolTip(
            _(
                "Ends the session and closes the camera.\n"
                "The final stack is always written to disk before finishing."
            )
        )
        self.btn_finish.clicked.connect(self.finish)

        self.btn_reset = QPushButton(_("Reset"))
        self._ic(self.btn_reset, "trash")
        self.btn_reset.setToolTip(
            _(
                "Discards the accumulated integration. Subs "
                "already written to disk are not deleted."
            )
        )
        self.btn_reset.clicked.connect(self.integrate.reset_stack)

        self._session_buttons = (
            self.btn_start,
            self.btn_pause,
            self.btn_finish,
            self.btn_reset,
        )
        for b in self._session_buttons:
            b.setAutoDefault(False)
            b.setDefault(False)
            row.addWidget(b)
        bv.addLayout(row)
        bv.addStretch(1)
        top.addWidget(btns)

        card.add_layout(top)
        self.health = HealthStrip()
        self.health.chosen.connect(self.image.show_frame)
        self.health.has_image = self._hist.__contains__  # type: ignore[assignment]
        card.add(self.health)
        return card

    def _left(self) -> QWidget:
        col = QWidget()
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        self.rail = ModeRail([(k, _(lab), _(hint), ic) for k, lab, hint, ic in MODES])
        self.rail.changed.connect(self.set_mode)
        v.addWidget(self.rail)

        self.panels = QStackedWidget()
        self._panel_index = {}
        for key, build in (
            ("frame", self.frame.panel),
            ("targets", self.targets_panel.panel),
            ("stack", self.integrate.panel),
            ("lucky", self.lucky.panel),
        ):
            sa = QScrollArea()
            sa.setWidget(build())
            sa.setWidgetResizable(True)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            sa.setFrameShape(QScrollArea.Shape.NoFrame)
            self._panel_index[key] = self.panels.addWidget(sa)
        v.addWidget(self.panels, 1)

        v.addWidget(self._capture_strip())
        col.setMinimumWidth(348)
        col.setMaximumWidth(452)
        self._left_col = col
        return col

    def _capture_strip(self) -> QWidget:
        c = Card(_("capture"))
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        self.sp_exp = QDoubleSpinBox()
        self.sp_exp.setRange(0.0001, 600.0)
        self.sp_exp.setDecimals(4)
        self.sp_exp.setValue(self.settings.exposure_s)
        self.sp_exp.setSuffix(" s")
        self.sp_exp.valueChanged.connect(self._exposure_changed)
        self.sp_gain = QSpinBox()
        self.sp_gain.setRange(0, 570)
        self.sp_gain.setValue(self.settings.gain)
        self.sp_gain.valueChanged.connect(lambda x: self._req(gain=x))
        r1.addWidget(tag(_("exp")))
        r1.addWidget(self.sp_exp, 1)
        r1.addWidget(tag(_("gain")))
        r1.addWidget(self.sp_gain, 1)
        c.add_layout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.sp_offset = QSpinBox()
        self.sp_offset.setRange(0, 80)
        self.sp_offset.setValue(self.settings.offset)
        self.sp_offset.setToolTip(_("offset 0 truncates the left tail of the noise"))
        self.sp_offset.valueChanged.connect(lambda x: self._req(offset=x))
        self.cb_bin = QComboBox()
        self.cb_bin.addItems(["1", "2", "3", "4"])
        self.cb_bin.setCurrentText(str(self.settings.binning))
        self.cb_bin.setToolTip(
            _(
                "bin2 is the only bin >1 without loss on this camera;\n"
                "bin3 and bin4 clip the highlights"
            )
        )
        self.cb_bin.currentTextChanged.connect(self._bin_changed)
        r2.addWidget(tag(_("offset")))
        r2.addWidget(self.sp_offset, 1)
        r2.addWidget(tag(_("bin")))
        r2.addWidget(self.cb_bin, 1)
        c.add_layout(r2)

        r3 = QHBoxLayout()
        r3.setSpacing(6)
        self.btn_cooler = QPushButton(_("Cooler"))
        self.btn_cooler.setCheckable(True)
        self.btn_cooler.setMaximumWidth(88)
        self.btn_cooler.setToolTip(
            _(
                "Switches cooling on with a controlled ramp.\n"
                "Sending the final target in one go makes the TEC pull 100% and the\n"
                "temperature plunge, which stresses the sensor joint and encourages\n"
                "condensation."
            )
        )
        self.btn_cooler.toggled.connect(self._cooler_toggled)
        self.sp_temp = QDoubleSpinBox()
        self.sp_temp.setRange(-40, 30)
        self.sp_temp.setValue(-5)
        self.sp_temp.setSuffix(" °C")
        self.sp_temp.valueChanged.connect(lambda x: self._req(target_temp=x))
        r3.addWidget(self.btn_cooler)
        r3.addWidget(tag(_("target")))
        r3.addWidget(self.sp_temp, 1)
        c.add_layout(r3)
        self.lbl_cool = QLabel(_("cooler off"))
        self.lbl_cool.setFont(T_MONO())
        self.lbl_cool.setWordWrap(True)
        c.add(self.lbl_cool)
        return c

    def _right(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        v.addWidget(self.image.bar)

        self.skymap = SkyMap()
        self.skymap.align_requested.connect(lambda m: self.frame.align_on(m.obj))
        self.skymap.az_dragged.connect(self._drag_sky)
        self.skymap.searched.connect(self.targets_panel.search)

        self.targets = TargetTable()
        self.targets.chosen.connect(self.targets_panel._suggestion_chosen)
        self.targets.activated_target.connect(self.targets_panel.use_suggestion)

        self.canvas = QStackedWidget()
        self.canvas.addWidget(self.image)
        self.canvas.addWidget(self.skymap)
        self.canvas.addWidget(self.targets)
        v.addWidget(self.canvas, 1)

        self.context = QStackedWidget()
        self.context.setMaximumHeight(210)
        self._ctx_index = {
            "frame": self.context.addWidget(self.frame.context()),
            "targets": self.context.addWidget(self.targets_panel.context()),
            "stack": self.context.addWidget(self.integrate.context()),
            "lucky": self.context.addWidget(self.lucky.context()),
        }
        v.addWidget(self.context)
        return w

    def _slider(self, name, lo, hi, val, div):
        hold = QWidget()
        h = QHBoxLayout(hold)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(name)
        lab.setFont(T_BODY())
        lab.setMinimumWidth(96)
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(lo, hi)
        s.setValue(val)
        out = QLabel(f"{val / div:.2f}")
        out.setFont(T_MONO())
        out.setMinimumWidth(40)
        s.valueChanged.connect(
            lambda x: (out.setText(f"{x / div:.2f}"), self.image.redraw())
        )
        h.addWidget(lab)
        h.addWidget(s, 1)
        h.addWidget(out)
        s._div = div
        return s, hold

    def _shortcuts(self) -> None:
        def typing() -> bool:
            f = QApplication.focusWidget()
            return isinstance(f, (QLineEdit, QSpinBox, QDoubleSpinBox))

        def guard(fn):
            return lambda: None if typing() else fn()

        for i, mode in enumerate(MODES):
            QShortcut(
                QKeySequence(str(i + 1)),
                self,
                guard(lambda k=mode[0]: self.rail.select(k)),
            )
        for key, fn in (
            ("+", self.image.zoom_in),
            ("=", self.image.zoom_in),
            ("-", self.image.zoom_out),
            ("0", self.image.fit_view),
            ("V", self.toggle_view),
            ("Z", lambda: self.image.btn_loupe.toggle()),
            ("M", lambda: self._set_view("live" if self._view == "map" else "map")),
            ("F", lambda: self.btn_full.toggle()),
            ("N", lambda: self.btn_night.toggle()),
            ("L", lambda: self.btn_log.toggle()),
            (
                "A",
                lambda: (
                    self.integrate.btn_align.toggle() if self._mode == "stack" else None
                ),
            ),
            ("Escape", self.image.exit_review),
            (
                "R",
                lambda: (
                    self.lucky.btn_burst.toggle()
                    if self._mode == "lucky"
                    else (
                        self.integrate.btn_realign.toggle()
                        if self._mode == "stack"
                        else None
                    )
                ),
            ),
            ("Ctrl+S", self.image.save),
            (CONFIG_SHORTCUT, self.config_window.open_it),
        ):
            QShortcut(
                QKeySequence(key), self, fn if key.startswith("Ctrl") else guard(fn)
            )

    def _block_wheel(self) -> None:
        self._wheel_guard = _WheelGuard(self)
        for kind in (QAbstractSpinBox, QComboBox, QSlider):
            for w in self.findChildren(kind):
                w.installEventFilter(self._wheel_guard)
                w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _pin_button_widths(self) -> None:
        for b in getattr(self, "_session_buttons", ()):
            b.setMinimumWidth(0)
            b.setMinimumWidth(b.sizeHint().width())

    def _rgb_pens(self) -> list:
        p = self.pal
        if p.image_red:
            return [
                pg.mkPen(p.bad, width=1),
                pg.mkPen(p.warn, width=1, style=Qt.PenStyle.DashLine),
                pg.mkPen(p.text_dim, width=1, style=Qt.PenStyle.DotLine),
            ]
        return [
            pg.mkPen(p.bad, width=1),
            pg.mkPen(p.ok, width=1),
            pg.mkPen(p.accent, width=1),
        ]

    def _ic(self, widget, name: str, size: int = 17):
        self._iconed = [(w, n, sz) for w, n, sz in self._iconed if w is not widget]
        self._iconed.append((widget, name, size))
        widget.setIcon(icons.icon(name, self.pal.text, size))
        widget.setIconSize(QSize(size, size))
        return widget

    def _retint_icons(self) -> None:
        for widget, name, size in self._iconed:
            widget.setIcon(icons.icon(name, self.pal.text, size))
        self.rail.set_icon_provider(
            lambda n, checked: icons.icon(n, self.pal.bg if checked else self.pal.text)
        )

    def set_mode(self, key: str) -> None:
        self._mode = key
        self._req(mode=key)
        self.rail.mark(key)
        self.panels.setCurrentIndex(self._panel_index[key])
        self.context.setCurrentIndex(self._ctx_index[key])
        self._set_view(
            {"frame": "live", "targets": "targets", "lucky": "live"}.get(key, "stack")
        )
        bright = key == "lucky"
        self.st_hfr.set_label(_("sharpness") if bright else _("HFR"))
        self.st_integ.set_label(_("burst") if bright else _("integration"))
        if key == "targets":
            self.targets_panel.refresh()
        if key == "frame":
            self.frame.refresh_align_pick()
        if bright:
            self.lucky.refresh_body()
        self.image.redraw(True)

    def _req(self, **kw) -> None:
        if self.worker:
            self.worker.request(**kw)

    def _flag(self, name: str) -> None:
        if self.worker:
            self.worker.flag(name)
            if name == "reset":
                self.image.exit_review()
                self.health.clear()
                self._hist.clear()

    def _exposure_changed(self, x: float) -> None:
        self._exposure = x
        self._req(exposure=x)

    def _cooler_toggled(self, on: bool) -> None:
        self._req(target_temp=self.sp_temp.value(), cooler=on)
        if not self.worker:
            self.lbl_cool.setText(
                _("cooler will start with the capture") if on else _("cooler off")
            )

    def _bin_changed(self, text: str) -> None:
        if int(text) > 2:
            self.on_log(
                _(
                    "warning: bin{bin} clips the highlights; bin2 is the "
                    "only lossless one on this camera"
                ).format(bin=text)
            )
        self._req(bin=int(text))
        self.config_window.update_scale_label()

    def _replay_speed(self) -> float:
        return REPLAY_SPEEDS.get(self.frame.cb_speed.currentData(), 4.0)

    def _source_changed(self, i: int) -> None:
        replay = i == 1
        self.frame.cb_camera.setVisible(not replay)
        self.frame.btn_refresh.setVisible(not replay)
        self.frame.btn_folder.setVisible(replay)
        self.frame.rep_row.setVisible(replay)

    def refresh_cameras(self) -> None:
        self.frame.cb_camera.clear()
        try:
            cams = list_cameras()
        except Exception as e:
            self.on_log(_("error listing cameras: {error}").format(error=e))
            return
        if not cams:
            self.frame.cb_camera.addItem(_("no camera"))
            self.on_log(_("no camera"))
            return
        for c in cams:
            self.frame.cb_camera.addItem(f"{c.name}  ({c.port})")
        self.on_log(
            _("{n} camera(s): {names}").format(
                n=len(cams), names=", ".join(c.name for c in cams)
            )
        )

    def pick_replay(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, _("recorded session"), str(self.settings.path("capture_dir"))
        )
        if d:
            self._replay_folder = d
            self.on_log(_("replay: {path}").format(path=d))

    def start(self) -> None:
        replay = self.frame.cb_source.currentIndex() == 1
        cfg = Config.from_settings(
            self.settings,
            mode=self._mode,
            source="replay" if replay else "camera",
            camera_index=max(self.frame.cb_camera.currentIndex(), 0),
            replay_folder=self._replay_folder,
            bin=int(self.cb_bin.currentText()),
            exposure=self.sp_exp.value(),
            gain=self.sp_gain.value(),
            offset=self.sp_offset.value(),
            target_temp=(self.sp_temp.value() if self.btn_cooler.isChecked() else None),
            bias_path=self._bias_path,
            dark_path=self._dark_path,
            flat_path=self._flat_path,
            quality_weighting=self.integrate.chk_weight.isChecked(),
            strictness=self.integrate.cb_strictness.currentData(),
            sigma_clip=(
                self.integrate.sp_sigma.value()
                if self.integrate.sp_sigma.value() > 0
                else None
            ),
            replay_speed=self._replay_speed(),
            replay_loop=self.frame.chk_loop.isChecked(),
            record=self.integrate.chk_record.isChecked(),
            target_name=self._target_name,
            record_every=self.integrate.sp_every.value(),
            compress=self.integrate.chk_compress.isChecked(),
            burst_seconds=self.lucky.sp_burst_sec.value(),
            burst_frames=self.lucky.sp_burst_frames.value(),
            burst_compress=self.lucky.chk_burst_rice.isChecked(),
            body=self.lucky.body,
        )
        self._exposure = cfg.exposure
        self.image.exit_review()
        self.health.clear()
        self._hist.clear()
        self.alert.setVisible(False)
        self.worker = CaptureWorker(cfg)
        self._thread = QThread(self)
        self.worker.moveToThread(self._thread)
        self._thread.started.connect(self.worker.run)
        self.worker.frame.connect(self.on_frame)
        self.worker.focus.connect(self.on_focus)
        self.worker.log.connect(self.on_log)
        self.worker.failed.connect(self.on_failed)
        self.worker.opened.connect(self.on_opened)
        self.worker.finished.connect(self.on_finished)
        self.worker.paused.connect(self.on_paused)
        self.worker.cooling.connect(self.on_cooling)
        self.worker.bias_saved.connect(self.on_bias_saved)
        self.worker.dark_saved.connect(self.on_dark_saved)
        self.worker.burst_state.connect(self.lucky.on_burst)
        self.worker.calibration.connect(self.integrate.on_calibration)
        self.worker.flat_saved.connect(self.on_flat_saved)
        self.worker.set_loupe(self.image._loupe_xy)
        self._thread.start()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_finish.setEnabled(True)
        self._paused = False
        self._set_state("exposing")
        self._t_frame = time.time()
        if self.integrate.btn_integrate.isChecked():
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

    def _output_dir(self) -> Path:
        rec = getattr(self.worker, "recorder", None) if self.worker else None
        d = getattr(rec, "session_dir", None) if rec else None
        if d:
            out = Path(d)
            out.mkdir(parents=True, exist_ok=True)
            return out
        return self.settings.path("export_dir", create=True)

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
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(stylesheet(p, self._large_targets))
        for plot, curve, line in (
            (self.integrate.hist, self.integrate.hist_curve, self.integrate.hist_black),
            (self.lucky.hist3, self.lucky.hist3_rgb[0], self.lucky.hist3_black),
        ):
            plot.setBackground(p.plot_bg)
            for ax in ("left", "bottom"):
                plot.getAxis(ax).setPen(p.text_dim)
                plot.getAxis(ax).setTextPen(p.text_dim)
            curve.setPen(pg.mkPen(p.curve, width=2))
            line.setPen(pg.mkPen(p.mark, style=Qt.PenStyle.DashLine))
        for curves in (self.integrate.hist_rgb, self.lucky.hist3_rgb):
            for curve, pen in zip(curves, self._rgb_pens(), strict=True):
                curve.setPen(pen)
        for lw in (self.integrate.hist_white, self.lucky.hist3_white):
            lw.setPen(pg.mkPen(p.text_dim, style=Qt.PenStyle.DotLine))
        self.image.view.setBackground(p.plot_bg)
        self.image.loupe.set_palette(p)
        self.health.set_palette(p)
        self.skymap.set_palette(p)
        self.targets.set_palette(p)
        self.targets_panel.preview.set_palette(p)
        self._pin_button_widths()
        self.targets_panel._style_completer()
        self.targets_panel._style_calendar()
        self._retint_icons()
        self._set_state(self._state)
        self.image.redraw(True)

    def toggle_full(self, on: bool) -> None:
        bar = self.image.btn_view_stack.parentWidget()
        if bar is not None:
            bar.setVisible(not on)
        self._left_col.setVisible(not on)
        self.context.setVisible(not on)
        self.log.setVisible(not on and self.btn_log.isChecked())

    def _frame_size(self) -> tuple[int, int]:
        if self.image._q is not None:
            return int(self.image._q.shape[1]), int(self.image._q.shape[0])
        b = max(int(self.cb_bin.currentText()), 1)
        return self.settings.sensor_width // b, self.settings.sensor_height // b

    def _fov_arcmin(self) -> tuple[float, float]:
        w, h = self._frame_size()
        e = self.pixel_scale() / 60.0
        return e * w, e * h

    def _cat(self) -> Catalog | None:
        if self._catalog is not None:
            return self._catalog
        if self._cat_missing and not self.settings.catalog_path().exists():
            return None
        try:
            self._catalog = Catalog(self.settings.catalog_path()).load()
            self._cat_missing = False
            self.on_log(_("catalogue: {n} objects").format(n=len(self._catalog.objs)))
        except Exception as e:
            self._cat_missing = True
            self.on_log(_("catalogue unavailable: {error}").format(error=e))
        return self._catalog

    def clear_target(self) -> None:
        self._target = None
        self.lucky.body_target = False
        self.targets_panel.ed_goto.clear()
        self.targets_panel.lbl_goto.setText(_("no target"))
        self.frame.lbl_goto_arrow.setText("")
        self.frame.lbl_goto_dir.setFont(T_BODY())
        self.frame.lbl_goto_dir.setText(_("choose a target in TARGETS (2)"))
        self.targets_panel.btn_clear_target.setEnabled(False)
        self.frame.refresh_align_pick(force=True)
        if self._view == "map":
            self._update_map()
        self.on_log(_("target forgotten"))

    def _objects_in_field(self) -> None:
        here = self.frame.pos()
        cat = self._cat()
        if here is None or not cat:
            return
        radius = self._fov_deg() / 2.0
        near = cat.near(here[0], here[1], radius, limit=8)
        self.frame.lbl_objects.setText(
            "\n".join(f"{o.label} — {o.kind_label}" for o in near)
            or _("nothing catalogued in the field")
        )

    def _sky_marks(self) -> list:
        now = time.monotonic()
        if self._marks and now - self._marks_t < 2.0:
            return self._marks
        self._marks_t = now
        lat, lon = self.settings.latitude, self.settings.longitude
        elev = self.settings.elevation_m

        stars = brightstars.STARS
        vs = sky_vectors(
            [s.ra for s in stars], [s.dec for s in stars], lat, lon, elevation_m=elev
        )
        marks = [
            Mark(s.label, v, s.mag, "star", s)
            for s, v in zip(stars, vs, strict=True)
            if v[2] > -0.09
        ]

        cat = self._cat()
        if cat:
            if self._dsos is None:
                self._dsos = [
                    o for o in cat.objs if np.isfinite(o.mag) and o.mag <= 10.0
                ]
            vs = sky_vectors(
                [o.ra for o in self._dsos],
                [o.dec for o in self._dsos],
                lat,
                lon,
                elevation_m=elev,
            )
            marks += [
                Mark(o.label.split(" (")[0], v, o.mag, "dso", o)
                for o, v in zip(self._dsos, vs, strict=True)
                if v[2] > -0.09
            ]
        ra, dec = constellations.endpoints()
        v = sky_vectors(ra, dec, lat, lon, elevation_m=elev)
        self._lines = v.reshape(-1, 2, 3)
        nv = sky_vectors(
            [c[1] for c in constellations.NAMES],
            [c[2] for c in constellations.NAMES],
            lat,
            lon,
            elevation_m=elev,
        )
        self._const_names = [
            (c[0], v2)
            for c, v2 in zip(constellations.NAMES, nv, strict=True)
            if v2[2] > 0.0
        ]
        self._marks = marks
        return marks

    def _update_map(self) -> None:
        rays = self.point.rays()
        marks = self._sky_marks() if rays is not None else []
        lat, lon = self.settings.latitude, self.settings.longitude
        elev = self.settings.elevation_m
        target = None
        if self._target is not None:
            v = sky_vectors(
                [self._target.ra], [self._target.dec], lat, lon, elevation_m=elev
            )[0]
            target = Mark(
                getattr(self._target, "label", _("target")),
                v,
                getattr(self._target, "mag", 99.0),
                "dso",
                self._target,
            )
            base = target.label.split(" (")[0]
            marks = [m for m in marks if m.label.split(" (")[0] != base] + [target]
        self.skymap.found = None
        if self._found is not None:
            v = sky_vectors(
                [self._found.ra], [self._found.dec], lat, lon, elevation_m=elev
            )[0]
            name = self._found.label.split(" (")[0]
            kind = "star" if isinstance(self._found, brightstars.Star) else "dso"
            m = Mark(name, v, getattr(self._found, "mag", 9.0), kind, self._found)
            self.skymap.found = m
            if not any(x.label == name for x in marks):
                marks = [*marks, m]
        w, h = self._frame_size()
        e = self.pixel_scale() / 3600.0
        self.skymap.cam_fov = (e * w, e * h)
        anchor = self.point.star.label if self.point.star is not None else ""
        self.skymap.set_state(
            rays,
            marks,
            target,
            self.point.aligned,
            self.point.live,
            self._lines,
            self._const_names,
            anchor,
        )

    def _drag_sky(self, degrees: float) -> None:
        if self.point.aligned:
            return
        self.point.nudge_az(degrees)

    def _apply_target(self, o) -> None:
        self._target = o
        self.targets_panel.btn_clear_target.setEnabled(True)
        self.targets_panel.lbl_goto.setText(
            f"{o.label}\n{o.kind_label} · RA {o.ra:.3f}° Dec {o.dec:+.3f}°"
        )
        self.frame.update_goto()
        self.frame.refresh_align_pick(force=True)

    def _fov_deg(self) -> float:
        return self._fov_arcmin()[0] / 60.0

    def _set_state(self, state: str) -> None:
        self._state = state
        p = self.pal
        spec = {
            "idle": (_("IDLE"), p.idle),
            "framing": (_("FRAMING"), p.accent),
            "live": (_("LIVE"), p.accent),
            "exposing": (_("EXPOSING"), p.accent),
            "integrating": (_("INTEGRATING"), p.ok),
            "realigning": (_("REALIGNING"), p.warn),
            "replay": (_("REPLAY"), p.accent),
            "ready": (_("READY"), p.accent),
            "recording": (_("RECORDING"), p.ok),
            "paused": (_("PAUSED"), p.warn),
            "stopping": (_("STOPPING"), p.warn),
            "error": (_("ERROR"), p.bad),
        }
        label, color = spec.get(state, (state.upper(), p.text))
        self.st_state.set(f"● {label}", color)

    def _on_tick(self) -> None:
        if (
            self._mode == "targets"
            and self.targets_panel.btn_now.isChecked()
            and time.monotonic() - self.targets_panel._targets_t > 60.0
        ):
            self.targets_panel.refresh()
        if self._mode == "frame":
            self.frame.refresh_align_pick()
        if self._mode == "lucky" and time.monotonic() - self.lucky.body_t > 30.0:
            self.lucky.refresh_body()
        if self.lucky.body_target and time.monotonic() - self.lucky.body_track > 60.0:
            self.lucky.body_track = time.monotonic()
            fresh = self.lucky.body_now(max_age=0.0)
            if fresh is not None:
                self._apply_target(fresh.as_target())
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
            self.phase.setText(
                _("EXPOSING  {elapsed:.1f} / {total:.1f}s").format(
                    elapsed=elapsed, total=exp
                )
            )
        else:
            self.prog.setValue(100)
            over = elapsed - exp
            self.phase.setText(
                _("READING AND PROCESSING  +{over:.1f}s").format(over=over)
                if over < 8
                else _("WAITING FOR A FRAME  +{over:.0f}s").format(over=over)
            )
        self._check_alerts()

    def _check_alerts(self) -> None:
        p, st = self.pal, self._last_stats
        msgs = []
        streak = self.health.streak()
        if streak >= 5:
            msgs.append(
                (
                    _(
                        "{n} frames rejected in a row — cloud, dew or the "
                        "target left the frame"
                    ).format(n=streak),
                    p.bad,
                )
            )
        bright = st.get("lucky") or {}
        if bright.get("clipped", 0.0) > 0.001:
            msgs.append(
                (
                    _(
                        "the disc is clipping on {pct:.2f}% of the measured "
                        "window — shorten the exposure"
                    ).format(pct=bright["clipped"] * 100),
                    p.bad,
                )
            )
        cool = self._cool
        if cool.get("phase") == "saturated":
            msgs.append((cool.get("message", _("cooler saturated")), p.bad))
        delta = cool.get("dark_delta")
        if delta is not None and abs(delta) > 3.0:
            msgs.append(
                (
                    _(
                        "dark taken {delta:+.1f} C away from the current "
                        "temperature — thermal residual in the stack"
                    ).format(delta=delta),
                    p.warn,
                )
            )
        plat = st.get("platform", {})
        useful = plat.get("useful_s", float("inf"))
        integ = st.get("integration", 0.0)
        if np.isfinite(useful) and integ > useful:
            msgs.append(
                (
                    _(
                        "integration passed the rotation budget ({min:.0f} "
                        "min): the corner stars are already trailing"
                    ).format(min=useful / 60),
                    p.warn,
                )
            )
        if msgs:
            text, color = msgs[0]
            self.alert.setText("⚠  " + text)
            self.alert.setStyleSheet(f"color: {color}")
            self.alert.setVisible(True)
        else:
            self.alert.setVisible(False)

    @Slot(dict)
    def on_opened(self, info: dict) -> None:
        lo, hi = info.get("gain_range", (0, 570))
        self.sp_gain.setRange(lo, hi)
        has_cooler = bool(info.get("cooler"))
        self.btn_cooler.setEnabled(has_cooler)
        self.sp_temp.setEnabled(has_cooler)
        if not has_cooler:
            self.lbl_cool.setText(_("camera without cooling"))
        if info.get("live") and info.get("width"):
            b = max(int(info.get("bin", 1)), 1)
            self.settings.sensor_width = int(info["width"]) * b
            self.settings.sensor_height = int(info["height"]) * b
        self._set_state("replay" if not info.get("live", True) else "exposing")
        self.on_log(
            f"{info['name']}  {info.get('port', '')}  "
            f"{info['width']}x{info['height']} bin{info['bin']}  "
            f"Bayer {info['bayer']}  "
            + _("scale 0..{full}").format(full=info["full_scale"])
        )

    @Slot(object, object, object, dict)
    def on_frame(self, live, stack, cfa, st: dict) -> None:
        self._live = live
        self.integrate.update_realign_arrow(st.get("realign"))
        if not st.get("n_stacked"):
            self._stack = None
        elif stack is not None:
            self._stack = stack
        self._last_stats = st
        self._t_frame = time.time()
        self.integrate.update_align(st.get("align"))
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
                "limits": st.get("limits") or {},
                "time": time.strftime("%H:%M:%S"),
            }
            self.health.push(bool(acc), info)
            self._hist.push(
                st.get("frame_index"), cfa, st.get("bayer"), info, accepted=bool(acc)
            )
            self._set_state("integrating" if st.get("n_stacked") else "exposing")
        elif st.get("realigning"):
            self._set_state("realigning")
        elif st.get("can_integrate"):
            self._set_state("ready")
        else:
            self._set_state("framing" if st.get("mode") == "frame" else "live")

        p = self.pal
        self.st_integ.set(_hms(st.get("integration", 0.0)))
        n_ok, n_bad = st.get("n_stacked", 0), st.get("n_rejected", 0)
        self.st_frames.set(
            f"{n_ok}·{n_bad}", p.bad if self.health.streak() >= 3 else None
        )
        fw = st.get("fwhm", float("nan"))
        if acc is False:
            self.on_log(
                _("rejected: {reason} ({n} stars, FWHM {fwhm:.2f})").format(
                    reason=st.get("reason", "?"), n=st.get("n_stars", 0), fwhm=fw
                )
            )
        if st.get("recording"):
            self.integrate.lbl_disk.setText(st["recording"])
            self.integrate.lbl_disk.setVisible(True)
        self.integrate.lbl_advice.setText(st.get("platform_advice", "—"))
        rej = st.get("rejections") or {}
        if rej:
            total = sum(rej.values())
            lines = [
                f"{v:3d}  {kind_label(k)}"
                for k, v in sorted(rej.items(), key=lambda kv: -kv[1])
            ]
            self.integrate.lbl_rej.setText(
                "\n".join(lines) + f"\n{'—' * 12}\n" + f"{total:3d}  " + _("total")
            )
        else:
            self.integrate.lbl_rej.setText(_("none"))
        plat = st.get("platform", {})
        rot = plat.get("rotation_deg_min", float("nan"))
        self.integrate.st_rot.set(f"{rot:+.4f}°/min" if np.isfinite(rot) else "—")
        u = plat.get("useful_s", float("inf"))
        self.integrate.st_useful.set(
            _("no limit")
            if not np.isfinite(u)
            else _("{min:.0f} min").format(min=u / 60)
        )
        sm = plat.get("corner_smear_px_min", float("nan"))
        self.integrate.st_smear.set(f"{sm:.2f} px/min" if np.isfinite(sm) else "—")
        if st.get("mode") == "lucky":
            self.lucky.show_frame(st.get("lucky") or {})
        self._update_view_label()
        self.image.redraw(True)
        self.frame.update_goto()

    @Slot(object, dict)
    def on_focus(self, crop, d: dict) -> None:
        hfr = d.get("hfr", float("nan"))
        ratio = d.get("ratio", float("nan"))
        p = self.pal
        color = None
        if np.isfinite(ratio):
            color = p.ok if ratio < 1.05 else (p.warn if ratio < 1.3 else p.bad)
        self.st_hfr.set(f"{hfr:.2f}" if np.isfinite(hfr) else "—", color)
        if self.image.loupe_on:
            self.image.loupe.set_focus(crop, d, p)
        self.beeper.beep_ratio(ratio)

    @Slot(bool)
    @Slot(str)
    def on_flat_saved(self, path: str) -> None:
        self._flat_path = path

    @Slot(str)
    def on_dark_saved(self, path: str) -> None:
        self._dark_path = path

    @Slot(str)
    def on_bias_saved(self, path: str) -> None:
        self._bias_path = path

    @Slot(dict)
    def on_cooling(self, st: dict) -> None:
        self._cool = st
        p = self.pal
        phase = st.get("phase", "off")
        color = {
            "stable": p.ok,
            "cooling": p.accent,
            "warming": p.warn,
            "saturated": p.bad,
        }.get(phase)
        current, power = st.get("current"), st.get("power", 0)
        eta = st.get("eta_s", float("nan"))
        txt = f"{current:+.1f} °C · TEC {power}%"
        if phase == "stable":
            txt += " · " + _("stable")
        elif phase in ("cooling", "warming") and np.isfinite(eta):
            txt += " · " + (_("cooling") if phase == "cooling" else _("warming up"))
            txt += f" ~{eta / 60:.0f} min"
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
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
        self.worker = None
        self._thread = None
        self._paused = False
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText(_("Pause"))
        self._ic(self.btn_pause, "pause")
        self.btn_finish.setEnabled(False)
        self.btn_finish.setText(_("Finish"))
        if self.integrate.btn_integrate.isChecked():
            self.integrate.btn_integrate.setChecked(False)
        if self.integrate.btn_realign.isChecked():
            self.integrate.btn_realign.setChecked(False)
        if self.integrate.btn_align.isChecked():
            self.integrate.btn_align.setChecked(False)
        self.integrate.update_realign_arrow(None)
        self.lucky.set_burst_button(False)
        self.lucky.follow_ema = None
        self._set_state("idle")
        self.on_log(_("session ended"))

    def _set_view(self, which: str) -> None:
        self.image._revisit = None
        self.health.select(None)
        self._view = which
        self.image.btn_view_stack.setChecked(which == "stack")
        self.image.btn_view_live.setChecked(which == "live")
        self.image.btn_view_map.setChecked(which == "map")
        self.canvas.setCurrentIndex({"map": 1, "targets": 2}.get(which, 0))
        self.image.loupe.setVisible(self.image.loupe_on)
        if self.image.loupe_on:
            self.image.loupe.place()
        if which == "map":
            self.targets_panel.build_completer()
            self._update_map()
        self._update_view_label()
        self.image.redraw(True)

    def toggle_view(self) -> None:
        self._set_view("live" if self._view in ("stack", "map", "targets") else "stack")

    def _update_view_label(self) -> None:
        st = self._last_stats
        p = self.pal
        if self.image._revisit is not None:
            d, ok = self.image._revisit["info"], self.image._revisit["ok"]
            txt = _("REVIEWING #{index}").format(index=self.image._revisit["index"])
            txt += " · " + (
                _("accepted")
                if ok
                else _("rejected — {reason}").format(reason=d.get("reason", "?"))
            )
            if d.get("n_stars") is not None:
                txt += " · " + _("{n} stars").format(n=d["n_stars"])
            txt += _criteria_inline(d)
            txt += "   ·   " + _("Esc returns to live")
            self.image.lbl_view.setText(txt)
            self.image.lbl_view.setStyleSheet(f"color: {p.ok if ok else p.bad}")
            return
        if self._view == "targets":
            self.image.lbl_view.setText("")
            self.image.lbl_view.setStyleSheet("")
            return
        if self._view == "map":
            pt = self.point
            col: str | None
            altaz = pt.altaz if pt.live else None
            if altaz is None:
                txt, col = _("map · no sensor reading"), p.warn
            else:
                alt, az = altaz
                txt = _("map · alt {alt:+.1f}° az {az:.1f}°").format(alt=alt, az=az)
                col = p.ok if pt.aligned else p.warn
                if pt.aligned and pt.star is not None:
                    txt += " · " + _("aligned on {star}").format(star=pt.star.label)
                elif not pt.aligned:
                    txt += " · " + _("not aligned")
            self.image.lbl_view.setText(txt)
            self.image.lbl_view.setStyleSheet(f"color: {col}" if col else "")
            return
        if self._view == "live":
            acc = st.get("accepted")
            if acc is None:
                txt, col = _("current frame, not stacking"), None
            elif acc:
                txt = _("frame accepted · {n} stars").format(n=st.get("n_stars", 0))
                col = p.ok
            else:
                txt = _("frame REJECTED · {reason}").format(
                    reason=st.get("reason", "?")
                )
                col = p.bad
            n = st.get("frame_index")
            if n:
                txt = f"#{n} · " + txt
        else:
            if self._stack is None:
                txt, col = _("no stack yet"), None
            else:
                txt = _("{n} frames · {time} of integration").format(
                    n=st.get("n_stacked", 0), time=_hms(st.get("integration", 0.0))
                )
                col = None
        self.image.lbl_view.setText(txt)
        self.image.lbl_view.setStyleSheet(f"color: {col}" if col else "")

    def _restore(self) -> None:
        self.btn_night.setChecked(self._theme == "night")
        self.config_window.sl_night.setValue(self._night_level)
        self.config_window.chk_touch.setChecked(self._large_targets)
        self._exposure = self.sp_exp.value()
        self.config_window.update_scale_label()

    def closeEvent(self, ev) -> None:
        s = self.settings
        if self._mode != "lucky":
            s.exposure_s = self.sp_exp.value()
            s.gain = self.sp_gain.value()
            s.offset = self.sp_offset.value()
            s.binning = int(self.cb_bin.currentText())
        self.lucky.store()
        self.config_window.store(save=False)
        s.target_min_alt = self.targets_panel.sp_min_alt.value()
        s.target_max_mag = self.targets_panel.sp_max_mag.value()
        s.target_family = self.targets_panel.cb_family.currentData()
        s.target_fits_only = self.targets_panel.chk_fits.isChecked()
        s.previews_enabled = self.targets_panel.chk_previews.isChecked()
        try:
            s.save()
        except OSError:
            pass
        self.tick.stop()
        self.tick_sensor.stop()
        if self.worker:
            self.worker.stop()
        if self._thread:
            self._thread.quit()
            self._thread.wait(2000)
        self.hs.stop()
        self.preview_loader.shutdown()
        super().closeEvent(ev)


def _criteria_inline(d: dict) -> str:
    limits = d.get("limits") or {}
    out = []
    for key, name in (
        ("weight", "weight"),
        ("elongation", "elong"),
        ("fwhm", "FWHM"),
        ("halo", "halo"),
    ):
        v, ceiling = d.get(key), limits.get(key)
        if (
            v is None
            or ceiling is None
            or not (np.isfinite(v) and np.isfinite(ceiling))
        ):
            continue
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
        return f"{s // 60}m{s % 60:02d}"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def main() -> int:
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME.lower())
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_NAME.lower())
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
        settings = Settings.load()
        set_language(requested[-1])
