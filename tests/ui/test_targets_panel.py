from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from PySide6.QtCore import QDateTime, QLocale, QPointF
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QCalendarWidget

from astrodoro.core.catalog import Catalog, Obj
from astrodoro.core.lucky import BodyState
from astrodoro.ui.design import DAY_NAMES


def _obj(
    name,
    kind="G",
    ra=0.0,
    dec=-6.7,
    major=12.0,
    minor=8.0,
    mag=8.0,
    messier="",
    common="",
) -> Obj:
    return Obj(
        name=name,
        kind=kind,
        ra=ra,
        dec=dec,
        major_arcmin=major,
        minor_arcmin=minor,
        mag=mag,
        messier=messier,
        common=common,
    )


@pytest.fixture
def window(make_window, settings):
    w = make_window()
    cat = Catalog(settings.catalog_path())
    cat.objs = (
        [
            _obj(
                "NGC1000",
                kind="G",
                ra=ra,
                dec=-6.7,
                messier=str(i + 1),
                common=f"Fake {i}",
            )
            for i, ra in enumerate(range(0, 360, 30))
        ]
        + [
            _obj(f"NGC200{i}", kind="OCl", ra=ra, dec=-30.0, major=20.0)
            for i, ra in enumerate(range(0, 360, 45))
        ]
        + [
            _obj(f"NGC300{i}", kind="PN", ra=ra, dec=20.0, major=3.0, mag=10.0)
            for i, ra in enumerate(range(0, 360, 45))
        ]
    )
    for i, o in enumerate(cat.objs):
        o.name = f"OBJ{i:03d}"
    w._catalog = cat
    return w


def _select_catalogue_row(w) -> int:
    for r in range(w.targets.rowCount()):
        if w.targets._rows[r].obj.kind not in ("Moon", "Planet"):
            w.targets.selectRow(r)
            return r
    pytest.skip("only the Moon and the planets are up right now")


def test_the_targets_mode_builds_a_list_and_answers_a_choice(window):
    w = window
    w.rail.select("targets")
    assert w._mode == "targets" and w._view == "targets"
    assert w.targets.rowCount() > 0, "nothing above the horizon in a full sky"
    assert w.targets_panel._sky is not None
    assert w.targets_panel.lbl_sky.text().strip()

    w.targets.selectRow(0)
    s = w.targets.current()
    assert s is not None
    assert s.obj.label in w.targets_panel.lbl_sug.text()
    assert w.targets_panel.lbl_sug_why.text()
    assert w.targets_panel.btn_use.isEnabled() and w.targets_panel.btn_see.isEnabled()

    scores = [w.targets._rows[r].score for r in range(w.targets.rowCount())]
    assert scores == sorted(scores, reverse=True)


def test_a_suggestion_becomes_the_target_and_opens_framing(window):
    w = window
    w.rail.select("targets")
    _select_catalogue_row(w)
    chosen = w.targets.current().obj
    w.targets_panel.use_suggestion(w.targets.current())
    assert w._target is chosen
    assert w.targets_panel.btn_clear_target.isEnabled()
    assert w._mode == "frame", "the next gesture is pushing the tube"
    w.frame.update_goto()

    w.clear_target()
    assert w._target is None


def test_the_hour_can_be_moved_and_stops_following_the_clock(window):
    w = window
    w.rail.select("targets")
    before = w.targets_panel.when()
    w.targets_panel._shift_when(4.0)
    assert not w.targets_panel.btn_now.isChecked(), "editing the hour leaves 'now'"
    after = w.targets_panel.when()
    assert 3.9 < (after - before).total_seconds() / 3600 < 4.1
    assert w.targets_panel._sky is not None

    w.targets_panel.btn_now.setChecked(True)
    assert (
        w.targets_panel.btn_now.isChecked() and not w.targets_panel.dt_when.isEnabled()
    )
    assert abs((w.targets_panel.when() - before).total_seconds()) < 120


def test_the_field_keeps_up_with_the_clock_while_following_it(window):
    w = window
    w.rail.select("targets")
    p = w.targets_panel
    assert p.btn_now.isChecked()

    p._when_guard = True
    p.dt_when.setDateTime(QDateTime.currentDateTime().addSecs(-7200))
    p._when_guard = False
    p.refresh()

    shown = p.dt_when.dateTime().toPython()
    assert abs((shown - datetime.now()).total_seconds()) < 120, (
        "with 'now' ticked the field must show the hour the ranking used"
    )
    assert p.btn_now.isChecked(), "following the clock is not an edit"


