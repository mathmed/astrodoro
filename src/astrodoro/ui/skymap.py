"""The pointed sky map — the chart AstroHopper shows on the phone.

Here it lives on the laptop, in the place of the image, because that is where
the screen is: the phone is only the sensor. The centre of the map is the tube's
sighting direction; pushing the Dobsonian drags the sky under the reticle and
the target comes into the field.

Two things an ordinary chart does not have and this one needs:

- the **sensor rectangle** drawn at the centre, at its real size (55'x37' with
  this optic). It is the difference between "the target is close" and "the target
  is framed", which you cannot judge by eye on a chart;
- **clicking a star aligns on it**. It is the map's only command gesture, and in
  the dark, hunting a name in a list of hundreds is what makes people give up.
  The target is chosen in the panel, not here: two click commands on the same
  drawing, one per mouse button, is the kind of thing nobody remembers when cold.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLineEdit, QWidget

from ..i18n import N_
from ..i18n import gettext as _
from ..pointing.orientation import bearing

#: Compass directions drawn on the horizon line. Single letters, translated
#: because they differ by language (E/W vs L/O in Portuguese).
CARDINALS = [(0, N_("N")), (45, N_("NE")), (90, N_("E")), (135, N_("SE")),
             (180, N_("S")), (225, N_("SW")), (270, N_("W")), (315, N_("NW"))]


@dataclass
class Mark:
    """An object on the map, already an ENU vector so drawing does no astronomy."""
    label: str
    enu: np.ndarray
    mag: float
    kind: str          # "star" | "dso"
    obj: object = None


class SkyMap(QWidget):
    align_requested = Signal(object)   # left click on a star
    az_dragged = Signal(float)         # drag, in degrees
    searched = Signal(str)             # text typed into the search box

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        # The search box lives inside the map rather than in a panel beside it:
        # looking for a star means looking at the drawn sky, not at a form.
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(_("star: aligns · object: becomes target"))
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(
            lambda: self.searched.emit(self.search.text()))
        self.search.resize(250, 28)
        self.search.move(10, 10)
        self.found: Mark | None = None
        self.fov = 40.0                     # degrees, shorter side of the window
        self.rays = None
        self.marks: list[Mark] = []
        self.target: Mark | None = None
        self.cam_fov = (55.0 / 60.0, 37.0 / 60.0)
        self.aligned = False
        self.aligned_on = ""    # label of the star that was aligned on
        self.live = False
        self.pal = None
        self.lines = np.empty((0, 2, 3))    # constellation figures
        self.names: list = []               # (abbreviation, ENU vector)
        self._V = np.empty((0, 3))
        self._order: list[int] = []
        self._hover: Mark | None = None     # object under the cursor
        self._drag: QPoint | None = None
        self._points: list[tuple[float, float, Mark]] = []
        self._labels: list[QRectF] = []

    # ------------------------------------------------------------------ input
    def set_palette(self, pal) -> None:
        self.pal = pal
        self.update()

    def set_state(self, rays, marks, target, aligned, live,
                  lines=None, names=None, aligned_on="") -> None:
        self.rays = rays
        if marks is not self.marks:
            # Stack the vectors once, not on every redraw: projecting ~1500
            # objects becomes a single matrix multiply.
            self.marks = marks
            self._V = (np.array([m.enu for m in marks], dtype=float)
                       if marks else np.empty((0, 3)))
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

    # ------------------------------------------------------------- projection
    def _scale(self) -> float:
        return min(self.width(), self.height()) / max(self.fov, 1e-3)

    def _mat(self) -> np.ndarray:
        """Matrix taking ENU into the camera frame (x right, y up)."""
        top, left, fwd = self.rays
        return np.array([-left, top, fwd])

    def _proj_many(self, V: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Project many vectors at once.

        Vectorised out of necessity, not elegance: there are 674 constellation
        segments plus ~1500 objects, and projecting them one by one in Python
        cost 13 ms per frame — 13% of a core just to redraw the map. In matrix
        form it is 5.3 ms with everything drawn.
        """
        if len(V) == 0:
            return np.empty((0, 2)), np.zeros(0, dtype=bool)
        c = V @ self._mat().T
        vis = c[:, 2] > 0.02
        e = self._scale()
        xy = np.column_stack([
            self.width() / 2 + np.degrees(np.arcsin(np.clip(c[:, 0], -1, 1))) * e,
            self.height() / 2 - np.degrees(np.arcsin(np.clip(c[:, 1], -1, 1))) * e])
        return xy, vis

    def _proj(self, enu) -> tuple[float, float] | None:
        """ENU -> pixel. Returns None for anything behind the tube.

        Direction-sine projection (`asin`) rather than gnomonic: at fields of 60
        degrees or more the gnomonic stretches the edges absurdly, and this map
        is used precisely wide open, to work out which part of the sky you are in.
        """
        x, y, z = bearing(enu, self.rays)
        if z <= 0.02:
            return None
        e = self._scale()
        return (self.width() / 2 + np.degrees(np.arcsin(x)) * e,
                self.height() / 2 - np.degrees(np.arcsin(y)) * e)

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self.search.resize(min(250, max(160, self.width() // 4)), 28)

    # ---------------------------------------------------------------- drawing
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self.pal
        p.fillRect(self.rect(), QColor(pal.plot_bg if pal else "#000000"))
        if self.rays is None or not self.live:
            self._notice(p, _("sensor off — switch the phone on in the panel")
                         if not self.live else "")
            return

        self._points.clear()
        self._horizon(p)
        self._constellations(p)
        self._target_line(p)
        self._objects(p)
        self._reticle(p)
        self._footer(p)

    def _target_ring(self, p: QPainter, x: float, y: float, r: float) -> None:
        """Target: a reticle — a ring with four radial ticks."""
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(self._color("mark"), 1.6))
        p.drawEllipse(QPointF(x, y), r, r)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            p.drawLine(QPointF(x + dx * r, y + dy * r),
                       QPointF(x + dx * (r + 5), y + dy * (r + 5)))

    def _search_brackets(self, p: QPainter, x: float, y: float, r: float) -> None:
        """Search hit: corner brackets, like a camera's focus box."""
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(self._color("accent"), 1.6))
        d, c = r, r * 0.55
        for sx in (-1, 1):
            for sy in (-1, 1):
                p.drawLine(QPointF(x + sx * d, y + sy * d),
                           QPointF(x + sx * (d - c), y + sy * d))
                p.drawLine(QPointF(x + sx * d, y + sy * d),
                           QPointF(x + sx * d, y + sy * (d - c)))

    def _anchor_diamond(self, p: QPainter, x: float, y: float, r: float) -> None:
        """Alignment anchor: a dotted diamond, a shape no sky object has."""
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(self._color("text_dim"), 1, Qt.DotLine))
        p.drawPolygon([QPointF(x, y - r), QPointF(x + r, y),
                       QPointF(x, y + r), QPointF(x - r, y)])

    def _color(self, name: str, alpha: int = 255) -> QColor:
        c = QColor(getattr(self.pal, name) if self.pal else "#dddddd")
        c.setAlpha(alpha)
        return c

    def _horizon(self, p: QPainter) -> None:
        """The horizon line and the cardinal points. Without them the map has no
        up and down, and on a Dobsonian everything is decided in alt-az."""
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
        """Down to which magnitude it is worth writing names, given the field.

        At a 40-degree field the whole catalogue labelled becomes an illegible
        smear of text; at 5 degrees there is room. The threshold follows the zoom.
        """
        if self.fov > 30:
            return 6.0
        return 8.0 if self.fov > 12 else 11.0

    def _fits(self, x: float, y: float, text: str) -> bool:
        """Avoid stacking labels on top of each other — first come, first served.

        The marks arrive ordered by brightness, so the winner is always the most
        visible object, which is what serves as a reference for finding your way.
        """
        width = 6.4 * len(text)
        r = QRectF(x, y - 9, width, 13)
        for other in self._labels:
            if r.intersects(other):
                return False
        self._labels.append(r)
        return True

    def _constellations(self, p: QPainter) -> None:
        """The figures, in a faint stroke. They are what match the map to the
        sky — a cloud of points is not recognisable, the Southern Cross is."""
        if len(self.lines) == 0:
            return
        xy, vis = self._proj_many(self.lines.reshape(-1, 3))
        xy = xy.reshape(-1, 2, 2)
        vis = vis.reshape(-1, 2).all(axis=1)
        p.setPen(QPen(self._color("border"), 1))
        for (a, b) in xy[vis]:
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
        """Connects the sighting direction to the target.

        This is what turns the map into an instruction: the line shows at a
        glance which way to push and how far is left, and shortens as you push.
        It goes under the objects so it covers none of them, and disappears on
        its own when the target reaches the centre.
        """
        if self.target is None:
            return
        cx, cy = self.width() / 2, self.height() / 2
        q = self._proj(self.target.enu)
        if q is None:
            # Target behind the tube: the line points at the border on the same
            # bearing as the arrow, instead of vanishing without explanation.
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
        p.setPen(QPen(self._color("mark", 150), 1, Qt.DashLine))
        p.drawLine(QPointF(cx, cy), QPointF(q[0], q[1]))

    def _objects(self, p: QPainter) -> None:
        p.setFont(QFont(self.font().family(), 9))
        self._labels = []
        limit = self._label_limit()
        xy, vis = self._proj_many(self._V)
        # Brightest to faintest: this decides who wins the label when two names
        # compete for the same patch of screen.
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
                p.setPen(Qt.NoPen)
                p.setBrush(self._color("mark" if is_target else "text"))
                p.drawEllipse(QPointF(x, y), r, r)
                if is_target:
                    self._target_ring(p, x, y, r + 6)
                if is_found:
                    self._search_brackets(p, x, y, r + 8)
                if is_anchor:
                    # Where the alignment came from: near here the pointing is
                    # exact, and the error grows as you move away.
                    self._anchor_diamond(p, x, y, r + 12)
                name = m.label
                if name and (forced or m.mag <= min(2.5, limit - 2.0)):
                    if forced or self._fits(x + r + 4, y + 4, name):
                        p.setPen(self._color("mark" if is_target else "text_dim"))
                        p.drawText(QPointF(x + r + 4, y + 4), name)
            else:
                color = self._color("mark" if is_target else "accent")
                p.setBrush(Qt.NoBrush)
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

        # Target (and whatever the search found) outside the map's field: an
        # arrow on the border, otherwise you hunt for a name that is behind your
        # shoulder.
        for m, color in ((self.target, "mark"), (self.found, "accent")):
            if m is None:
                continue
            q = self._proj(m.enu)
            margin = 24
            inside = (q is not None and margin < q[0] < self.width() - margin
                      and margin < q[1] < self.height() - margin)
            if not inside:
                self._edge_arrow(p, m, color)

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
        pts = [QPointF(px + 9 * np.cos(ang), py + 9 * np.sin(ang)),
               QPointF(px + 7 * np.cos(ang + 2.5), py + 7 * np.sin(ang + 2.5)),
               QPointF(px + 7 * np.cos(ang - 2.5), py + 7 * np.sin(ang - 2.5))]
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
        # The sensor's real field, at the right size — with a 6 px floor so it
        # stays visible at wide fields, where 55' is half a pixel.
        w = max(self.cam_fov[0] * e, 6.0)
        h = max(self.cam_fov[1] * e, 4.0)
        p.setPen(QPen(self._color("text_dim"), 1, Qt.DashLine))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(cx - w / 2, cy - h / 2, w, h))

    def _footer(self, p: QPainter) -> None:
        # A band behind the text: without it the footer mixes with the stars
        # below and neither can be read.
        band = QColor(self.pal.plot_bg if self.pal else "#000000")
        band.setAlpha(210)
        p.fillRect(QRectF(0, self.height() - 22, self.width(), 22), band)
        p.setFont(QFont(self.font().family(), 9))
        p.setPen(self._color("text_dim"))
        p.drawText(QPointF(10, self.height() - 10),
                   _("field {fov:.0f}°  ·  scroll to zoom  ·  click a star to "
                     "align, or search by name above").format(fov=self.fov))
        # The alignment state is always in the same corner, aligned or not:
        # reading two situations in two different places of the screen costs
        # more attention than you have with a hand on the tube.
        if self.aligned:
            text = (_("aligned on {star}").format(star=self.aligned_on)
                    if self.aligned_on else _("aligned"))
            color = "ok"
        else:
            text, color = _("not aligned"), "warn"
        p.setPen(self._color(color))
        p.drawText(QPointF(self.width() - 9.0 * len(text) - 14,
                           self.height() - 10), text)

    def _notice(self, p: QPainter, text: str) -> None:
        p.setPen(self._color("text_dim"))
        p.drawText(self.rect(), Qt.AlignCenter, text or _("no sensor reading"))

    # ----------------------------------------------------------- interaction
    def _pick(self, pos) -> Mark | None:
        best, dist = None, 16.0
        for x, y, m in self._points:
            d = float(np.hypot(x - pos.x(), y - pos.y()))
            if d < dist:
                best, dist = m, d
        return best

    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.LeftButton:
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
            self.fov = float(np.clip(self.fov * (0.88 if d > 0 else 1.14),
                                     2.0, 110.0))
            self.update()
