from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ...i18n import available as available_languages
from ...i18n import gettext as _
from ..design import T_BODY, T_MONO, Card, shorten


class ConfigWindow(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.settings = win.settings

        self.setObjectName("root")
        self.setWindowTitle(_("Astrodoro — configuration"))
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)
        v.addWidget(self._card_folders())
        v.addWidget(self._card_site())
        v.addWidget(self._card_display())
        v.addStretch(1)
        close = QPushButton(_("close"))
        close.setAutoDefault(False)
        close.clicked.connect(self.close)
        v.addWidget(close)
        self.setMinimumWidth(408)
        self.finished.connect(self.store)

    def open_it(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _card_folders(self) -> Card:
        c = Card(_("folders"))
        self._dir_labels = {}
        for key, label, hint in (
            (
                "capture_dir",
                _("sessions"),
                _("root of the recorded sessions: subs, stacks and previews"),
            ),
            ("bias_dir", _("bias"), _("where master bias frames are written")),
            ("dark_dir", _("darks"), _("where master darks are written")),
            ("flat_dir", _("flats"), _("where master flats are written")),
            ("export_dir", _("exports"), _("images saved outside a recording session")),
        ):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            name = QLabel(label)
            name.setFont(T_BODY())
            name.setMinimumWidth(72)
            name.setToolTip(hint)
            value = QLabel(shorten(getattr(self.settings, key)))
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
        self.win._ic(b, "folder")
        b.clicked.connect(
            lambda: self._reveal(self.settings.path("capture_dir", create=True))
        )
        c.add(b)
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
        for sp in (self.sp_lat, self.sp_lon, self.sp_elev):
            sp.valueChanged.connect(self._site_changed)
        c.field(
            _("latitude"),
            self.sp_lat,
            _(
                "latitude feeds straight into the altitude of the pole: 0.1° "
                "of error becomes 6' of alignment error"
            ),
        )
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
        self.lbl_site_missing = QLabel(
            _(
                "Not set yet. The target list and the platform alignment need it: "
                "an error in latitude becomes the same error in the correction."
            )
        )
        self.lbl_site_missing.setWordWrap(True)
        self.lbl_site_missing.setFont(T_BODY())
        self.lbl_site_missing.setVisible(not self.settings.has_site())
        c.add(self.lbl_site_missing)
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

        self.sl_night, wn = self.win._slider(_("night brightness"), 0, 2, 1, 1.0)
        self.sl_night.valueChanged.connect(self.win._night_changed)
        c.add(wn)
        self.chk_touch = QCheckBox(_("large click targets"))
        self.chk_touch.setToolTip(
            _(
                "in the dark, cold and without reading "
                "glasses, a small target is expensive"
            )
        )
        self.chk_touch.toggled.connect(self.win._touch_changed)
        c.add(self.chk_touch)
        return c

    def _site_changed(self) -> None:
        s = self.settings
        s.latitude = self.sp_lat.value()
        s.longitude = self.sp_lon.value()
        s.elevation_m = self.sp_elev.value()
        s.site_set = True
        self.lbl_site_missing.setVisible(False)
        p = self.win.point
        p.lat, p.lon, p.elevation_m = s.latitude, s.longitude, s.elevation_m

    def _optics_changed(self) -> None:
        self.settings.focal_length_mm = self.sp_focal.value()
        self.settings.pixel_size_um = self.sp_pixel.value()
        self.update_scale_label()

    def update_scale_label(self) -> None:
        s = self.win.pixel_scale()
        w, h = self.win._frame_size()
        self.lbl_scale.setText(
            _("{scale:.3f}\"/px · field {w:.0f}' x {h:.0f}'").format(
                scale=s, w=s * w / 60.0, h=s * h / 60.0
            )
        )

    def _language_changed(self, index: int) -> None:
        tag = self.cb_lang.itemData(index)
        if not tag or tag == self.settings.language:
            return
        self.settings.language = tag
        self.settings.save()
        if self.win.worker is not None:
            self.win.on_log(_("language will change when the session ends"))
            return
        self.win.language_changed.emit(tag)

    def pick_dir(self, key: str) -> None:
        current = str(self.settings.path(key))
        d = QFileDialog.getExistingDirectory(self, _("choose a folder"), current)
        if not d:
            return
        setattr(self.settings, key, d)
        self.settings.save()
        lab = self._dir_labels.get(key)
        if lab is not None:
            lab.setText(shorten(d))
            lab.setToolTip(d)
        self.win.on_log(_("{name} -> {path}").format(name=key, path=d))

    def _reveal(self, path: Path) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def store(self, _result: int = 0, save: bool = True) -> None:
        s = self.settings
        if s.site_set:
            s.latitude = self.sp_lat.value()
            s.longitude = self.sp_lon.value()
            s.elevation_m = self.sp_elev.value()
        s.focal_length_mm = self.sp_focal.value()
        s.pixel_size_um = self.sp_pixel.value()
        if save:
            try:
                s.save()
            except OSError:
                pass