def test_the_calendar_is_legible_in_the_dark(window):
    w = window
    w.rail.select("targets")
    cal = w.targets_panel.dt_when.calendarWidget()

    assert (
        cal.horizontalHeaderFormat()
        is QCalendarWidget.HorizontalHeaderFormat.ShortDayNames
    )
    fm = cal.fontMetrics()
    short = QLocale.FormatType.ShortFormat
    widest = max(
        fm.horizontalAdvance(cal.locale().dayName(d.value, short)) for d in DAY_NAMES
    )
    assert cal.minimumWidth() >= widest * 7, "the day names must not be elided"

    theme = QColor(w.pal.text)
    for d in DAY_NAMES:
        assert cal.weekdayTextFormat(d).foreground().color() == theme, (
            "Qt paints the weekend red, which the palette does not own"
        )
    assert cal.headerTextFormat().foreground().color() == QColor(w.pal.text_dim)


def test_the_hour_field_shows_the_year_the_calendar_can_change(window):
    w = window
    w.rail.select("targets")
    assert "yy" in w.targets_panel.dt_when.displayFormat()


def test_the_filters_narrow_the_list(window):
    w = window
    w.rail.select("targets")
    everything = w.targets.rowCount()

    w.targets_panel.cb_family.setCurrentIndex(
        w.targets_panel.cb_family.findData("planetary")
    )
    only_pn = w.targets.rowCount()
    assert only_pn < everything
    assert all(s.obj.kind == "PN" for s in w.targets._rows)

    w.targets_panel.cb_family.setCurrentIndex(w.targets_panel.cb_family.findData("all"))
    w.targets_panel.sp_min_alt.setValue(80.0)
    assert w.targets.rowCount() <= everything

    w.targets_panel.sp_min_alt.setValue(25.0)
    w.targets_panel.sp_max_mag.setValue(3.0)
    w.targets_panel.cb_family.setCurrentIndex(
        w.targets_panel.cb_family.findData("galaxy")
    )
    assert w.targets.rowCount() == 0
    assert w.targets.current() is None and not w.targets_panel.btn_use.isEnabled()


def test_an_empty_list_says_so_instead_of_going_blank(window):
    w = window
    w._catalog = Catalog(w.settings.catalog_path())
    w.rail.select("targets")
    w.targets_panel.cb_family.setCurrentIndex(
        w.targets_panel.cb_family.findData("galaxy")
    )
    assert w.targets.rowCount() == 0
    assert not w.targets_panel.btn_use.isEnabled()
    assert w.targets_panel.lbl_sug.text()


def _planet(lst_deg, dec, key="jupiter", diameter_arcmin=0.75) -> BodyState:
    return BodyState(
        body=key,
        when=datetime.now(UTC),
        alt=80.0 - abs(dec),
        az=0.0,
        ra=lst_deg,
        dec=dec,
        illum=1.0,
        waxing=False,
        diameter_arcmin=diameter_arcmin,
        distance_km=6e8,
    )


def test_a_planet_is_in_the_list_and_is_pointed_at_by_its_ephemeris(
    window, monkeypatch
):
    from astrodoro.ui.panels import targets as targets_panel

    w = window
    w.rail.select("targets")
    lst = w.targets_panel._sky.lst_deg
    monkeypatch.setattr(
        targets_panel.lucky,
        "bodies_at",
        lambda *a, **k: [_planet(lst, w.settings.latitude)],
    )
    w.targets_panel._bodies_key = None
    w.targets_panel.refresh()

    row = next((s for s in w.targets._rows if s.body == "jupiter"), None)
    assert row is not None, "the planet did not reach the list"
    assert row.obj.kind == "Planet"

    w.targets.selectRow(w.targets._rows.index(row))
    assert w.targets_panel.preview._pix is None and w.targets_panel.preview._note

    w.targets_panel.use_suggestion(row)
    assert w.lucky.cb_body.currentData() == "jupiter"
    assert w.lucky.body_target, "pointing at it did not start following it"
    assert w._mode == "frame"


def test_the_loupe_opens_over_the_image_and_pins_a_star(window):
    w = window
    w.set_mode("frame")
    w.image._q = np.zeros((120, 160), dtype=np.uint16)

    w.image.btn_loupe.setChecked(True)
    assert w.image.loupe_on
    # A size of its own: the window is never shown here, so the layout gives
    # the view the default 100 px and nothing would fit inside it.
    w.image.view.resize(640, 480)
    w.image.loupe.place()
    assert w.image.loupe.geometry().right() <= w.image.view.width()

    scene_pos = w.image.vb.mapViewToScene(QPointF(40.0, 50.0))
    w.image._image_clicked(_FakeClick(scene_pos))
    assert w.image._loupe_xy is not None
    w.image.pin_loupe(None)
    assert w.image._loupe_xy is None

    w.image._image_clicked(_FakeClick(w.image.vb.mapViewToScene(QPointF(-40.0, -50.0))))
    assert w.image._loupe_xy is None

    w.image.btn_loupe.setChecked(False)
    assert not w.image.loupe_on


def test_the_loupe_hides_itself_over_the_map_and_the_list(window):
    w = window
    w.set_mode("frame")
    w.image.btn_loupe.setChecked(True)
    w._set_view("map")
    assert not w.image.loupe_on
    w._set_view("live")
    assert w.image.loupe_on
    w.rail.select("targets")
    assert not w.image.loupe_on


