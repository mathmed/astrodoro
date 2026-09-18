from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLineEdit, QWidget

from ..i18n import N_
from ..i18n import gettext as _
from ..pointing.orientation import bearing

CARDINALS = [
    (0, N_("N")),
    (45, N_("NE")),
    (90, N_("E")),
    (135, N_("SE")),
    (180, N_("S")),
    (225, N_("SW")),
    (270, N_("W")),
    (315, N_("NW")),
]


@dataclass
class Mark:
    label: str
    enu: np.ndarray
    mag: float
    kind: str
    obj: object = None


class SkyMap(QWidget):
    align_requested = Signal(object)
    az_dragged = Signal(float)
    searched = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(_("star: aligns · object: becomes target"))
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(
            lambda: self.searched.emit(self.search.text())
        )
        self.search.resize(250, 28)
        self.search.move(10, 10)
        self.found: Mark | None = None
        self.fov = 40.0
        self.rays = None
        self.marks: list[Mark] = []
        self.target: Mark | None = None
        self.cam_fov = (55.0 / 60.0, 37.0 / 60.0)
        self.aligned = False
        self.aligned_on = ""
        self.live = False
        self.pal = None
        self.lines = np.empty((0, 2, 3))
        self.names: list = []
        self._V = np.empty((0, 3))
        self._order: list[int] = []
        self._hover: Mark | None = None
        self._drag: QPoint | None = None
        self._points: list[tuple[float, float, Mark]] = []
        self._labels: list[QRectF] = []

    def set_palette(self, pal) -> None:
        self.pal = pal
        self.update()

    def set_state(
        self, rays, marks, target, aligned, live, lines=None, names=None, aligned_on=""
    ) -> None:
        self.rays = rays
        if marks is not self.marks:
            self.marks = marks
            self._V = (
                np.array([m.enu for m in marks], dtype=float)
                if marks
                else np.empty((0, 3))
            )
            self._order = sorted(range(len(marks)), key=lambda i: marks[i].mag)
        self.target = target
        self.aligned = aligned
        self.aligned_on = aligned_on
        self.live = live
        if lines is not None:
            self.lines = lines
        if names is not None:
            self.names = names
        self.update()

    def _scale(self) -> float:
        return min(self.width(), self.height()) / max(self.fov, 1e-3)

    def _mat(self) -> np.ndarray:
        top, left, fwd = self.rays
        return np.array([-left, top, fwd])

    def _proj_many(self, V: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if len(V) == 0:
            return np.empty((0, 2)), np.zeros(0, dtype=bool)
        c = V @ self._mat().T
        vis = c[:, 2] > 0.02
        e = self._scale()
        xy = np.column_stack(
            [
                self.width() / 2 + np.degrees(np.arcsin(np.clip(c[:, 0], -1, 1))) * e,
                self.height() / 2 - np.degrees(np.arcsin(np.clip(c[:, 1], -1, 1))) * e,
            ]
        )
        return xy, vis

    def _proj(self, enu) -> tuple[float, float] | None:
        x, y, z = bearing(enu, self.rays)
        if z <= 0.02:
            return None
        e = self._scale()
        return (
            self.width() / 2 + np.degrees(np.arcsin(x)) * e,
            self.height() / 2 - np.degrees(np.arcsin(y)) * e,
        )

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.search.resize(min(250, max(160, self.width() // 4)), 28)

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pal = self.pal
        p.fillRect(self.rect(), QColor(pal.plot_bg if pal else "#000000"))
        if self.rays is None or not self.live:
            self._notice(
                p,
                _("sensor off — switch the phone on in the panel")
                if not self.live
                else "",
            )
            return

        self._points.clear()
        self._horizon(p)
        self._constellations(p)
        self._target_line(p)
        self._objects(p)
        self._reticle(p)
        self._footer(p)

    def _target_ring(self, p: QPainter, x: float, y: float, r: float) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self._color("mark"), 1.6))
        p.drawEllipse(QPointF(x, y), r, r)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            p.drawLine(
                QPointF(x + dx * r, y + dy * r),
                QPointF(x + dx * (r + 5), y + dy * (r + 5)),
            )

    def _search_brackets(self, p: QPainter, x: float, y: float, r: float) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self._color("accent"), 1.6))
        d, c = r, r * 0.55
        for sx in (-1, 1):
            for sy in (-1, 1):
                p.drawLine(
                    QPointF(x + sx * d, y + sy * d),
                    QPointF(x + sx * (d - c), y + sy * d),
                )
                p.drawLine(
                    QPointF(x + sx * d, y + sy * d),
                    QPointF(x + sx * d, y + sy * (d - c)),
                )

    def _anchor_diamond(self, p: QPainter, x: float, y: float, r: float) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(self._color("text_dim"), 1, Qt.PenStyle.DotLine))
        p.drawPolygon(
            [QPointF(x, y - r), QPointF(x + r, y), QPointF(x, y + r), QPointF(x - r, y)]
        )

    def _color(self, name: str, alpha: int = 255) -> QColor:
        c = QColor(getattr(self.pal, name) if self.pal else "#dddddd")
        c.setAlpha(alpha)
        return c

    def _horizon(self, p: QPainter) -> None:
        from ..pointing.orientation import altaz_to_enu

        p.setPen(QPen(self._color("border"), 1))
        prev = None
        for az in range(0, 361, 3):
            q = self._proj(altaz_to_enu(0.0, az))
            if q and prev:
                p.drawLine(QPointF(*prev), QPointF(*q))
            prev = q
        p.setFont(QFont(self.font().family(), 9))
        p.setPen(self._color("text_dim"))
        for az, name in CARDINALS:
            q = self._proj(altaz_to_enu(0.0, az))
            if q:
                p.drawText(QPointF(q[0] - 6, q[1] + 14), _(name))

    def _label_limit(self) -> float:
        if self.fov > 30:
            return 6.0
        return 8.0 if self.fov > 12 else 11.0

    def _fits(self, x: float, y: float, text: str) -> bool:
        width = 6.4 * len(text)
        r = QRectF(x, y - 9, width, 13)
        for other in self._labels:
            if r.intersects(other):
                return False
        self._labels.append(r)
        return True

    def _constellations(self, p: QPainter) -> None:
        if len(self.lines) == 0:
            return
        xy, vis = self._proj_many(self.lines.reshape(-1, 3))
        xy = xy.reshape(-1, 2, 2)
        vis = np.asarray(vis.reshape(-1, 2).all(axis=1))
        p.setPen(QPen(self._color("border"), 1))
        for a, b in xy[vis]:
            p.drawLine(QPointF(a[0], a[1]), QPointF(b[0], b[1]))
        if self.names and self.fov > 12:
            V = np.array([v for _n, v in self.names])
            nxy, nvis = self._proj_many(V)
            p.setFont(QFont(self.font().family(), 8))
            p.setPen(self._color("border"))
            for (name, _v), q, ok in zip(self.names, nxy, nvis, strict=True):
                if ok:
                    p.drawText(QPointF(q[0], q[1]), name.upper())

    def _target_line(self, p: QPainter) -> None:
        if self.target is None:
            return
        cx, cy = self.width() / 2, self.height() / 2
        q = self._proj(self.target.enu)
        if q is None:
            x, y, _z = bearing(self.target.enu, self.rays)
            v = -np.array([x, -y])
            n = float(np.linalg.norm(v))
            if n < 1e-6:
                return
            v = v / n
            r = min(cx, cy) - 26
            q = (cx + v[0] * r, cy + v[1] * r)
        if abs(q[0] - cx) < 3 and abs(q[1] - cy) < 3:
            return
        p.setPen(QPen(self._color("mark", 150), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(cx, cy), QPointF(q[0], q[1]))

    def _objects(self, p: QPainter) -> None:
        p.setFont(QFont(self.font().family(), 9))
        self._labels = []
        limit = self._label_limit()
        xy, vis = self._proj_many(self._V)
        for i in self._order:
            if not vis[i]:
                continue
            m = self.marks[i]
            x, y = xy[i]
            if not (-40 < x < self.width() + 40 and -40 < y < self.height() + 40):
                continue
            self._points.append((x, y, m))
            is_target = self.target is not None and m is self.target
            is_found = self.found is not None and m.label == self.found.label
            is_anchor = bool(self.aligned_on) and m.label == self.aligned_on
            forced = is_target or is_found or is_anchor or m is self._hover
            if m.kind == "star":
                r = max(1.5, 6.4 - 1.05 * m.mag)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(self._color("mark" if is_target else "text"))
                p.drawEllipse(QPointF(x, y), r, r)
                if is_target:
                    self._target_ring(p, x, y, r + 6)
                if is_found:
                    self._search_brackets(p, x, y, r + 8)
                if is_anchor:
                    self._anchor_diamond(p, x, y, r + 12)
                name = m.label
                if name and (forced or m.mag <= min(2.5, limit - 2.0)):
                    if forced or self._fits(x + r + 4, y + 4, name):
                        p.setPen(self._color("mark" if is_target else "text_dim"))
                        p.drawText(QPointF(x + r + 4, y + 4), name)
            else:
                color = self._color("mark" if is_target else "accent")
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(color, 1.2))
                rr = 4.5
                p.drawEllipse(QPointF(x, y), rr, rr * 0.72)
                if is_target:
                    self._target_ring(p, x, y, rr + 5)
                if is_found:
                    self._search_brackets(p, x, y, rr + 9)
                if forced or m.mag <= limit:
                    if forced or self._fits(x + 8, y + 4, m.label):
                        p.setPen(color)
                        p.drawText(QPointF(x + 8, y + 4), m.label)

        for edge_mark, role in ((self.target, "mark"), (self.found, "accent")):
            if edge_mark is None:
                continue
            q = self._proj(edge_mark.enu)
            margin = 24
            inside = (
                q is not None
                and margin < q[0] < self.width() - margin
                and margin < q[1] < self.height() - margin
            )
            if not inside:
                self._edge_arrow(p, edge_mark, role)

    def _edge_arrow(self, p: QPainter, m: Mark, color: str = "mark") -> None:
        x, y, z = bearing(m.enu, self.rays)
        cx, cy = self.width() / 2, self.height() / 2
        v = np.array([x, -y]) if z > 0 else -np.array([x, -y])
        n = float(np.linalg.norm(v))
        if n < 1e-6:
            return
        v = v / n
        r = min(cx, cy) - 26
        px, py = cx + v[0] * r, cy + v[1] * r
        p.setPen(QPen(self._color(color), 2))
        p.setBrush(self._color(color))
        ang = np.arctan2(v[1], v[0])
        pts = [
            QPointF(px + 9 * np.cos(ang), py + 9 * np.sin(ang)),
            QPointF(px + 7 * np.cos(ang + 2.5), py + 7 * np.sin(ang + 2.5)),
            QPointF(px + 7 * np.cos(ang - 2.5), py + 7 * np.sin(ang - 2.5)),
        ]
        p.drawPolygon(pts)
        p.setFont(QFont(self.font().family(), 9))
        name = m.label if len(m.label) <= 22 else m.label[:21] + "…"
        p.drawText(QPointF(px - 20 + v[0] * 14, py - 12 + v[1] * 14), name)

    def _reticle(self, p: QPainter) -> None:
        cx, cy = self.width() / 2, self.height() / 2
        e = self._scale()
        p.setPen(QPen(self._color("text_dim"), 1))
        p.drawLine(QPointF(cx - 14, cy), QPointF(cx - 5, cy))
        p.drawLine(QPointF(cx + 5, cy), QPointF(cx + 14, cy))
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy - 5))
        p.drawLine(QPointF(cx, cy + 5), QPointF(cx, cy + 14))
        w = max(self.cam_fov[0] * e, 6.0)
        h = max(self.cam_fov[1] * e, 4.0)
        p.setPen(QPen(self._color("text_dim"), 1, Qt.PenStyle.DashLine))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(QRectF(cx - w / 2, cy - h / 2, w, h))

    def _footer(self, p: QPainter) -> None:
        band = QColor(self.pal.plot_bg if self.pal else "#000000")
        band.setAlpha(210)
        p.fillRect(QRectF(0, self.height() - 22, self.width(), 22), band)
        p.setFont(QFont(self.font().family(), 9))
        p.setPen(self._color("text_dim"))
        p.drawText(
            QPointF(10, self.height() - 10),
            _(
                "field {fov:.0f}°  ·  scroll to zoom  ·  click a star to "
                "align, or search by name above"
            ).format(fov=self.fov),
        )
        if self.aligned:
            text = (
                _("aligned on {star}").format(star=self.aligned_on)
                if self.aligned_on
                else _("aligned")
            )
            color = "ok"
        else:
            text, color = _("not aligned"), "warn"
        p.setPen(self._color(color))
        p.drawText(
            QPointF(self.width() - 9.0 * len(text) - 14, self.height() - 10), text
        )

    def _notice(self, p: QPainter, text: str) -> None:
        p.setPen(self._color("text_dim"))
        p.drawText(
            self.rect(), Qt.AlignmentFlag.AlignCenter, text or _("no sensor reading")
        )

    def _pick(self, pos) -> Mark | None:
        best, dist = None, 16.0
        for x, y, m in self._points:
            d = float(np.hypot(x - pos.x(), y - pos.y()))
            if d < dist:
                best, dist = m, d
        return best

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        m = self._pick(ev.position())
        if m is not None and m.kind == "star":
            self.align_requested.emit(m)
            return
        self._drag = ev.position().toPoint()

    def mouseMoveEvent(self, ev) -> None:
        hover = self._pick(ev.position())
        if hover is not self._hover:
            self._hover = hover
            self.update()
        if self._drag is None:
            return
        dx = ev.position().toPoint().x() - self._drag.x()
        self._drag = ev.position().toPoint()
        if dx:
            self.az_dragged.emit(dx / self._scale())

    def mouseReleaseEvent(self, ev) -> None:
        self._drag = None

    def wheelEvent(self, ev) -> None:
        d = ev.angleDelta().y()
        if d:
            self.fov = float(np.clip(self.fov * (0.88 if d > 0 else 1.14), 2.0, 110.0))
            self.update()
