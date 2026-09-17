from __future__ import annotations

import time
from datetime import datetime

import numpy as np
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateTimeEdit,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core import lucky, previews, tonight
from ...core.tonight import compass_point
from ...i18n import gettext as _
from ...pointing import brightstars, sky_vectors
from ..design import T_L, T_MONO, T_SMALL, Card, Stat, style_calendar
from ..previews import PreviewView


class TargetsPanel:
    def __init__(self, win):
        self.win = win
        self.settings = win.settings
        self._sky = None
        self._targets_t = 0.0
        self._when_guard = False
        self._bodies: list = []
        self._bodies_key: tuple | None = None
        self._preview_want = ""
        self._completer: QCompleter | None = None

    def panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        c = Card(_("1 · when"))
        row = QHBoxLayout()
        self.dt_when = QDateTimeEdit(QDateTime.currentDateTime())
        self.dt_when.setDisplayFormat("dd/MM/yy  HH:mm")
        self.dt_when.setCalendarPopup(True)
        self.dt_when.dateTimeChanged.connect(self._when_edited)
        self.btn_now = QPushButton(_("now"))
        self.btn_now.setCheckable(True)
        self.btn_now.setChecked(True)
        self.btn_now.setMaximumWidth(70)
        self.btn_now.setToolTip(_("follow the clock — untick to plan another hour"))
        self.btn_now.toggled.connect(self._now_toggled)
        row.addWidget(self.dt_when, 1)
        row.addWidget(self.btn_now)
        c.add_layout(row)
        row = QHBoxLayout()
        row.setSpacing(4)
        for label, hours in (
            (_("−1h"), -1.0),
            (_("+1h"), 1.0),
            (_("+2h"), 2.0),
            (_("+4h"), 4.0),
        ):
            b = QPushButton(label)
            b.clicked.connect(lambda _checked=False, h=hours: self._shift_when(h))
            row.addWidget(b)
        c.add_layout(row)
        self.lbl_sky = QLabel("—")
        self.lbl_sky.setFont(T_MONO())
        self.lbl_sky.setWordWrap(True)
        self.lbl_sky.setToolTip(
            _(
                "the two things that limit every target "
                "tonight: how dark it is and where the Moon "
                "is"
            )
        )
        c.add(self.lbl_sky)
        v.addWidget(c)

        c = Card(_("2 · filters"))
        self.cb_family = QComboBox()
        for key, (label, _types) in tonight.FAMILIES.items():
            self.cb_family.addItem(_(label), key)
        i = self.cb_family.findData(self.settings.target_family)
        self.cb_family.setCurrentIndex(max(i, 0))
        self.cb_family.currentIndexChanged.connect(self.refresh)
        c.field(_("type"), self.cb_family)

        self.sp_min_alt = QDoubleSpinBox()
        self.sp_min_alt.setRange(0.0, 85.0)
        self.sp_min_alt.setDecimals(0)
        self.sp_min_alt.setSuffix("°")
        self.sp_min_alt.setValue(self.settings.target_min_alt)
        self.sp_min_alt.valueChanged.connect(self.refresh)
        c.field(
            _("minimum altitude"),
            self.sp_min_alt,
            _(
                "what your horizon actually clears — trees, the neighbour's "
                "wall, the worst of the light dome"
            ),
        )

        self.sp_max_mag = QDoubleSpinBox()
        self.sp_max_mag.setRange(3.0, 16.0)
        self.sp_max_mag.setDecimals(1)
        self.sp_max_mag.setSingleStep(0.5)
        self.sp_max_mag.setValue(self.settings.target_max_mag)
        self.sp_max_mag.valueChanged.connect(self.refresh)
        c.field(_("faintest magnitude"), self.sp_max_mag)

        self.chk_fits = QCheckBox(_("only what fits in the frame"))
        self.chk_fits.setChecked(self.settings.target_fits_only)
        self.chk_fits.toggled.connect(self.refresh)
        c.add(self.chk_fits)

        self.chk_previews = QCheckBox(_("show a photo of the object"))
        self.chk_previews.setChecked(self.settings.previews_enabled)
        self.chk_previews.setToolTip(
            _(
                "DSS survey images, downloaded once and kept on disk. Untick to "
                "stop the program touching the network at all."
            )
        )
        self.chk_previews.toggled.connect(self._previews_toggled)
        c.add(self.chk_previews)
        self.btn_prefetch = QPushButton(_("cache the photos of this list"))
        self.win._ic(self.btn_prefetch, "save")
        self.btn_prefetch.setToolTip(
            _(
                "Fetch every suggestion's picture now, while there is internet — "
                "in the field there is none, and only the cache answers."
            )
        )
        self.btn_prefetch.clicked.connect(self._prefetch)
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
        self.ed_goto.returnPressed.connect(self.win.frame.set_goto)
        b = QPushButton(_("go"))
        b.setMaximumWidth(62)
        self.win._ic(b, "arrow")
        b.clicked.connect(self.win.frame.set_goto)
        self.btn_clear_target = QPushButton("✕")
        self.btn_clear_target.setMaximumWidth(38)
        self.btn_clear_target.setToolTip(_("forget the target"))
        self.btn_clear_target.clicked.connect(self.win.clear_target)
        self.btn_clear_target.setEnabled(False)
        row.addWidget(self.ed_goto, 1)
        row.addWidget(b)
        row.addWidget(self.btn_clear_target)
        c.add_layout(row)
        self.lbl_goto = QLabel(_("no target"))
        self.lbl_goto.setWordWrap(True)
        self.lbl_goto.setFont(T_MONO())
        c.add(self.lbl_goto)
        v.addWidget(c)

        v.addStretch(1)
        return w

    def context(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)

        c = Card(_("what it looks like"))
        self.preview = PreviewView()
        self.preview.setToolTip(
            _(
                "DSS survey image of the object, with your frame drawn on it — "
                "the fastest way to see whether the target fits. Downloaded once "
                "and kept on disk, so it is there with no internet in the field."
            )
        )
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
        self.win._ic(self.btn_use, "target")
        self.btn_use.setEnabled(False)
        self.btn_use.setToolTip(
            _("sets the target and opens FRAME, where the arrow says which way to push")
        )
        self.btn_use.clicked.connect(
            lambda: self.use_suggestion(self.win.targets.current())
        )
        self.btn_see = QPushButton(_("see on the map"))
        self.win._ic(self.btn_see, "star")
        self.btn_see.setEnabled(False)
        self.btn_see.clicked.connect(self._show_suggestion_on_map)
        row.addWidget(self.btn_use, 1)
        row.addWidget(self.btn_see)
        c.add_layout(row)
        h.addWidget(c, 1)

        c2 = Card(_("why it scored that"))
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
        self.st_f_fame = Stat(_("named object"), "—", min_width=132)
        for i, st in enumerate(
            (
                self.st_f_alt,
                self.st_f_window,
                self.st_f_moon,
                self.st_f_size,
                self.st_f_bright,
                self.st_f_fame,
            )
        ):
            g.addWidget(st, i % 3, i // 3)
        c2.add(grid)
        c2.setMaximumWidth(420)
        h.addWidget(c2)
        return w

    def when(self) -> datetime:
        if self.btn_now.isChecked():
            return datetime.now().astimezone()
        chosen = self.dt_when.dateTime().toPython()
        assert isinstance(chosen, datetime)
        return chosen.astimezone()

    def _now_toggled(self, on: bool) -> None:
        self.dt_when.setEnabled(not on)
        self.refresh()

    def _when_edited(self, *_args) -> None:
        if self._when_guard:
            return
        if self.btn_now.isChecked():
            self.btn_now.setChecked(False)
            return
        self.refresh()

    def _shift_when(self, hours: float) -> None:
        base = (
            self.dt_when.dateTime()
            if not self.btn_now.isChecked()
            else QDateTime.currentDateTime()
        )
        self.btn_now.setChecked(False)
        self._when_guard = True
        self.dt_when.setDateTime(base.addSecs(int(hours * 3600)))
        self._when_guard = False
        self.refresh()

    def _target_bodies(self, when: datetime) -> list:
        key = (
            when.replace(second=0, microsecond=0),
            self.settings.latitude,
            self.settings.longitude,
            self.settings.elevation_m,
        )
        if self._bodies_key != key:
            try:
                self._bodies = lucky.bodies_at(
                    self.settings.latitude,
                    self.settings.longitude,
                    when,
                    elevation_m=self.settings.elevation_m,
                )
            except Exception as e:
                self.win.on_log(
                    _("could not compute the planets: {error}").format(error=e)
                )
                self._bodies = []
            self._bodies_key = key
        return self._bodies

    def refresh(self, *_args) -> None:
        if not hasattr(self.win, "targets"):
            return
        if self.btn_now.isChecked():
            self._when_guard = True
            self.dt_when.setDateTime(QDateTime.currentDateTime())
            self._when_guard = False
        if not self.settings.has_site():
            self.lbl_sky.setText(
                _(
                    "set the observing site first — configuration, "
                    "“observing site and optics”"
                )
            )
            self.win.targets.set_rows([])
            return
        cat = self.win._cat()
        if cat is None:
            self.lbl_sky.setText(_("no catalogue — run `astrodoro catalog`"))
            self.win.targets.set_rows([])
            return
        when = self.when()
        try:
            sky = tonight.sky_at(
                self.settings.latitude,
                self.settings.longitude,
                when,
                elevation_m=self.settings.elevation_m,
            )
        except Exception as e:
            self.win.on_log(_("could not read the sky: {error}").format(error=e))
            return
        self._sky = sky
        self.lbl_sky.setText(
            "\n".join(t for t in (sky.twilight_text(), sky.moon_text()) if t)
        )

        fov = self.win._fov_arcmin()
        self.lbl_fov.setText(_("frame {w:.0f}' x {h:.0f}'").format(w=fov[0], h=fov[1]))
        rows = tonight.rank(
            cat.objs,
            sky,
            self.settings.latitude,
            fov_arcmin=fov,
            min_alt=self.sp_min_alt.value(),
            max_mag=self.sp_max_mag.value(),
            family=self.cb_family.currentData(),
            fits_only=self.chk_fits.isChecked(),
            bodies=self._target_bodies(when),
            arcsec_per_px=self.win.pixel_scale(),
        )
        self.win.targets.set_rows(rows)
        self._targets_t = time.monotonic()
        if self.win._view == "targets":
            self.win._update_view_label()
        if not rows:
            self.lbl_sug.setText(_("nothing passes these filters at this hour"))
            self.lbl_sug_why.setText(
                _(
                    "lower the minimum altitude, allow fainter objects, or try "
                    "another hour."
                )
            )

    def _previews_toggled(self, on: bool) -> None:
        self.btn_prefetch.setEnabled(on)
        cur = self.win.targets.current()
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
        f = s.factors
        frac, sb = s.field_fraction, s.surface_brightness
        left = (
            _("all night")
            if np.isinf(s.minutes_left)
            else _("{min:.0f} min").format(min=s.minutes_left)
        )
        fame = (
            o.kind_label
            if s.body
            else (
                _("Messier")
                if o.messier
                else (_("has a name") if o.common else _("catalogue number"))
            )
        )
        for st, label, factor in (
            (self.st_f_alt, _("altitude {alt:.0f}°").format(alt=s.alt), f["altitude"]),
            (self.st_f_window, left, f["window"]),
            (
                self.st_f_moon,
                _("Moon {deg:.0f}°").format(deg=s.moon_sep)
                if np.isfinite(s.moon_sep)
                else _("moonlight is no obstacle"),
                f["moon"],
            ),
            (
                self.st_f_size,
                _("frame {pct:.0f}%").format(pct=frac * 100)
                if frac
                else _("size unknown"),
                f["size"],
            ),
            (
                self.st_f_bright,
                _("{sb:.1f} mag/arcsec²").format(sb=sb)
                if np.isfinite(sb)
                else _("brightness unknown"),
                f["brightness"],
            ),
            (self.st_f_fame, fame, f["fame"]),
        ):
            st.set_label(label)
            st.set(f"{factor:.2f}", self._factor_colour(factor))
        self._request_preview(o)

    def _preview_key(self, o) -> tuple[str, float]:
        return o.name, previews.cutout_fov(o.major_arcmin, self.win._fov_arcmin())

    def _request_preview(self, o) -> None:
        if o.kind in ("Moon", "Planet"):
            self.preview.clear(_("no survey picture of a moving body"))
            return
        if not self.chk_previews.isChecked():
            self.preview.clear(_("previews are off"))
            return
        name, fov = self._preview_key(o)
        self._preview_want = name
        hit = self.win.preview_loader.request(name, o.ra, o.dec, fov)
        if hit is not None:
            self._show_preview(o, str(hit))
        else:
            self.preview.clear(_("fetching…"))

    def _show_preview(self, o, path: str) -> None:
        _name, fov = self._preview_key(o)
        fraction = previews.frame_fraction(self.win._fov_arcmin(), fov)
        frame = None if max(fraction) > 0.98 else fraction
        self.preview.show_image(path, frame, _("DSS2 · {fov:.0f}'").format(fov=fov))

    def _preview_ready(self, name: str, path: str) -> None:
        s = self.win.targets.current()
        if s is None or s.obj.name != name or self._preview_want != name:
            return
        self._show_preview(s.obj, path)

    def _preview_failed(self, name: str) -> None:
        if self._preview_want == name:
            self.preview.clear(_("no preview — offline?"))

    def _prefetch(self) -> None:
        if not self.chk_previews.isChecked():
            self.win.on_log(_("previews are off — tick the box first"))
            return
        rows = [s for s in self.win.targets._rows if not s.body]
        want = [(s.obj, *self._preview_key(s.obj)) for s in rows]
        missing = [
            (o, n, f)
            for o, n, f in want
            if self.win.preview_loader.cached(n, f) is None
        ]
        for o, name, fov in missing:
            self.win.preview_loader.request(name, o.ra, o.dec, fov)
        self.win.on_log(
            _("previews: {n} already cached, fetching {m}").format(
                n=len(want) - len(missing), m=len(missing)
            )
        )

    def _factor_colour(self, factor: float) -> str:
        p = self.win.pal
        return p.ok if factor >= 0.85 else (p.warn if factor >= 0.5 else p.bad)

    def use_suggestion(self, s) -> None:
        if s is None:
            return
        if s.body:
            self.win.lucky.cb_body.setCurrentIndex(
                self.win.lucky.cb_body.findData(s.body)
            )
            self.win.lucky.body_point()
            return
        self.win._apply_target(s.obj)
        self.ed_goto.setText(s.obj.label.split(" (")[0])
        self.win.rail.select("frame")

    def _show_suggestion_on_map(self) -> None:
        s = self.win.targets.current()
        if s is None:
            return
        self.win._found = s.obj
        self.win._set_view("map")
        self.win._update_map()

    def search(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self.win._found = None
            return
        cat = self.win._cat()
        obj = brightstars.find(text) or (cat.find(text) if cat else None)
        if obj is None and " (" in text:
            short = text.split(" (")[0]
            obj = brightstars.find(short) or (cat.find(short) if cat else None)
        if obj is None:
            self.win.on_log(_("'{text}' not found").format(text=text))
            self.win._found = None
            return
        self.win._found = obj
        v = sky_vectors(
            [obj.ra],
            [obj.dec],
            self.settings.latitude,
            self.settings.longitude,
            elevation_m=self.settings.elevation_m,
        )[0]
        alt = float(np.degrees(np.arcsin(np.clip(v[2], -1, 1))))
        az = float(np.degrees(np.arctan2(v[0], v[1])) % 360.0)
        where = (
            _("{alt:.0f}° up, {dir}").format(alt=alt, dir=compass_point(az))
            if alt > 0
            else _("below the horizon ({alt:.0f}°)").format(alt=alt)
        )
        self.win.on_log(f"{obj.label} — {where}")
        self.win._update_map()

        if isinstance(obj, brightstars.Star):
            self.win.frame.align_on(obj)
        else:
            self.ed_goto.setText(text)
            self.win.frame.set_goto()

    def build_completer(self) -> None:
        if self._completer is not None:
            return
        names = [s.full for s in brightstars.STARS]
        cat = self.win._cat()
        if cat:
            names += [o.label for o in cat.objs]
        names.sort(key=str.lower)
        c = QCompleter(names, self.win)
        c.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        c.setFilterMode(Qt.MatchFlag.MatchContains)
        c.setMaxVisibleItems(8)
        self._completer = c
        self.win.skymap.search.setCompleter(c)
        self._style_completer()

    def _style_calendar(self) -> None:
        cal = self.dt_when.calendarWidget()
        if cal is not None:
            style_calendar(cal, self.win.pal)

    def _style_completer(self) -> None:
        if self._completer is None:
            return
        p = self.win.pal
        popup = self._completer.popup()
        if popup is None:
            return
        popup.setStyleSheet(
            f"background: {p.surface2}; color: {p.text}; "
            f"border: 1px solid {p.border}; selection-background-color: "
            f"{p.accent}; selection-color: {p.bg}; padding: 2px;"
        )
