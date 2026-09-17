from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core import lucky, stretch
from ...core.tonight import compass_point
from ...i18n import gettext as _
from ..design import T_BODY, T_MONO, Card, Stat, hms, image_lut, shorten

LUCKY_WHITE = (10, 100)
LUCKY_GAMMA = (30, 100)

FOLLOW_ZOOM = 3.0

FOLLOW_SMOOTH = 0.2


class LuckyPanel:
    def __init__(self, win):
        self.win = win
        self.settings = win.settings
        self.body_state = None
        self.body_t = 0.0
        self.body_track = 0.0
        self.body_target = False
        self.follow_ema: tuple[float, float] | None = None

    def panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        c = Card(_("the body right now"))
        self.cb_body = QComboBox()
        for key, body in lucky.BODIES.items():
            self.cb_body.addItem(body.label, key)
        i = self.cb_body.findData(self.settings.lucky_body)
        self.cb_body.setCurrentIndex(max(i, 0))
        self.cb_body.currentIndexChanged.connect(self._body_changed)
        c.field(
            _("body"),
            self.cb_body,
            _(
                "the Sun is deliberately absent: nothing here can know "
                "whether there is a filter on the tube"
            ),
        )
        self.lbl_body = QLabel(_("computing…"))
        self.lbl_body.setFont(T_BODY())
        self.lbl_body.setWordWrap(True)
        c.add(self.lbl_body)
        self.lbl_body_fit = QLabel("")
        self.lbl_body_fit.setFont(T_MONO())
        self.lbl_body_fit.setWordWrap(True)
        c.add(self.lbl_body_fit)
        self.btn_body_point = QPushButton(_("Point at it"))
        self.win._ic(self.btn_body_point, "moon")
        self.btn_body_point.setToolTip(
            _(
                "Makes this body the target and opens FRAME, where the arrow says\n"
                "which way to push. Its position is recomputed every minute: the\n"
                "Moon moves half a degree an hour against the stars, which is its\n"
                "own diameter."
            )
        )
        self.btn_body_point.clicked.connect(self.body_point)
        c.add(self.btn_body_point)
        v.addWidget(c)

        c = Card(_("exposure"))
        self.lbl_body_exp = QLabel(_("start the capture"))
        self.lbl_body_exp.setFont(T_BODY())
        self.lbl_body_exp.setWordWrap(True)
        self.lbl_body_exp.setToolTip(
            _(
                "Measured on the raw mosaic, before demosaicing: saturation happens\n"
                "per photosite, and mixing three colours together hides the channel\n"
                "that went over. A clipped highlight is gone — no amount of stacking\n"
                "afterwards brings a crater floor back."
            )
        )
        c.add(self.lbl_body_exp)
        row = QHBoxLayout()
        self.btn_body_preset = QPushButton(_("Defaults for this body"))
        self.btn_body_preset.setToolTip(
            _(
                "Milliseconds, low gain, bin1 — none of the deep-sky settings\n"
                "survive a target eight magnitudes brighter than everything else.\n"
                "Scaled off the Moon's exposure by the ratio of surface\n"
                "brightnesses, and capped where a longer frame would average the\n"
                "seeing instead of freezing it. A starting point, not an answer:\n"
                "finish with the suggestion above."
            )
        )
        self.btn_body_preset.clicked.connect(self._body_preset)
        self.btn_body_apply = QPushButton(_("Apply the suggestion"))
        self.btn_body_apply.setToolTip(
            _(
                "Scales the exposure so the brightest photosite lands just below\n"
                "saturation. Takes two or three goes once it is clipping: a clipped\n"
                "frame no longer records how far over it went."
            )
        )
        self.btn_body_apply.clicked.connect(self._apply_suggested_exposure)
        row.addWidget(self.btn_body_preset)
        row.addWidget(self.btn_body_apply)
        c.add_layout(row)
        v.addWidget(c)

        c = Card(_("burst"))
        self.btn_burst = QPushButton(_("Record burst   (R)"))
        self.btn_burst.setCheckable(True)
        self.btn_burst.setMinimumHeight(38)
        self.win._ic(self.btn_burst, "play")
        self.btn_burst.setToolTip(
            _(
                "Writes every frame to its own session folder until the limit below,\n"
                "and stops on its own. This is what replaces integration here: the\n"
                "sharpest few percent are stacked later, in a program built for it.\n"
                "Each frame carries its measured sharpness in the FITS header, so\n"
                "picking them does not mean measuring everything again."
            )
        )
        self.btn_burst.toggled.connect(self._burst_toggled)
        c.add(self.btn_burst)
        self.prog_burst = QProgressBar()
        self.prog_burst.setTextVisible(False)
        self.prog_burst.setFixedHeight(10)
        c.add(self.prog_burst)
        self.sp_burst_sec = QDoubleSpinBox()
        self.sp_burst_sec.setRange(0.0, 600.0)
        self.sp_burst_sec.setDecimals(0)
        self.sp_burst_sec.setValue(self.settings.lucky_burst_seconds)
        self.sp_burst_sec.setSuffix(" s")
        self.sp_burst_sec.valueChanged.connect(lambda x: self.win._req(burst_seconds=x))
        c.field(
            _("length"),
            self.sp_burst_sec,
            _("0 removes the time limit — then only the frame count stops it"),
        )
        self.sp_burst_frames = QSpinBox()
        self.sp_burst_frames.setRange(0, 20000)
        self.sp_burst_frames.setValue(self.settings.lucky_burst_frames)
        self.sp_burst_frames.valueChanged.connect(
            lambda x: self.win._req(burst_frames=x)
        )
        c.field(
            _("frames"),
            self.sp_burst_frames,
            _("0 removes the frame limit. Whichever limit comes first ends the burst"),
        )
        self.chk_burst_rice = QCheckBox(_("compress the burst (RICE)"))
        self.chk_burst_rice.setChecked(self.settings.lucky_burst_compress)
        self.chk_burst_rice.setToolTip(
            _(
                "Off by default, unlike a deep-sky session. Measured at bin1 it\n"
                "costs 119 ms a frame against 32 ms, which caps the burst at 8 fps\n"
                "instead of 31, and saves 40% of the space. Lucky imaging is bought\n"
                "in frames, so the trade normally goes the other way here — turn it\n"
                "on for long exposures, where the frame rate is not the limit."
            )
        )
        c.add(self.chk_burst_rice)
        self.lbl_burst = QLabel("")
        self.lbl_burst.setFont(T_MONO())
        self.lbl_burst.setWordWrap(True)
        self.lbl_burst.setVisible(False)
        c.add(self.lbl_burst)
        v.addWidget(c)

        c = Card(_("view"))
        self.chk_follow = QCheckBox(_("keep the body centred"))
        self.chk_follow.setChecked(self.settings.lucky_follow)
        self.chk_follow.setToolTip(
            _(
                "At the magnification a planet needs, wind and seeing walk it\n"
                "across the screen — and judging focus on something that will not\n"
                "stay still is guesswork. This keeps the view on the body instead\n"
                "of on the sensor: the image does not move, the window onto it\n"
                "does.\n\n"
                "Display only. The recorded frames are untouched, and the stack\n"
                "aligns them afterwards on its own."
            )
        )
        self.chk_follow.toggled.connect(self._follow_toggled)
        c.add(self.chk_follow)
        self.sl_lucky_white, wwh = self.win._slider(
            _("white point"), *LUCKY_WHITE, 100, 100.0
        )
        self.sl_lucky_white.setToolTip(
            _(
                "Which fraction of full scale comes out white. A gibbous Moon at a "
                "safe exposure only reaches half the range, and at 1.00 it is a grey "
                "disc on screen while the data underneath is fine."
            )
        )
        c.add(wwh)
        self.btn_view_white = QPushButton(_("Fit to this frame"))
        self.btn_view_white.setToolTip(
            _("Sets the white point from the brightest part of the current frame.")
        )
        self.btn_view_white.clicked.connect(self._lucky_fit_white)
        c.add(self.btn_view_white)
        self.sl_lucky_gamma, wga = self.win._slider(
            _("gamma"), *LUCKY_GAMMA, int(round(self.settings.lucky_gamma * 100)), 100.0
        )
        self.sl_lucky_gamma.setToolTip(
            _(
                "Below 1.00 opens up the maria and the terminator without touching "
                "the highlights. It is display only — the recorded frames are raw."
            )
        )
        c.add(wga)
        v.addWidget(c)

        v.addWidget(self._card_colour())
        v.addStretch(1)
        return w

    def _card_colour(self) -> Card:
        c = Card(_("colour"))
        self.sl_lucky_r, wr = self.win._slider(_("red"), 30, 300, 100, 100.0)
        self.sl_lucky_b, wb = self.win._slider(_("blue"), 30, 300, 100, 100.0)
        for sl, value in (
            (self.sl_lucky_r, self.settings.lucky_wb_red),
            (self.sl_lucky_b, self.settings.lucky_wb_blue),
        ):
            sl.setValue(int(round(value * 100)))
            sl.setToolTip(
                _(
                    "Green stays at 1.00: on a Bayer sensor it has twice the "
                    "photosites and is the least noisy of the three, so it is the "
                    "one worth measuring the other two against."
                )
            )
        c.add(wr)
        c.add(wb)
        row = QHBoxLayout()
        self.btn_body_balance = QPushButton(_("Balance on the disc"))
        self.btn_body_balance.setToolTip(
            _(
                "Matches the three channel medians over the lit disc. Measured on "
                "the disc and not on the frame: the frame is mostly black sky, whose "
                "median says nothing about colour.\n\n"
                "The Moon only. Grey-world is a measurement there and an error "
                "anywhere else — Mars is red, and neutralising it would be "
                "correcting the camera for the planet."
            )
        )
        self.btn_body_balance.clicked.connect(self._lucky_balance)
        self.btn_body_neutral = QPushButton(_("neutral"))
        self.btn_body_neutral.setToolTip(_("returns both gains to 1.00"))
        self.btn_body_neutral.clicked.connect(
            lambda: [sl.setValue(100) for sl in (self.sl_lucky_r, self.sl_lucky_b)]
        )
        row.addWidget(self.btn_body_balance, 1)
        row.addWidget(self.btn_body_neutral)
        c.add_layout(row)
        self.sl_lucky_sat, wsa = self.win._slider(_("saturation"), 0, 40, 10, 10.0)
        self.sl_lucky_sat.setValue(int(round(self.settings.lucky_saturation * 10)))
        self.sl_lucky_sat.setToolTip(
            _(
                "The mineral Moon lives at 2-3: the colour is real — titanium in "
                "the blue maria, iron oxide in the orange ones — and only a few "
                "percent apart, so it takes amplifying to be seen at all."
            )
        )
        c.add(wsa)
        self._sync_body_widgets()
        return c

    def context(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        c = Card(_("histogram"))
        self.hist3 = pg.PlotWidget()
        self.hist3.setLogMode(False, True)
        self.hist3_rgb = [self.hist3.plot() for _ in range(3)]
        self.hist3_black = pg.InfiniteLine(angle=90)
        self.hist3.addItem(self.hist3_black)
        self.hist3_white = pg.InfiniteLine(angle=90)
        self.hist3.addItem(self.hist3_white)
        c.add(self.hist3, 1)
        h.addWidget(c, 1)

        c = Card(_("this frame"))
        grid = QWidget()
        g = QVBoxLayout(grid)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(1)
        self.st_peak = Stat(_("peak"), "—", min_width=150)
        self.st_clipped = Stat(_("clipped"), "—", min_width=150)
        self.st_sharp = Stat(_("sharpness"), "—", min_width=150)
        self.st_window = Stat(_("window"), "—", min_width=150)
        for st in (self.st_peak, self.st_clipped, self.st_sharp, self.st_window):
            g.addWidget(st)
        c.add(grid)
        c.setMaximumWidth(210)
        h.addWidget(c)

        c = Card(_("this body"))
        grid = QWidget()
        g = QVBoxLayout(grid)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(1)
        self.st_body_phase = Stat(_("phase"), "—", min_width=200)
        self.st_body_alt = Stat(_("altitude"), "—", min_width=200)
        self.st_body_size = Stat(_("disc"), "—", min_width=200)
        for st in (self.st_body_phase, self.st_body_alt, self.st_body_size):
            g.addWidget(st)
        c.add(grid)
        c.setMaximumWidth(250)
        h.addWidget(c)
        return w

    @property
    def body(self) -> str:
        return self.cb_body.currentData() or "moon"

    def body_now(self, max_age: float = 30.0):
        if (
            self.body_state is not None
            and self.body_state.body == self.body
            and time.monotonic() - self.body_t < max_age
        ):
            return self.body_state
        try:
            self.body_state = lucky.body_at(
                self.body,
                self.settings.latitude,
                self.settings.longitude,
                elevation_m=self.settings.elevation_m,
            )
        except Exception as e:
            self.win.on_log(
                _("could not compute {body}: {error}").format(
                    body=lucky.BODIES[self.body].label, error=e
                )
            )
            self.body_state = None
        self.body_t = time.monotonic()
        return self.body_state

    def _body_changed(self, *_args) -> None:
        self.body_state = None
        self.body_t = 0.0
        self.follow_ema = None
        self.win._req(body=self.body)
        self.win._flag("reset_focus_best")
        self._sync_body_widgets()
        self.refresh_body()

    def _sync_body_widgets(self) -> None:
        name = lucky.BODIES[self.body].label
        self.btn_body_point.setText(_("Point at {body}").format(body=name))
        self.btn_body_preset.setText(_("Defaults for {body}").format(body=name))
        self.btn_body_balance.setEnabled(self.body == "moon")

    def refresh_body(self) -> None:
        m = self.body_now()
        if m is None:
            self.lbl_body.setText(_("ephemeris unavailable"))
            return
        self.lbl_body.setText(m.summary())
        self.lbl_body_fit.setText(self._body_fit_text(m))
        self.st_body_phase.set(
            _("{phase}, {pct:.0f}%").format(phase=m.phase_name(), pct=m.illum * 100)
        )
        self.st_body_alt.set(
            _("{alt:+.0f}° · {compass}").format(alt=m.alt, compass=compass_point(m.az))
        )
        self.st_body_size.set(
            f"{m.diameter_arcmin:.1f}'"
            if m.is_moon
            else _('{arcsec:.1f}"').format(arcsec=m.diameter_arcmin * 60)
        )

    def _body_fit_text(self, m) -> str:
        if not m.is_moon:
            px = m.disc_px(self.win.pixel_scale())
            return _('{px:.0f} px across at {scale:.2f}"/px').format(
                px=px, scale=self.win.pixel_scale()
            )
        frac = m.frame_fraction(self.win._fov_arcmin())
        return (
            _("disc {pct:.0f}% of the short side of the frame").format(pct=frac * 100)
            if frac <= 1.0
            else _("disc {times:.1f}x the short side — it is a mosaic").format(
                times=frac
            )
        )

    def body_point(self) -> None:
        m = self.body_now(max_age=0.0)
        if m is None:
            return
        if not m.up:
            self.win.on_log(
                _("{body} is {alt:.0f}° below the horizon").format(
                    body=m.name, alt=-m.alt
                )
            )
        self.body_target = True
        self.body_track = time.monotonic()
        self.win._target_name = m.name
        self.win.st_target.set(self.win._target_name)
        self.win._apply_target(m.as_target())
        self.win.rail.select("frame")

    def _body_preset(self) -> None:
        s = self.settings
        exposure, missing = lucky.exposure_for(self.body, s.lucky_exposure_s)
        self.win.sp_exp.setValue(exposure)
        self.win.sp_gain.setValue(s.lucky_gain)
        self.win.cb_bin.setCurrentText(str(s.lucky_binning))
        self.win.on_log(
            _(
                "{body}: {exp:.4f}s, gain {gain}, bin{bin} — now adjust "
                "the exposure by the histogram"
            ).format(
                body=lucky.BODIES[self.body].label,
                exp=exposure,
                gain=s.lucky_gain,
                bin=s.lucky_binning,
            )
        )
        if missing > 1.05:
            self.win.on_log(
                _(
                    "{body} wants {factor:.0f}x more light than {exp:.3f}s "
                    "gives; past that the exposure stops freezing the "
                    "seeing, so raise the gain instead"
                ).format(
                    body=lucky.BODIES[self.body].label,
                    factor=missing,
                    exp=lucky.FREEZE_S,
                )
            )

    def _apply_suggested_exposure(self) -> None:
        m = self.win._last_stats.get("lucky") or {}
        factor = m.get("factor")
        if not factor or not np.isfinite(factor):
            self.win.on_log(_("no frame measured yet"))
            return
        new = float(np.clip(self.win.sp_exp.value() * factor, 0.001, 600.0))
        self.win.sp_exp.setValue(new)
        self.win.on_log(
            _("exposure {exp:.4f}s ({factor:.2f}x)").format(exp=new, factor=factor)
        )

    def _follow_toggled(self, on: bool) -> None:
        if not on:
            self.follow_ema = None
        if not on or self.win._view == "map":
            return
        m = self.win._last_stats.get("lucky") or {}
        cx, cy, side = m.get("cx"), m.get("cy"), m.get("window", 0)
        if not side or cx is None or cy is None or not np.isfinite(cx):
            return
        self.follow_ema = (float(cx), float(cy))
        half = side * FOLLOW_ZOOM / 2.0
        if self.win.image.vb.viewRect().width() > side * FOLLOW_ZOOM:
            self.win.image.vb.setRange(
                xRange=(cx - half, cx + half), yRange=(cy - half, cy + half), padding=0
            )

    def _follow_body(self, m: dict) -> None:
        cx, cy = m.get("cx", float("nan")), m.get("cy", float("nan"))
        if not (np.isfinite(cx) and np.isfinite(cy)):
            return
        if self.follow_ema is None:
            self.follow_ema = (cx, cy)
        else:
            px, py = self.follow_ema
            self.follow_ema = (
                px + FOLLOW_SMOOTH * (cx - px),
                py + FOLLOW_SMOOTH * (cy - py),
            )
        cx, cy = self.follow_ema
        rect = self.win.image.vb.viewRect()
        w, h = rect.width(), rect.height()
        self.win.image.vb.setRange(
            xRange=(cx - w / 2, cx + w / 2), yRange=(cy - h / 2, cy + h / 2), padding=0
        )

    def _lucky_fit_white(self) -> None:
        src = self.win.image.source()
        if src is None:
            self.win.on_log(_("no frame on screen"))
            return
        top = float(np.max(src))
        lo, hi = LUCKY_WHITE
        self.sl_lucky_white.setValue(int(np.clip(round(top * 100) + 2, lo, hi)))

    def _lucky_balance(self) -> None:
        if self.body != "moon":
            self.win.on_log(
                _(
                    "grey-world only means anything on the Moon: {body} "
                    "has a colour of its own"
                ).format(body=lucky.BODIES[self.body].label)
            )
            return
        src = self.win.image.source()
        if src is None or src.ndim != 3:
            self.win.on_log(_("no colour image to balance"))
            return
        lum = src.mean(axis=2)
        lit = lum >= max(float(lum.max()) * 0.25, 1e-4)
        if int(lit.sum()) < 100:
            self.win.on_log(_("no disc in the frame to balance on"))
            return
        med = [float(np.median(src[..., k][lit])) for k in range(3)]
        if min(med) <= 0:
            self.win.on_log(_("a channel has no signal on the disc"))
            return
        for sl, m in ((self.sl_lucky_r, med[0]), (self.sl_lucky_b, med[2])):
            sl.setValue(int(round(np.clip(med[1] / m, 0.3, 3.0) * 100)))
        g = self.gains()
        self.win.on_log(
            _("disc balance: R={red:.2f}  B={blue:.2f}").format(red=g[0], blue=g[2])
        )

    def set_burst_button(self, on: bool) -> None:
        self.btn_burst.blockSignals(True)
        self.btn_burst.setChecked(on)
        self.btn_burst.blockSignals(False)
        self.btn_burst.setText(_("Stop the burst") if on else _("Record burst   (R)"))
        self.win._ic(self.btn_burst, "stop" if on else "play")

    def _burst_toggled(self, on: bool) -> None:
        if on and not self.win.worker:
            self.win.on_log(_("start the capture before recording a burst"))
            self.set_burst_button(False)
            return
        self.set_burst_button(on)
        if on:
            self.win._req(
                target_name=(self.win._target_name or lucky.BODIES[self.body].label)
            )
        self.win._flag("burst_start" if on else "burst_stop")

    def show_frame(self, m: dict) -> None:
        p = self.win.pal
        peak = m.get("peak", float("nan"))
        clipped = m.get("clipped", 0.0)
        colour = (
            p.bad
            if clipped > 0.001
            else (p.ok if peak > lucky.HEADROOM * 0.5 else p.warn)
        )
        self.st_peak.set(f"{peak * 100:.0f}%" if np.isfinite(peak) else "—", colour)
        self.st_clipped.set(f"{clipped * 100:.2f}%", p.bad if clipped > 0.001 else None)
        sharp = m.get("sharpness", float("nan"))
        self.st_sharp.set(f"{sharp:.1f}" if np.isfinite(sharp) else "—")
        side = int(m.get("window", 0))
        self.st_window.set(_("{px} px").format(px=side) if side else _("whole frame"))
        self.lbl_body_exp.setText(m.get("advice", ""))
        self.lbl_body_exp.setStyleSheet(f"color: {colour}")

        if self.chk_follow.isChecked():
            self._follow_body(m)

        recording = bool(m.get("recording"))
        self.prog_burst.setValue(int(m.get("progress", 0.0) * 100))
        self.win.st_integ.set(hms(m.get("elapsed", 0.0)))
        self.win.st_frames.set(str(m.get("frames", 0)))
        self.win._set_state("recording" if recording else "live")
        if recording:
            self.lbl_burst.setVisible(True)
            self.lbl_burst.setText(
                _("{n} frames · {seconds:.0f}s · {folder}").format(
                    n=m.get("frames", 0),
                    seconds=m.get("elapsed", 0.0),
                    folder=shorten(m.get("folder", "")),
                )
            )

    def on_burst(self, on: bool) -> None:
        self.set_burst_button(on)
        if not on:
            self.prog_burst.setValue(0)

    def gains(self) -> list[float]:
        return [
            self.sl_lucky_r.value() / self.sl_lucky_r._div,
            1.0,
            self.sl_lucky_b.value() / self.sl_lucky_b._div,
        ]

    def stretch_for_display(self, src) -> np.ndarray:
        q = self.win.image._q
        white = max(self.sl_lucky_white.value() / self.sl_lucky_white._div, 0.02)
        gamma = self.sl_lucky_gamma.value() / self.sl_lucky_gamma._div
        sat = self.sl_lucky_sat.value() / self.sl_lucky_sat._div
        gains = self.gains() if q.ndim == 3 else [1.0]
        level = np.arange(65536, dtype=np.float32) / 65535.0

        def table(gain: float) -> np.ndarray:
            t = np.clip(level * gain / white, 0.0, 1.0)
            return stretch.to_uint8(t**gamma if abs(gamma - 1.0) > 1e-3 else t)

        if q.ndim == 2:
            out = np.repeat(table(1.0)[q][..., None], 3, axis=2)
        else:
            out = np.empty(q.shape, np.uint8)
            for k in range(3):
                out[..., k] = table(gains[k])[q[..., k]]
        if abs(sat - 1.0) > 1e-3:
            out = stretch.saturate_u8(out, sat)
        self.win.image._black = 0.0
        night = image_lut(self.win.pal)
        return night[out[..., 1]] if night is not None else out

    def store(self) -> None:
        s = self.settings
        s.lucky_burst_seconds = self.sp_burst_sec.value()
        s.lucky_burst_frames = self.sp_burst_frames.value()
        s.lucky_burst_compress = self.chk_burst_rice.isChecked()
        s.lucky_gamma = self.sl_lucky_gamma.value() / self.sl_lucky_gamma._div
        s.lucky_wb_red, _g, s.lucky_wb_blue = self.gains()
        s.lucky_saturation = self.sl_lucky_sat.value() / self.sl_lucky_sat._div
        s.lucky_body = self.body
        s.lucky_follow = self.chk_follow.isChecked()
