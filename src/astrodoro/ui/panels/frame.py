from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.tonight import compass_point
from ...i18n import N_
from ...i18n import gettext as _
from ...pointing import brightstars
from ...pointing.pushto import guide
from ..design import (
    T_BODY,
    T_DISPLAY,
    T_L,
    T_MONO,
    T_XL,
    Card,
    tag,
)
from .integrate import arrow_for

REPLAY_SPEEDS = {
    N_("as fast as possible"): 0.0,
    N_("real time"): 1.0,
    N_("4x"): 4.0,
    N_("10x"): 10.0,
}


class FramePanel:
    def __init__(self, win):
        self.win = win
        self.settings = win.settings
        self._align_picks: list = []
        self._align_i = 0
        self._align_t = 0.0

    def panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        c = Card(_("1 · sensor on the tube"))
        self.btn_sensor = QPushButton(_("Switch the sensor on"))
        self.win._ic(self.btn_sensor, "target")
        self.btn_sensor.setCheckable(True)
        self.btn_sensor.toggled.connect(self.toggle_sensor)
        c.add(self.btn_sensor)
        self.lbl_qr = QLabel()
        self.lbl_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_qr.setVisible(False)
        c.add(self.lbl_qr)
        self.lbl_url = QLabel("")
        self.lbl_url.setFont(T_MONO())
        self.lbl_url.setWordWrap(True)
        self.lbl_url.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.lbl_url.setVisible(False)
        c.add(self.lbl_url)
        self.lbl_sensor = QLabel(_("off"))
        self.lbl_sensor.setFont(T_MONO())
        self.lbl_sensor.setWordWrap(True)
        c.add(self.lbl_sensor)
        self.lbl_altaz = QLabel("")
        self.lbl_altaz.setFont(T_L())
        self.lbl_altaz.setVisible(False)
        c.add(self.lbl_altaz)
        c.add(tag(_("star to align on")))
        self.lbl_align_pick = QLabel("—")
        self.lbl_align_pick.setFont(T_MONO())
        self.lbl_align_pick.setWordWrap(True)
        self.lbl_align_pick.setToolTip(
            _(
                "The star worth aligning on right now: bright, unmistakable "
                "(no similar star beside it), comfortable to reach, and as close to "
                "the target as possible — one star corrects two axes and is most "
                "accurate around itself."
            )
        )
        c.add(self.lbl_align_pick)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_align_pick = QPushButton(_("aim at it and align"))
        self.win._ic(self.btn_align_pick, "star")
        self.btn_align_pick.setToolTip(
            _("point the tube at this star first — the alignment reads the sensor now")
        )
        self.btn_align_pick.clicked.connect(self._align_on_pick)
        self.btn_align_next = QPushButton("↻")
        self.btn_align_next.setMaximumWidth(38)
        self.btn_align_next.setToolTip(
            _("another star — this one may be behind a tree")
        )
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
        self.win._ic(b, "target")
        b.clicked.connect(lambda: self.win._set_view("map"))
        c.add(b)
        self.btn_reset_align = QPushButton(_("clear the alignment"))
        self.win._ic(self.btn_reset_align, "refresh")
        self.btn_reset_align.clicked.connect(self.reset_align)
        self.btn_reset_align.setEnabled(False)
        c.add(self.btn_reset_align)
        v.addWidget(c)

        c = Card(_("2 · camera"))
        row = QHBoxLayout()
        self.cb_source = QComboBox()
        self.cb_source.addItems([_("camera"), _("replay")])
        self.cb_source.currentIndexChanged.connect(self.win._source_changed)
        row.addWidget(self.cb_source)
        self.cb_camera = QComboBox()
        self.cb_camera.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.cb_camera.setMinimumContentsLength(8)
        row.addWidget(self.cb_camera, 1)
        self.btn_refresh = QPushButton()
        self.btn_refresh.setMaximumWidth(38)
        self.btn_refresh.setToolTip(_("look for cameras again"))
        self.win._ic(self.btn_refresh, "refresh")
        self.btn_refresh.clicked.connect(self.win.refresh_cameras)
        row.addWidget(self.btn_refresh)
        self.btn_folder = QPushButton(_("folder…"))
        self.win._ic(self.btn_folder, "folder")
        self.btn_folder.clicked.connect(self.win.pick_replay)
        self.btn_folder.setVisible(False)
        row.addWidget(self.btn_folder)
        c.add_layout(row)

        self.rep_row = QWidget()
        rr = QHBoxLayout(self.rep_row)
        rr.setContentsMargins(0, 0, 0, 0)
        self.cb_speed = QComboBox()
        for name in REPLAY_SPEEDS:
            self.cb_speed.addItem(_(name), name)
        self.cb_speed.setCurrentIndex(2)
        self.chk_loop = QCheckBox(_("loop"))
        rr.addWidget(QLabel(_("speed")))
        rr.addWidget(self.cb_speed, 1)
        rr.addWidget(self.chk_loop)
        self.rep_row.setVisible(False)
        c.add(self.rep_row)
        row = QHBoxLayout()
        for s in (0.2, 0.5, 1.0, 2.0):
            b = QPushButton(f"{s:g}s")
            b.clicked.connect(lambda _checked=False, x=s: self.win.sp_exp.setValue(x))
            row.addWidget(b)
        c.add_layout(row)
        v.addWidget(c)

        v.addStretch(1)
        return w

    def context(self) -> QWidget:
        c = Card(_("target direction"))
        self.lbl_goto_arrow = QLabel("")
        self.lbl_goto_arrow.setFont(T_DISPLAY())
        self.lbl_goto_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
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

    def toggle_sensor(self, on: bool) -> None:
        if on:
            try:
                url = self.win.hs.start()
            except OSError as e:
                self.win.on_log(_("sensor: {error}").format(error=e))
                self.btn_sensor.setChecked(False)
                return
            self.btn_sensor.setText(_("Switch the sensor off"))
            self.lbl_url.setText(url)
            self.lbl_url.setVisible(True)
            self.lbl_qr.setPixmap(self._qr(url))
            self.lbl_qr.setVisible(True)
            self.lbl_sensor.setText(_("scan the QR code with the phone"))
            self.win.on_log(_("sensor at {url}").format(url=url))
        else:
            self.win.hs.stop()
            self.win.point.reset()
            self.btn_sensor.setText(_("Switch the sensor on"))
            self.lbl_qr.setVisible(False)
            self.lbl_url.setVisible(False)
            self.lbl_url.setText("")
            self.lbl_sensor.setText(_("off"))
            self.lbl_altaz.setText("")
            self.lbl_altaz.setVisible(False)
            self.btn_reset_align.setEnabled(False)

    def _qr(self, url: str) -> QPixmap:
        import cv2

        m = cv2.QRCodeEncoder.create().encode(url)
        n = 4
        big = np.kron(m, np.ones((n, n), dtype=np.uint8))
        big = np.pad(big, 4 * n, constant_values=255)
        light, dark = self._qr_colors()
        rgb = np.where(big[:, :, None] > 127, light, dark).astype(np.uint8)
        h, w = big.shape
        img = QImage(rgb.tobytes(), w, h, 3 * w, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(img)

    def _qr_colors(self) -> tuple[np.ndarray, np.ndarray]:
        a, b = QColor(self.win.pal.text), QColor(self.win.pal.bg)
        if a.valueF() < b.valueF():
            a, b = b, a
        return (
            np.array([a.red(), a.green(), a.blue()]),
            np.array([b.red(), b.green(), b.blue()]),
        )

    @Slot(float, float, float, object, float)
    def _on_sample(self, a: float, b: float, g: float, c, t: float) -> None:
        self.win.point.feed(a, b, g, c, t)

    @Slot(int)
    def _on_handset_clients(self, n: int) -> None:
        self.lbl_qr.setVisible(n == 0)
        self.lbl_url.setVisible(n == 0)
        if n == 0:
            self.lbl_sensor.setText(_("phone disconnected — scan the QR again"))

    def _sensor_state_text(self) -> str:
        p = self.win.point
        if p.aligned:
            return (
                _("aligned on {star}").format(star=p.star.label)
                if p.star is not None
                else _("aligned")
            )
        return (
            _("compass — not aligned yet")
            if p.compass is not None
            else _("no compass — not aligned yet")
        )

    def tick(self) -> None:
        if not self.win.hs.running:
            return
        p = self.win.point
        p.lat, p.lon = self.win.settings.latitude, self.win.settings.longitude
        p.elevation_m = self.win.settings.elevation_m
        if not p.live:
            if p.age > 3.0:
                self.lbl_sensor.setText(
                    _("no reading — the phone screen went dark or the page closed")
                )
            return

        alt, az = p.altaz
        self.lbl_altaz.setText(f"alt {alt:+5.1f}°   az {az:5.1f}°  {compass_point(az)}")
        self.lbl_altaz.setVisible(True)
        self.lbl_sensor.setText(self._sensor_state_text())

        now = time.monotonic()
        if now - getattr(self, "_hs_echo", 0.0) > 0.5:
            self._hs_echo = now
            self.win.hs.send({"alt": alt, "az": az})
        if now - getattr(self, "_goto_t", 0.0) > 0.2:
            self._goto_t = now
            self.update_goto()
            self.win.integrate.update_realign_goto()
        if now - getattr(self, "_field_t", 0.0) > 2.0:
            self._field_t = now
            self.win._objects_in_field()
        if self.win._view == "map":
            self.win._update_map()
            self.win._update_view_label()

    def reset_align(self) -> None:
        self.win.point.reset()
        self.lbl_sensor.setText(self._sensor_state_text())
        self.btn_reset_align.setEnabled(False)
        if self.win._view == "map":
            self.win._update_map()
        self.win._update_view_label()
        self.update_goto()
        self.win.on_log(_("alignment cleared"))

    def align_on(self, star) -> None:
        p = self.win.point
        if not p.live:
            self.win.on_log(_("no sensor reading — is the phone connected?"))
            return
        tremor = p.steadiness()
        if tremor > 0.3:
            self.win.on_log(
                _(
                    "the tube is moving ({deg:.1f}° in the last half "
                    "second) — wait for it to settle and click again"
                ).format(deg=tremor)
            )
            return
        al = p.align_on(star)
        if al is None:
            self.win.on_log(_("could not align: no recent samples"))
            return
        self.lbl_sensor.setText(self._sensor_state_text())
        self.btn_reset_align.setEnabled(True)
        if self.win._view == "map":
            self.win._update_map()
        self.win._update_view_label()
        self.update_goto()
        self.win.on_log(_("aligned on {star}").format(star=star.full))

    def set_goto(self) -> None:
        text = self.win.targets_panel.ed_goto.text()
        cat = self.win._cat()
        o = cat.find(text) if cat else None
        if not o:
            o = brightstars.find(text)
        if not o:
            self.win.targets_panel.lbl_goto.setText(
                _("'{text}' not found").format(text=text)
            )
            return
        self.win._apply_target(o)

    def update_goto(self) -> None:
        if self.win._target is None:
            return
        here = self.pos()
        if here is None:
            self.lbl_goto_dir.setFont(T_BODY())
            self.lbl_goto_dir.setText(_("align the sensor to compute the direction"))
            return
        g = guide(
            here,
            (self.win._target.ra, self.win._target.dec),
            self.win.settings.latitude,
            self.win.settings.longitude,
            elevation_m=self.win.settings.elevation_m,
            fov_deg=self.win._fov_deg(),
        )
        self.lbl_goto_dir.setFont(T_XL())
        self.lbl_goto_dir.setText(g.text)
        color = self.win.pal.ok if g.on_target else self.win.pal.text
        self.lbl_goto_dir.setStyleSheet(f"color: {color}")
        self.lbl_goto_arrow.setText(arrow_for(g))
        self.lbl_goto_arrow.setStyleSheet(f"color: {color}")

    def refresh_align_pick(self, force: bool = False) -> None:
        if not hasattr(self, "lbl_align_pick"):
            return
        now = time.monotonic()
        if not force and (not self.win.isVisible() or now - self._align_t < 30.0):
            return
        self._align_t = now
        target = (
            (self.win._target.ra, self.win._target.dec)
            if self.win._target is not None
            else None
        )
        floor = max(self.win.targets_panel.sp_min_alt.value(), brightstars.FLOOR_ALT)
        try:
            self._align_picks = brightstars.for_alignment(
                self.win.settings.latitude,
                self.win.settings.longitude,
                target=target,
                min_alt=floor,
                elevation_m=self.win.settings.elevation_m,
            )
        except Exception as e:
            self.win.on_log(
                _("could not choose an alignment star: {error}").format(error=e)
            )
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
                    alt=max(
                        self.win.targets_panel.sp_min_alt.value(), brightstars.FLOOR_ALT
                    )
                )
            )
            return
        text = _("{star} · mag {mag:.1f}\n{alt:.0f}° up, {dir}").format(
            star=pick.star.full,
            mag=pick.star.mag,
            alt=pick.alt,
            dir=compass_point(pick.az),
        )
        if np.isfinite(pick.target_sep):
            text += "\n" + _("{deg:.0f}° from the target").format(deg=pick.target_sep)
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
        self.win._found = pick.star
        self.win._set_view("map")
        self.win._update_map()

    def pos(self) -> tuple[float, float] | None:
        if self.win.point.aligned and self.win.point.live:
            return self.win.point.radec
        return None
