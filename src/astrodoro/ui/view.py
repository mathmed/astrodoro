from __future__ import annotations

import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core import stretch
from ..i18n import gettext as _
from .design import T_MONO, T_SMALL, ElidedLabel, hms, image_lut
from .loupe import Loupe
from .panels.integrate import STRETCH_PRESETS

HISTOGRAM_SKY_SPAN = 3.0
HISTOGRAM_TAIL_PERCENTILE = 99.9
SATURATED_FRACTION = 0.001


class RealignArrow(pg.GraphicsObject):
    def __init__(self):
        super().__init__()
        self._center = QPointF(0, 0)
        self._tip = QPointF(0, 0)
        self._shaft = QPolygonF()
        self._head = QPolygonF()
        self._reticle = 0.0
        self._color = QColor("#ffffff")
        self._label = ""
        self._pointing = False
        self._rect = QRectF()
        self._font = QFont()
        self._font.setPointSizeF(11.0)

    def set_info(
        self,
        cx: float,
        cy: float,
        dx: float,
        dy: float,
        *,
        ok: bool,
        on_target: bool,
        color: str,
        label: str,
    ) -> None:
        self.prepareGeometryChange()
        self._center = QPointF(cx, cy)
        self._color = QColor(color)
        self._label = label
        self._pointing = ok and not on_target
        half = max(min(cx, cy), 1.0)
        if self._pointing:
            dist = float(np.hypot(dx, dy))
            r = min(dist, half * 0.85)
            ang = float(np.arctan2(dy, dx))
            tx, ty = cx + r * np.cos(ang), cy + r * np.sin(ang)
            self._tip = QPointF(tx, ty)
            self._reticle = max(half * 0.012, 5.0)
            width = max(half * 0.010, 4.0)
            perp = ang + np.pi / 2
            ox, oy = width * np.cos(perp), width * np.sin(perp)
            head = min(max(half * 0.04, 10.0), r * 0.6)
            bx, by = tx - head * np.cos(ang), ty - head * np.sin(ang)
            self._shaft = QPolygonF(
                [
                    QPointF(cx + ox, cy + oy),
                    QPointF(cx - ox, cy - oy),
                    QPointF(bx - ox, by - oy),
                    QPointF(bx + ox, by + oy),
                ]
            )
            hw = head * 0.6
            self._head = QPolygonF(
                [
                    self._tip,
                    QPointF(bx + hw * np.cos(perp), by + hw * np.sin(perp)),
                    QPointF(bx - hw * np.cos(perp), by - hw * np.sin(perp)),
                ]
            )
            pad = head + 40
            xs = (cx, tx)
            ys = (cy, ty)
        else:
            self._tip = self._center
            self._shaft = QPolygonF()
            self._head = QPolygonF()
            self._reticle = 0.0
            r = max(half * 0.02, 6.0)
            pad = r + 40
            xs = (cx - r, cx + r)
            ys = (cy - r, cy + r)
        self._rect = QRectF(
            min(xs) - pad,
            min(ys) - pad,
            max(xs) - min(xs) + 2 * pad,
            max(ys) - min(ys) + 2 * pad,
        )
        self.update()

    def boundingRect(self) -> QRectF:
        return self._rect

    def paint(self, p: QPainter, _opt, _widget=None) -> None:
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._pointing:
            p.setPen(QPen(self._color, 1.5))
            p.setBrush(self._color)
            p.drawPolygon(self._shaft)
            p.drawPolygon(self._head)
            k = self._reticle
            p.drawLine(
                QPointF(self._center.x() - k, self._center.y()),
                QPointF(self._center.x() + k, self._center.y()),
            )
            p.drawLine(
                QPointF(self._center.x(), self._center.y() - k),
                QPointF(self._center.x(), self._center.y() + k),
            )
        else:
            p.setPen(QPen(self._color, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            r = max(min(self._center.x(), self._center.y()) * 0.02, 6.0)
            p.drawEllipse(self._center, r, r)
        if self._label:
            p.setFont(self._font)
            p.setPen(self._color)
            anchor = self._tip if self._pointing else self._center
            p.drawText(anchor + QPointF(10, -10), self._label)


def _histogram_top(channels: list) -> float:
    sky = max(float(np.median(c)) for c in channels)
    tail = max(float(np.percentile(c, HISTOGRAM_TAIL_PERCENTILE)) for c in channels)
    top = max(HISTOGRAM_SKY_SPAN * sky, tail, 1e-4)
    if any(float((c >= 0.99).mean()) > SATURATED_FRACTION for c in channels):
        top = max(top, 1.0)
    return top


class ImageView(QWidget):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self._q = None
        self._q_src = None
        self._black = 0.0
        self._revisit: dict | None = None
        self._loupe_xy: tuple[float, float] | None = None

        # The bar is built here but lives outside this widget: the shell puts
        # it above the canvas, which swaps between the image, the sky map and
        # the target list. Inside, it would disappear with the image.
        self.bar = self._bar()

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.view = pg.GraphicsLayoutWidget()
        self.vb = self.view.addViewBox(lockAspect=True, invertY=True)
        self.img = pg.ImageItem()
        self.vb.addItem(self.img)
        self.realign_arrow = RealignArrow()
        self.realign_arrow.setVisible(False)
        self.vb.addItem(self.realign_arrow)
        v.addWidget(self.view, 1)

        self.loupe = Loupe(self.view)
        self.loupe.setVisible(False)
        self.loupe.closed.connect(lambda: self.btn_loupe.setChecked(False))
        self.loupe.beep_toggled.connect(lambda on: setattr(win.beeper, "enabled", on))
        self.loupe.reset_best.connect(lambda: win._flag("reset_focus_best"))
        self.loupe.auto_star.connect(lambda: self.pin_loupe(None))
        self.view.installEventFilter(self)
        self.vb.scene().sigMouseClicked.connect(self._image_clicked)

    def _bar(self) -> QWidget:
        win = self.win
        bar = QWidget()
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(2, 0, 2, 0)
        hb.setSpacing(6)
        self.btn_view_stack = QPushButton(_("Stack"))
        self.btn_view_live = QPushButton(_("Last frame"))
        self.btn_view_map = QPushButton(_("Sky map   (M)"))
        for b, mode in (
            (self.btn_view_stack, "stack"),
            (self.btn_view_live, "live"),
            (self.btn_view_map, "map"),
        ):
            b.setCheckable(True)
            b.clicked.connect(lambda _checked=False, m=mode: win._set_view(m))
            hb.addWidget(b)
        win._ic(self.btn_view_stack, "stack", 15)
        win._ic(self.btn_view_live, "camera", 15)
        win._ic(self.btn_view_map, "target", 15)

        hb.addSpacing(10)
        for label, hint, fn in (
            ("−", _("zoom out"), self.zoom_out),
            ("+", _("zoom in"), self.zoom_in),
            (_("fit"), _("fit everything on screen"), self.fit_view),
            ("1:1", _("one sensor pixel per screen pixel"), self.zoom_one),
            (
                _("save"),
                _(
                    "saves the image as it looks right now (Ctrl+S) — "
                    "a new file each time"
                ),
                self.save,
            ),
        ):
            b = QPushButton(label)
            b.setToolTip(hint)
            if len(label) == 1:
                b.setMaximumWidth(34)
            b.clicked.connect(fn)
            hb.addWidget(b)
        self.btn_loupe = QPushButton(_("loupe"))
        self.btn_loupe.setCheckable(True)
        self.btn_loupe.setToolTip(
            _(
                "5x view of a star over the image, to focus "
                "without leaving what you are doing (Z)"
            )
        )
        win._ic(self.btn_loupe, "focus", 15)
        self.btn_loupe.toggled.connect(self.toggle_loupe)
        hb.addWidget(self.btn_loupe)

        self.lbl_view = ElidedLabel("—")
        self.lbl_view.setFont(T_MONO())
        hb.addSpacing(10)
        hb.addWidget(self.lbl_view, 1)
        shortcut = QLabel(_("V toggles"))
        shortcut.setObjectName("statLabel")
        shortcut.setFont(T_SMALL())
        hb.addWidget(shortcut)
        return bar

    def source(self):
        if self._revisit is not None:
            return self._revisit["rgb"]
        if self.win._view == "live":
            return self.win._live
        return self.win._stack if self.win._stack is not None else self.win._live

    def _quantize(self, src) -> None:
        if self._q_src is src and self._q is not None:
            return
        self._q = (np.clip(src, 0.0, 1.0) * 65535.0 + 0.5).astype(np.uint16)
        self._q_src = src

    def _channel_gains(self) -> list[float]:
        if self.win._mode == "lucky":
            return self.win.lucky.gains()
        gains = self.win.integrate
        return [sl.value() / sl._div for sl in (gains.sl_r, gains.sl_g, gains.sl_b)]

    def equalize_channels(self) -> None:
        src = self.source()
        if src is None or src.ndim != 3:
            self.win.on_log(_("no colour image to equalise"))
            return
        med = [float(np.median(src[..., k])) for k in range(3)]
        if min(med) <= 0:
            self.win.on_log(_("a channel has no signal — nothing to equalise"))
            return
        panel = self.win.integrate
        sliders = (panel.sl_r, panel.sl_g, panel.sl_b)
        for sl, m in zip(sliders, med, strict=True):
            sl.setValue(int(round(np.clip(med[1] / m, 0.3, 3.0) * 100)))
        self.win.on_log(
            _("gains: {values}").format(
                values="  ".join(
                    f"{c}={g:.2f}"
                    for c, g in zip("RGB", self._channel_gains(), strict=True)
                )
            )
        )

    def stretched(self) -> np.ndarray:
        q, src = self._q, self.source()
        if self.win._mode == "lucky":
            return self.win.lucky.stretch_for_display(src)
        p = self.win.integrate.stretch_params()
        target, clip = p.target_bg, p.shadows_clip
        sat, white = p.saturation, p.white
        gains = self._channel_gains()

        if not p.auto:
            out = (q >> 8).astype(np.uint8)
        elif p.arcsinh:
            base = src
            if src.ndim == 3 and any(abs(g - 1.0) > 1e-3 for g in gains):
                base = np.clip(src * np.array(gains, np.float32), 0.0, 1.0)
            img = stretch.auto_arcsinh(
                base, target_bg=target, shadows_clip=clip, preserve_color=True
            )
            src = base
            self._black, _m = stretch.estimate_params(
                src.mean(axis=2) if src.ndim == 3 else src, target, clip, 8
            )
            out = stretch.to_uint8(stretch.saturate(img, sat))
        else:
            out = np.empty(q.shape, np.uint8)
            blacks = []
            for k in range(3):
                channel = (
                    src[..., k] * gains[k] if abs(gains[k] - 1) > 1e-3 else src[..., k]
                )
                c0, m = stretch.estimate_params(channel, target, clip, 8)
                out[..., k] = stretch.build_lut(c0, m, white, gains[k])[q[..., k]]
                blacks.append(c0)
            self._black = float(np.mean(blacks))
            if abs(sat - 1.0) > 1e-3:
                out = stretch.to_uint8(
                    stretch.saturate(out.astype(np.float32) / 255.0, sat)
                )

        lut = image_lut(self.win.pal)
        if lut is not None:
            out = lut[out.mean(axis=2).astype(np.uint8)]
        return out

    def display(self) -> np.ndarray:
        src = self.source()
        if src is None:
            return np.zeros((1, 1, 3), np.uint8)
        self._quantize(src)
        return self.stretched()

    def redraw(self, force: bool = False) -> None:
        src = self.source()
        if src is None:
            return
        self._quantize(src)
        self.img.setImage(self.stretched(), autoLevels=False, levels=(0, 255))
        if force:
            self.draw_histogram(src)
            self.win.config_window.update_scale_label()

    def draw_histogram(self, src) -> None:
        sample = src[::8, ::8]
        gains = self._channel_gains()
        if sample.ndim == 3:
            channels = [
                sample[:, :, k] * gains[k] for k in range(min(3, sample.shape[2]))
            ]
        else:
            channels = [sample]
        top = _histogram_top(channels)
        white = self.win.integrate.sl_white.value() / self.win.integrate.sl_white._div
        for lw in (self.win.integrate.hist_white, self.win.lucky.hist3_white):
            lw.setValue(white)
            lw.setVisible(white < top)
        panel = self.win.integrate
        for curves, line in (
            (panel.hist_rgb, panel.hist_black),
            (self.win.lucky.hist3_rgb, self.win.lucky.hist3_black),
        ):
            for k, curve in enumerate(curves):
                if k >= len(channels):
                    curve.setData([], [])
                    continue
                counts, edges = np.histogram(channels[k], bins=256, range=(0.0, top))
                curve.setData(edges[:-1], counts + 1)
            line.setValue(self._black)

    def _zoom(self, factor: float) -> None:
        if self.win._view == "map":
            sky = self.win.skymap
            sky.fov = float(np.clip(sky.fov / factor, 2.0, 110.0))
            self.win.skymap.update()
            return
        self.vb.scaleBy((1 / factor, 1 / factor), center=self.vb.viewRect().center())

    def zoom_in(self) -> None:
        self._zoom(1.35)

    def zoom_out(self) -> None:
        self._zoom(1 / 1.35)

    def fit_view(self) -> None:
        if self.win._view == "map":
            self.win.skymap.fov = 40.0
            self.win.skymap.update()
            return
        if self.win._mode == "lucky":
            self.win.lucky.chk_follow.setChecked(False)
        self.vb.autoRange()

    def zoom_one(self) -> None:
        if self.win._view == "map":
            self.win.skymap.fov = max(self.win.skymap.cam_fov[0] * 1.4, 0.4)
            self.win.skymap.update()
            return
        if self._q is None:
            return
        c = self.vb.viewRect().center()
        vw, vh = self.vb.width() or 800, self.vb.height() or 600
        self.vb.setRange(
            xRange=(c.x() - vw / 2, c.x() + vw / 2),
            yRange=(c.y() - vh / 2, c.y() + vh / 2),
            padding=0,
        )

    def show_frame(self, i: int) -> None:
        info = self.win.health.info[i] if 0 <= i < len(self.win.health.info) else {}
        idx = info.get("index")
        e = self.win._hist.get(idx)
        if e is None:
            self.win.on_log(
                _(
                    "frame {index} is no longer in memory — the history "
                    "keeps the last {n} ({mb:.0f} MB)"
                ).format(
                    index=idx if idx else "?",
                    n=len(self.win._hist),
                    mb=self.win._hist.mb,
                )
            )
            return
        if self.win._view == "map":
            self.win._set_view("live")
        self._revisit = {
            "i": i,
            "index": e.index,
            "info": e.info,
            "ok": bool(self.win.health.marks[i]),
            "rgb": e.rgb(),
        }
        self.win.health.select(i)
        self.win._update_view_label()
        self.redraw(True)

    def exit_review(self) -> None:
        if self._revisit is None:
            return
        self._revisit = None
        self.win.health.select(None)
        self.win._update_view_label()
        self.redraw(True)

    def apply_preset(self, name: str) -> None:
        bg, clip = STRETCH_PRESETS[name]
        self.win.integrate.chk_auto.setChecked(True)
        self.win.integrate.sl_bg.setValue(int(round(bg * 100)))
        self.win.integrate.sl_clip.setValue(int(round(clip * 10)))

    @property
    def loupe_on(self) -> bool:
        return self.btn_loupe.isChecked() and self.win._view in ("live", "stack")

    def toggle_loupe(self, on: bool) -> None:
        if on and self.win._view == "map":
            self.win._set_view("live")
        self.loupe.setVisible(self.loupe_on)
        if self.loupe_on:
            self.loupe.set_palette(self.win.pal)
            self.loupe.set_pinned(self._loupe_xy)
            self.loupe.place()

    def pin_loupe(self, xy) -> None:
        self._loupe_xy = xy
        if self.win.worker:
            self.win.worker.set_loupe(xy)
        self.loupe.set_pinned(xy)

    def _image_clicked(self, ev) -> None:
        if not self.loupe_on:
            return
        if self._q is None:
            return
        pos = self.vb.mapSceneToView(ev.scenePos())
        x, y = float(pos.x()), float(pos.y())
        h, w = self._q.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            return
        self.pin_loupe((x, y))

    def eventFilter(self, obj, ev) -> bool:
        if obj is self.view and ev.type() == QEvent.Type.Resize and self.loupe_on:
            self.loupe.place()
        return super().eventFilter(obj, ev)

    def save(self) -> None:
        if self.win._stack is None and self.win._live is None:
            self.win.on_log(_("nothing to save"))
            return
        import cv2
        from astropy.io import fits

        out = self.win._output_dir()
        st = self.win._last_stats
        if self._revisit is not None:
            name = f"frame_{self._revisit['index']:05d}_{time.strftime('%H%M%S')}"
            cv2.imwrite(
                str(out / f"{name}.png"),
                cv2.cvtColor(self.display(), cv2.COLOR_RGB2BGR),
            )
            self.win.on_log(_("saved: {path}").format(path=out / f"{name}.png"))
            return
        n = st.get("n_stacked", 0)
        name = f"stack_{time.strftime('%H%M%S')}"
        if n:
            name += f"_{n}f_{hms(st.get('integration', 0.0))}"
        cv2.imwrite(
            str(out / f"{name}.png"), cv2.cvtColor(self.display(), cv2.COLOR_RGB2BGR)
        )
        if self.win._stack is not None:
            fits.PrimaryHDU(self.win._stack.transpose(2, 0, 1)).writeto(
                out / f"{name}.fits", overwrite=True
            )
        self.win.on_log(_("saved: {path}").format(path=out / f"{name}.png"))
