from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core import platform_align
from ...i18n import N_
from ...i18n import gettext as _
from ...pointing.pushto import guide
from ..design import (
    T_BODY,
    T_DISPLAY,
    T_MONO,
    T_XL,
    Card,
    ElidedLabel,
    Stat,
    hms,
)

STRICTNESS_LEVELS = (N_("lenient"), N_("normal"), N_("strict"))

REALIGN_ON_TARGET_PX = 6.0


@dataclass(frozen=True)
class StretchParams:
    target_bg: float
    shadows_clip: float
    saturation: float
    white: float
    auto: bool
    arcsinh: bool


STRETCH_PRESETS = {
    N_("soft"): (0.15, 3.2),
    N_("medium"): (0.25, 2.8),
    N_("strong"): (0.40, 2.2),
}


class IntegratePanel:
    def __init__(self, win):
        self.win = win
        self.settings = win.settings
        self._calib_labels: dict = {}

    def stretch_params(self) -> StretchParams:
        def val(slider):
            return slider.value() / slider._div

        return StretchParams(
            target_bg=val(self.sl_bg),
            shadows_clip=-val(self.sl_clip),
            saturation=val(self.sl_sat),
            white=val(self.sl_white),
            auto=self.chk_auto.isChecked(),
            arcsinh=self.cb_algo.currentIndex() == 1,
        )

    def panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        c = Card(_("integration"))
        self.btn_integrate = QPushButton(_("Start integrating"))
        self.btn_integrate.setCheckable(True)
        self.btn_integrate.setMinimumHeight(38)
        self.btn_integrate.setToolTip(
            _(
                "Starts stacking, recording the subs and measuring the platform.\n"
                "Switching modes does not trigger this on its own: coming in here\n"
                "only shows the panel. Writing to disk and consuming platform travel\n"
                "is your decision, not a side effect of clicking a tab."
            )
        )
        self.win._ic(self.btn_integrate, "play")
        self.btn_integrate.toggled.connect(self._integrate_toggled)
        c.add(self.btn_integrate)
        v.addWidget(c)

        v.addWidget(self._card_stretch())
        v.addWidget(self._card_channel_gain())

        c = Card(_("stacking"))
        self.chk_weight = QCheckBox(_("give better frames more weight"))
        self.chk_weight.setChecked(True)
        self.chk_weight.setToolTip(
            _(
                "Weights each frame by flux/(noise²·FWHM²). It matters alongside\n"
                "vote-based registration, which lets marginal frames in: the weight\n"
                "keeps them from dragging the stack down."
            )
        )
        c.add(self.chk_weight)
        self.sp_sigma = QDoubleSpinBox()
        self.sp_sigma.setRange(0.0, 6.0)
        self.sp_sigma.setValue(3.0)
        self.sp_sigma.setSingleStep(0.5)
        self.sp_sigma.setSuffix(" σ")
        self.sp_sigma.valueChanged.connect(
            lambda x: self.win._req(sigma_clip=(x if x > 0 else None))
        )
        c.field(
            _("outlier rejection"),
            self.sp_sigma,
            _("removes satellites and aircraft from the stack. 0 disables it"),
        )
        self.cb_strictness = QComboBox()
        for level in STRICTNESS_LEVELS:
            self.cb_strictness.addItem(_(level), level)
        self.cb_strictness.setCurrentIndex(1)
        self.cb_strictness.setToolTip(
            _(
                "How much a frame has to be worth, compared to what tonight is\n"
                "delivering, to enter the stack. Lenient uses more frames and gains\n"
                "signal; strict uses fewer and gains sharpness."
            )
        )
        self.cb_strictness.currentIndexChanged.connect(
            lambda i: self.win._req(strictness=self.cb_strictness.itemData(i))
        )
        c.field(
            _("accept frames"),
            self.cb_strictness,
            _("how much to demand of each frame for it to be used"),
        )
        self.btn_realign = QPushButton(_("Pause & realign   (R)"))
        self.btn_realign.setCheckable(True)
        self.win._ic(self.btn_realign, "target")
        self.btn_realign.setToolTip(
            _(
                "Pauses accumulation without losing the stack, and draws a "
                "reticle over the image with the target's current position "
                "marked — nudge the tube until the two coincide. If the platform "
                "had to be reset all the way back, the target may be nowhere in "
                "the frame: the phone arrow above it then gives the coarse "
                "direction from where it points now, the on-image reticle takes "
                "over for the last stretch once the target is back in view. "
                "Resume starts a new segment on its own, with relaxed thresholds "
                "and a fresh reference."
            )
        )
        self.btn_realign.toggled.connect(self._realign_toggled)
        c.add(self.btn_realign)
        self.lbl_realign_arrow = QLabel("")
        self.lbl_realign_arrow.setFont(T_DISPLAY())
        self.lbl_realign_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_realign_arrow.setVisible(False)
        c.add(self.lbl_realign_arrow)
        self.lbl_realign_dir = QLabel("")
        self.lbl_realign_dir.setWordWrap(True)
        self.lbl_realign_dir.setFont(T_BODY())
        self.lbl_realign_dir.setVisible(False)
        self.lbl_realign_dir.setToolTip(
            _(
                "Coarse guidance from the phone sensor, not from the stars: the "
                "one thing that still works when the target left the frame "
                "entirely and there is nothing in common to register against."
            )
        )
        c.add(self.lbl_realign_dir)
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
        self.sp_every.valueChanged.connect(lambda x: self.win._req(record_every=x))
        row.addWidget(self.chk_compress)
        row.addWidget(QLabel(_("1 in every")))
        row.addWidget(self.sp_every)
        c.add_layout(row)
        self.lbl_disk = QLabel("")
        self.lbl_disk.setFont(T_MONO())
        self.lbl_disk.setWordWrap(True)
        self.lbl_disk.setVisible(False)
        c.add(self.lbl_disk)
        v.addWidget(c)

        c = Card(_("calibration"))
        row = QHBoxLayout()
        self.btn_bias = QPushButton(_("Load bias"))
        self.win._ic(self.btn_bias, "bias")
        self.btn_bias.setToolTip(
            _(
                "The offset pedestal, at the shortest exposure the camera does.\n"
                "Applied only when no dark is loaded — a dark already contains it —\n"
                "and it is what a flat has to have subtracted."
            )
        )
        self.btn_bias.clicked.connect(self.pick_bias)
        self.btn_capture_bias = QPushButton(_("Record bias"))
        self.win._ic(self.btn_capture_bias, "bias")
        self.btn_capture_bias.setToolTip(
            _(
                "Records a master bias with the gain, offset and bin of the session\n"
                "in progress. The exposure drops to the camera's minimum for the\n"
                "recording and goes back afterwards. Cap the sensor first."
            )
        )
        self.btn_capture_bias.clicked.connect(self.capture_bias)
        self.btn_clear_bias = QPushButton()
        self.win._ic(self.btn_clear_bias, "close")
        self.btn_clear_bias.setMaximumWidth(38)
        self.btn_clear_bias.setEnabled(False)
        self.btn_clear_bias.setToolTip(
            _(
                "Stops using this master — the frames that follow are no\n"
                "longer corrected by it. The file stays on disk."
            )
        )
        self.btn_clear_bias.clicked.connect(self.clear_bias)
        row.addWidget(self.btn_bias)
        row.addWidget(self.btn_capture_bias)
        row.addWidget(self.btn_clear_bias)
        c.add_layout(row)
        self.lbl_bias = QLabel(_("{kind}: none").format(kind="bias"))
        self.lbl_bias.setFont(T_MONO())
        self.lbl_bias.setWordWrap(True)
        c.add(self.lbl_bias)
        row = QHBoxLayout()
        self.btn_dark = QPushButton(_("Load dark"))
        self.win._ic(self.btn_dark, "dark")
        self.btn_dark.clicked.connect(self.pick_dark)
        self.btn_capture_dark = QPushButton(_("Record dark"))
        self.win._ic(self.btn_capture_dark, "dark")
        self.btn_capture_dark.setToolTip(
            _(
                "Records a master dark with the exposure, gain, offset, bin and\n"
                "temperature of the session in progress — which is exactly what a\n"
                "dark has to match. Cap the sensor first."
            )
        )
        self.btn_capture_dark.clicked.connect(self.capture_dark)
        self.btn_clear_dark = QPushButton()
        self.win._ic(self.btn_clear_dark, "close")
        self.btn_clear_dark.setMaximumWidth(38)
        self.btn_clear_dark.setEnabled(False)
        self.btn_clear_dark.setToolTip(
            _(
                "Stops using this master — the frames that follow are no\n"
                "longer corrected by it. The file stays on disk."
            )
        )
        self.btn_clear_dark.clicked.connect(self.clear_dark)
        row.addWidget(self.btn_dark)
        row.addWidget(self.btn_capture_dark)
        row.addWidget(self.btn_clear_dark)
        c.add_layout(row)
        self.lbl_dark = QLabel(_("{kind}: none").format(kind="dark"))
        self.lbl_dark.setFont(T_MONO())
        self.lbl_dark.setWordWrap(True)
        c.add(self.lbl_dark)
        row = QHBoxLayout()
        self.btn_flat = QPushButton(_("Load flat"))
        self.win._ic(self.btn_flat, "grid")
        self.btn_flat.setToolTip(
            _(
                "Corrects vignetting and dust; without one the corners go dark and\n"
                "the autostretch gives it away."
            )
        )
        self.btn_flat.clicked.connect(self.pick_flat)
        self.btn_capture_flat = QPushButton(_("Record flat"))
        self.win._ic(self.btn_capture_flat, "grid")
        self.btn_capture_flat.setToolTip(
            _(
                "Records a master flat with the gain and bin of the session in\n"
                "progress. Point at an evenly illuminated surface — twilight sky, a\n"
                "flat panel, a stretched white shirt — and set the exposure so the\n"
                "histogram lands near half scale. The bias, or a dark of this same\n"
                "exposure, is subtracted from it."
            )
        )
        self.btn_capture_flat.clicked.connect(self.capture_flat)
        self.btn_clear_flat = QPushButton()
        self.win._ic(self.btn_clear_flat, "close")
        self.btn_clear_flat.setMaximumWidth(38)
        self.btn_clear_flat.setEnabled(False)
        self.btn_clear_flat.setToolTip(
            _(
                "Stops using this master — the frames that follow are no\n"
                "longer corrected by it. The file stays on disk."
            )
        )
        self.btn_clear_flat.clicked.connect(self.clear_flat)
        row.addWidget(self.btn_flat)
        row.addWidget(self.btn_capture_flat)
        row.addWidget(self.btn_clear_flat)
        c.add_layout(row)
        self.lbl_flat = QLabel(_("{kind}: none").format(kind="flat"))
        self.lbl_flat.setFont(T_MONO())
        self.lbl_flat.setWordWrap(True)
        c.add(self.lbl_flat)
        self._calib_labels = {
            "bias": self.lbl_bias,
            "dark": self.lbl_dark,
            "flat": self.lbl_flat,
        }
        self._calib_clear = {
            "bias": self.btn_clear_bias,
            "dark": self.btn_clear_dark,
            "flat": self.btn_clear_flat,
        }
        v.addWidget(c)

        c = Card(_("equatorial platform"))
        self.lbl_advice = QLabel("—")
        self.lbl_advice.setWordWrap(True)
        self.lbl_advice.setFont(T_MONO())
        c.add(self.lbl_advice)
        self.btn_align = QPushButton(_("Align the platform   (A)"))
        self.btn_align.setCheckable(True)
        self.win._ic(self.btn_align, "target")
        self.btn_align.setToolTip(
            _(
                "Reads the polar error off the rotation of the field itself, frame\n"
                "by frame, and keeps reading while you turn the screws. One field\n"
                "gives the error along the direction it points, so finish on one\n"
                "field and check on another far from it.\n"
                "The number sharpens with time, not with frames: the first reading\n"
                "lands after about a minute and is worth a degree, five minutes are\n"
                "worth a quarter of one."
            )
        )
        self.btn_align.toggled.connect(self._align_toggled)
        c.add(self.btn_align)
        self.lbl_align = QLabel("—")
        self.lbl_align.setFont(T_XL())
        self.lbl_align.setAlignment(Qt.AlignmentFlag.AlignCenter)
        c.add(self.lbl_align)
        self.pb_align = QProgressBar()
        self.pb_align.setRange(0, 100)
        self.pb_align.setTextVisible(False)
        self.pb_align.setVisible(False)
        c.add(self.pb_align)
        self.lbl_align_step = ElidedLabel("—")
        self.lbl_align_step.setFont(T_MONO())
        c.add(self.lbl_align_step)
        self.lbl_align_verdict = ElidedLabel("")
        self.lbl_align_verdict.setFont(T_MONO())
        c.add(self.lbl_align_verdict)
        v.addWidget(c)
        v.addStretch(1)
        return w

    def _card_stretch(self) -> Card:
        c = Card(_("stretch"))
        row = QHBoxLayout()
        self.cb_algo = QComboBox()
        self.cb_algo.addItems([_("MTF (default)"), _("arcsinh")])
        self.cb_algo.setToolTip(
            _(
                "MTF is the default, the same family as PixInsight and SharpCap.\n"
                "arcsinh is gentler on the highlights and preserves the colour of a\n"
                "bright star's core, of a globular and of M42 — where the MTF blows\n"
                "out to white."
            )
        )
        self.cb_algo.currentIndexChanged.connect(lambda: self.win.image.redraw(True))
        row.addWidget(QLabel(_("algorithm")))
        row.addWidget(self.cb_algo, 1)
        c.add_layout(row)
        row = QHBoxLayout()
        for name in STRETCH_PRESETS:
            b = QPushButton(_(name))
            b.clicked.connect(lambda _c=False, n=name: self.win.image.apply_preset(n))
            row.addWidget(b)
        c.add_layout(row)
        self.chk_auto = QCheckBox(_("autostretch"))
        self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(lambda: self.win.image.redraw(True))
        c.add(self.chk_auto)
        self.sl_bg, wbg = self.win._slider(_("background"), 5, 60, 25, 100.0)
        self.sl_clip, wcl = self.win._slider(_("shadows"), 10, 60, 28, 10.0)
        c.add(wbg)
        c.add(wcl)
        self.sl_white, wwh = self.win._slider(_("whites"), 20, 100, 100, 100.0)
        self.sl_white.valueChanged.connect(lambda: self.win.image.redraw(True))
        self.sl_white.setToolTip(
            _(
                "White point: the value that comes out as 255. Lowering it saturates\n"
                "the highlights on purpose — that is how faint nebulosity is brought\n"
                "up without waiting for the core to behave. 1.00 uses the full scale."
            )
        )
        c.add(wwh)
        self.sl_sat, wsat = self.win._slider(_("saturation"), 0, 25, 10, 10.0)
        self.sl_sat.setToolTip(
            _(
                "Stretching compresses the distance between channels and the nebula\n"
                "goes pale; this gives the colour back without touching brightness."
            )
        )
        c.add(wsat)
        return c

    def _card_channel_gain(self) -> Card:
        c = Card(_("per-channel gain"))
        self.sl_r, wr = self.win._slider(_("red"), 30, 300, 100, 100.0)
        self.sl_g, wg = self.win._slider(_("green"), 30, 300, 100, 100.0)
        self.sl_b, wb = self.win._slider(_("blue"), 30, 300, 100, 100.0)
        for sl in (self.sl_r, self.sl_g, self.sl_b):
            sl.valueChanged.connect(lambda: self.win.image.redraw(True))
        for wid in (wr, wg, wb):
            c.add(wid)
        row = QHBoxLayout()
        b = QPushButton(_("equalise background"))
        b.setToolTip(
            _(
                "Matches the median of the three channels, taking green as the\n"
                "reference — a white balance measured on the sky, which is the grey\n"
                "surface always in the frame."
            )
        )
        b.clicked.connect(self.win.image.equalize_channels)
        row.addWidget(b)
        b = QPushButton(_("neutral"))
        b.setToolTip(_("returns the three gains to 1.00"))
        b.clicked.connect(
            lambda: [sl.setValue(100) for sl in (self.sl_r, self.sl_g, self.sl_b)]
        )
        row.addWidget(b)
        c.add_layout(row)
        return c

    def context(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        c = Card(_("histogram"))
        self.hist = pg.PlotWidget()
        self.hist.setLogMode(False, True)
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
        self.lbl_rej.setToolTip(
            _(
                "why frames are being dropped — what tells you "
                "whether to change strictness, refocus or wait "
                "out the cloud"
            )
        )
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

    def _integrate_toggled(self, on: bool) -> None:
        if on and not self.ask_target_name():
            self.btn_integrate.setChecked(False)
            return
        self.btn_integrate.setText(
            _("Stop integrating") if on else _("Start integrating")
        )
        self.win._ic(self.btn_integrate, "pause" if on else "play")
        self.win._flag("integrate_on" if on else "integrate_off")
        if on and not self.win.worker:
            self.win.on_log(_("start the capture first"))

    def reset_stack(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        integ = self.win._last_stats.get("integration", 0.0)
        n = self.win._last_stats.get("n_stacked", 0)
        if not n:
            self.win.on_log(_("nothing accumulated to reset"))
            return
        r = QMessageBox.question(
            self.win,
            _("Reset"),
            _(
                "Discard {time} of integration ({n} frames)?\n\n"
                "Subs already written to disk are not deleted."
            ).format(time=hms(integ), n=n),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        self.win._flag("reset")
        self.win.on_log(_("stack reset"))

    def ask_target_name(self) -> bool:
        suggestion = self.win._target_name
        if not suggestion and self.win._target is not None:
            suggestion = self.win._target.label.split(" (")[0]
        name, ok = QInputDialog.getText(
            self.win,
            _("DSO"),
            _("Target name (it will name the session folder):"),
            text=suggestion,
        )
        if not ok:
            return False
        self.win._target_name = name.strip()
        self.win.st_target.set(self.win._target_name or "—")
        self.win._req(target_name=self.win._target_name)
        return True

    def _align_toggled(self, on: bool) -> None:
        if on and not self.win.worker:
            self.win.on_log(_("start the capture first"))
            self.btn_align.setChecked(False)
            return
        self.btn_align.setText(
            _("Stop aligning   (A)") if on else _("Align the platform   (A)")
        )
        self.pb_align.setVisible(on)
        self.win._req(align=self.settings.align_minutes if on else 0.0)
        if not on:
            self.update_align(None)

    def update_align(self, status: dict | None) -> None:
        if status is None:
            self.lbl_align.setText("—")
            self.lbl_align_step.setText("—")
            self.lbl_align_verdict.setText("")
            self.pb_align.setValue(0)
            return
        p = self.win.pal
        self.pb_align.setValue(int(status.get("progress", 0.0) * 100))
        self.lbl_align.setText(platform_align.reading(status))
        self.lbl_align_step.setText(platform_align.advice(status))
        self.lbl_align_verdict.setText(platform_align.verdict(status))
        err = status.get("error_deg", float("nan"))
        colour = p.text_dim
        if status.get("settled") and np.isfinite(err):
            colour = p.ok if err <= platform_align.GOOD_DEG else p.warn
        self.lbl_align.setStyleSheet(f"color: {colour}")

    def _realign_toggled(self, on: bool) -> None:
        if on and not self.win.worker:
            self.win.on_log(_("start the capture first"))
            self.btn_realign.setChecked(False)
            return
        self.btn_realign.setText(
            _("Resume   (R)") if on else _("Pause & realign   (R)")
        )
        self.win._ic(self.btn_realign, "pause" if on else "target")
        self.btn_integrate.setEnabled(not on)
        self.win._flag("realign_on" if on else "realign_off")
        if on:
            self.update_realign_goto()
        else:
            self.update_realign_arrow(None)
            self.lbl_realign_arrow.setVisible(False)
            self.lbl_realign_dir.setVisible(False)

    def update_realign_goto(self) -> None:
        if not self.btn_realign.isChecked():
            return
        if self.win._target is None:
            self.lbl_realign_arrow.setVisible(False)
            self.lbl_realign_dir.setVisible(False)
            return
        self.lbl_realign_arrow.setVisible(True)
        self.lbl_realign_dir.setVisible(True)
        here = self.win.frame.pos()
        if here is None:
            self.lbl_realign_arrow.setText("")
            self.lbl_realign_dir.setFont(T_BODY())
            self.lbl_realign_dir.setText(_("align the sensor to compute the direction"))
            return
        g = guide(
            here,
            (self.win._target.ra, self.win._target.dec),
            self.settings.latitude,
            self.settings.longitude,
            elevation_m=self.settings.elevation_m,
            fov_deg=self.win._fov_deg(),
        )
        self.lbl_realign_dir.setFont(T_XL())
        self.lbl_realign_dir.setText(g.text)
        color = self.win.pal.ok if g.on_target else self.win.pal.warn
        self.lbl_realign_dir.setStyleSheet(f"color: {color}")
        self.lbl_realign_arrow.setText(arrow_for(g))
        self.lbl_realign_arrow.setStyleSheet(f"color: {color}")

    def update_realign_arrow(self, info: dict | None) -> None:
        if not info or self.win._live is None:
            self.win.image.realign_arrow.setVisible(False)
            return
        h, w = self.win._live.shape[:2]
        cx, cy = w / 2.0, h / 2.0
        p = self.win.pal
        if not info["ok"]:
            self.win.image.realign_arrow.set_info(
                cx,
                cy,
                0,
                0,
                ok=False,
                on_target=False,
                color=p.bad,
                label=info["reason"],
            )
        elif info["distance"] < REALIGN_ON_TARGET_PX:
            self.win.image.realign_arrow.set_info(
                cx,
                cy,
                0,
                0,
                ok=True,
                on_target=True,
                color=p.ok,
                label="✔ " + _("on target"),
            )
        else:
            arcmin = info["distance"] * self.win.pixel_scale() / 60.0
            self.win.image.realign_arrow.set_info(
                cx,
                cy,
                info["dx"],
                info["dy"],
                ok=True,
                on_target=False,
                color=p.warn,
                label=_("{arcmin:.1f}'").format(arcmin=arcmin),
            )
        self.win.image.realign_arrow.setVisible(True)

    def show_calibration(
        self, kind: str, path: str, reason: str = "", pending: bool = False
    ) -> None:
        label = self._calib_labels.get(kind)
        if label is None:
            return
        clear = self._calib_clear.get(kind)
        if clear is not None:
            clear.setEnabled(bool(path))
        p = self.win.pal
        if reason:
            label.setText(reason)
            label.setStyleSheet(f"color: {p.bad}")
        elif not path:
            label.setText(_("{kind}: none").format(kind=kind))
            label.setStyleSheet("")
        elif pending and not self.win.worker:
            label.setText(
                _("{kind}: {name} (checked at Start)").format(
                    kind=kind, name=Path(path).name
                )
            )
            label.setStyleSheet(f"color: {p.warn}")
        else:
            label.setText(_("{kind}: {name}").format(kind=kind, name=Path(path).name))
            label.setStyleSheet(f"color: {p.ok}")

    def on_calibration(self, d: dict) -> None:
        self.show_calibration(
            d.get("kind", "dark"), d.get("path", ""), d.get("reason", "")
        )

    def capture_bias(self) -> None:
        if not self.win.worker:
            self.win.on_log(_("start the capture before recording a bias"))
            return
        n, ok = QInputDialog.getInt(
            self.win,
            _("Record bias"),
            _(
                "How many frames?\n\n"
                "CAP THE SENSOR before confirming.\n"
                "The exposure drops to the camera's minimum during the\n"
                "recording and goes back to the session's afterwards."
            ),
            30,
            5,
            200,
        )
        if not ok:
            return
        self.win._req(bias_frames=n)
        self.win._flag("capture_bias")
        self.win.on_log(
            _("recording a bias of {n} frames — keep the sensor capped").format(n=n)
        )

    def capture_dark(self) -> None:
        if not self.win.worker:
            self.win.on_log(_("start the capture before recording a dark"))
            return
        n, ok = QInputDialog.getInt(
            self.win,
            _("Record dark"),
            _(
                "How many frames?\n\n"
                "CAP THE SENSOR before confirming.\n"
                "Capture pauses while recording."
            ),
            20,
            5,
            100,
        )
        if not ok:
            return
        self.win._req(dark_frames=n)
        self.win._flag("capture_dark")
        self.win.on_log(
            _("recording a dark of {n} frames — keep the sensor capped").format(n=n)
        )

    def capture_flat(self) -> None:
        if not self.win.worker:
            self.win.on_log(_("start the capture before recording a flat"))
            return
        n, ok = QInputDialog.getInt(
            self.win,
            _("Record flat"),
            _(
                "How many frames?\n\n"
                "POINT at an evenly illuminated surface and set the exposure so\n"
                "the histogram lands near half scale. The bias — or a dark of\n"
                "this same exposure — is subtracted from it. Capture pauses\n"
                "while recording."
            ),
            20,
            5,
            100,
        )
        if not ok:
            return
        self.win._req(flat_frames=n)
        self.win._flag("capture_flat")
        self.win.on_log(
            _("recording a flat of {n} frames — do not change the lighting").format(n=n)
        )

    def pick_bias(self) -> None:
        self._pick_master("bias")

    def pick_dark(self) -> None:
        self._pick_master("dark")

    def pick_flat(self) -> None:
        self._pick_master("flat")

    def clear_bias(self) -> None:
        self._clear_master("bias")

    def clear_dark(self) -> None:
        self._clear_master("dark")

    def clear_flat(self) -> None:
        self._clear_master("flat")

    def _clear_master(self, kind: str) -> None:
        if getattr(self.win, f"_{kind}_path", None) is None:
            return
        setattr(self.win, f"_{kind}_path", None)
        self.show_calibration(kind, "")
        self.win._req(**{f"{kind}_path": ""})
        self.win.on_log(
            _("{kind} removed — the frames are no longer corrected by it").format(
                kind=kind
            )
        )

    def _pick_master(self, kind: str) -> None:
        p, _sel = QFileDialog.getOpenFileName(
            self.win,
            _("master {kind}").format(kind=kind),
            str(self.settings.path(f"{kind}_dir")),
            "FITS (*.fits)",
        )
        if not p:
            return
        setattr(self.win, f"_{kind}_path", p)
        self.show_calibration(kind, p, pending=True)
        self.win._req(**{f"{kind}_path": p})


def arrow_for(g) -> str:
    if g.target_alt_deg < 0:
        return "—"
    if g.on_target:
        return "✔"
    tol = 0.1
    vert = "↑" if g.delta_alt_deg > tol else ("↓" if g.delta_alt_deg < -tol else "")
    hori = "→" if g.delta_az_deg > tol else ("←" if g.delta_az_deg < -tol else "")
    return (hori + vert) or "·"