def test_the_focus_readout_survives_without_the_focus_mode(window):
    w = window
    w.image.btn_loupe.setChecked(True)
    crop = np.linspace(0, 1, 64 * 64, dtype=np.float32).reshape(64, 64)
    w.on_focus(
        crop,
        {
            "hfr": 3.21,
            "best": 2.90,
            "ratio": 1.11,
            "trend": -0.4,
            "verdict": "improving",
            "series": (np.empty(0), np.empty(0)),
        },
    )
    assert "3.21" in w.st_hfr.val.text()
    assert "3.21" in w.image.loupe.lbl_hfr.text()

    w.on_focus(
        None,
        {
            "hfr": float("nan"),
            "best": float("nan"),
            "ratio": float("nan"),
            "trend": float("nan"),
            "verdict": "not enough stars",
            "series": (np.empty(0), np.empty(0)),
        },
    )
    assert w.st_hfr.val.text() == "—"


class _FakeClick:
    def __init__(self, scene_pos):
        self._p = scene_pos

    def scenePos(self):
        return self._p


def test_the_alignment_star_is_suggested_and_follows_the_target(window):
    w = window
    w.show()
    w.set_mode("frame")
    w.frame.refresh_align_pick(force=True)
    if not w.frame._align_picks:
        pytest.skip("no alignment star above the horizon right now")

    first = w.frame._align_pick()
    assert first.star.full in w.frame.lbl_align_pick.text()
    assert w.frame.btn_align_pick.isEnabled()

    for _ in range(len(w.frame._align_picks) + 2):
        w.frame._align_next_pick()
    assert w.frame._align_pick() is not None
    assert w.frame._align_pick().star.full in w.frame.lbl_align_pick.text()

    w.rail.select("targets")
    w.targets.selectRow(0)
    w.targets_panel.use_suggestion(w.targets.current())
    assert w.frame._align_picks, "the suggestion was lost when the target was set"
    assert np.isfinite(w.frame._align_pick().target_sep)
    assert "°" in w.frame.lbl_align_pick.text()

    w.frame._align_pick_on_map()
    assert w._found is w.frame._align_pick().star and w._view == "map"


def test_aligning_on_the_suggestion_goes_through_the_usual_guard(window):
    w = window
    w.show()
    w.frame.refresh_align_pick(force=True)
    if not w.frame._align_picks:
        pytest.skip("no alignment star above the horizon right now")
    w.frame._align_on_pick()
    assert not w.point.aligned


def test_the_preview_appears_for_the_selected_object(window, tmp_path, monkeypatch):
    from astrodoro.core import previews as core_previews

    jpg = tmp_path / "fake.jpg"
    QPixmap(64, 64).save(str(jpg), "JPG")
    monkeypatch.setattr(core_previews, "cached", lambda *a, **k: jpg)

    w = window
    w.rail.select("targets")
    _select_catalogue_row(w)
    assert w.targets_panel.preview._pix is not None, "a cached picture was not shown"

    w.targets_panel.chk_previews.setChecked(False)
    assert w.targets_panel.preview._pix is None and w.targets_panel.preview._note
    assert not w.targets_panel.btn_prefetch.isEnabled()
    w.targets_panel.chk_previews.setChecked(True)
    assert w.targets_panel.preview._pix is not None


def test_a_late_download_for_another_object_is_ignored(window, tmp_path):
    w = window
    w.rail.select("targets")
    w.targets.selectRow(0)
    current = w.targets.current().obj.name

    jpg = tmp_path / "late.jpg"
    QPixmap(64, 64).save(str(jpg), "JPG")
    w.targets_panel.preview.clear("waiting")
    w.targets_panel._preview_ready("SOMETHING/ELSE", str(jpg))
    assert w.targets_panel.preview._pix is None, "showed a picture of another object"

    w.targets_panel._preview_want = current
    w.targets_panel._preview_ready(current, str(jpg))
    assert w.targets_panel.preview._pix is not None


def test_the_frame_rectangle_is_dropped_when_it_would_not_fit(window, tmp_path):
    from astrodoro.core import previews as core_previews

    w = window
    w.rail.select("targets")
    jpg = tmp_path / "tiny.jpg"
    QPixmap(64, 64).save(str(jpg), "JPG")

    small = _obj("NGC0001", major=1.0, minor=1.0)
    w.targets_panel._show_preview(small, str(jpg))
    assert w.targets_panel.preview._frame is None

    big = _obj("NGC0002", major=45.0, minor=30.0)
    w.targets_panel._show_preview(big, str(jpg))
    assert w.targets_panel.preview._frame is not None
    assert max(w.targets_panel.preview._frame) < 1.0
    assert core_previews.cutout_fov(45.0, w._fov_arcmin()) > max(w._fov_arcmin())
