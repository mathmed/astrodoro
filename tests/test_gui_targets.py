"""Walks the paths of the TARGETS screen and of the loupe.

Like `test_gui_framing`, it does not check appearance — it checks that every
gesture of the mode runs: the list is built, the hour can be moved, a suggestion
becomes the target, and the loupe opens over the image and pins a star.

The catalogue is injected rather than loaded: OpenNGC is downloaded, not
versioned, and a test that skips when it is absent would cover nothing on a
fresh checkout.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPixmap

from astrodoro.core.catalog import Catalog, Obj
from astrodoro.core.lucky import BodyState


def _obj(name, kind="G", ra=0.0, dec=-6.7, major=12.0, minor=8.0, mag=8.0,
         messier="", common="") -> Obj:
    return Obj(name=name, kind=kind, ra=ra, dec=dec, major_arcmin=major,
               minor_arcmin=minor, mag=mag, messier=messier, common=common)


@pytest.fixture
def window(qapp, settings):
    """A window whose catalogue is a handful of objects spread around the sky,
    so something is above the horizon whatever hour the test runs at."""
    from astrodoro.ui.main import MainWindow
    w = MainWindow(settings)
    cat = Catalog(settings.catalog_path())
    cat.objs = [
        _obj("NGC1000", kind="G", ra=ra, dec=-6.7, messier=str(i + 1),
             common=f"Fake {i}")
        for i, ra in enumerate(range(0, 360, 30))
    ] + [
        _obj(f"NGC200{i}", kind="OCl", ra=ra, dec=-30.0, major=20.0)
        for i, ra in enumerate(range(0, 360, 45))
    ] + [
        _obj(f"NGC300{i}", kind="PN", ra=ra, dec=20.0, major=3.0, mag=10.0)
        for i, ra in enumerate(range(0, 360, 45))
    ]
    # Distinct names: the table keeps the selection by name.
    for i, o in enumerate(cat.objs):
        o.name = f"OBJ{i:03d}"
    w._catalog = cat
    yield w
    w.close()


def test_the_targets_mode_builds_a_list_and_answers_a_choice(window):
    w = window
    w.rail.select("targets")
    assert w._mode == "targets" and w._view == "targets"
    assert w.targets.rowCount() > 0, "nothing above the horizon in a full sky"
    assert w._sky is not None
    assert w.lbl_sky.text().strip()

    # Selecting a row fills the detail panel and enables the two actions.
    w.targets.selectRow(0)
    s = w.targets.current()
    assert s is not None
    assert s.obj.label in w.lbl_sug.text()
    assert w.lbl_sug_why.text()
    assert w.btn_use.isEnabled() and w.btn_see.isEnabled()

    # And the list is ordered: the first row is the best score.
    scores = [w.targets._rows[r].score for r in range(w.targets.rowCount())]
    assert scores == sorted(scores, reverse=True)


def test_a_suggestion_becomes_the_target_and_opens_framing(window):
    w = window
    w.rail.select("targets")
    w.targets.selectRow(0)
    chosen = w.targets.current().obj
    w._use_suggestion(w.targets.current())
    assert w._target is chosen
    assert w.btn_clear_target.isEnabled()
    assert w._mode == "frame", "the next gesture is pushing the tube"
    w._update_goto()

    w.clear_target()
    assert w._target is None


def test_the_hour_can_be_moved_and_stops_following_the_clock(window):
    w = window
    w.rail.select("targets")
    before = w._targets_when()
    w._shift_when(4.0)
    assert not w.btn_now.isChecked(), "editing the hour leaves 'now'"
    after = w._targets_when()
    assert 3.9 < (after - before).total_seconds() / 3600 < 4.1
    # The sky moved with it: same objects, different altitudes.
    assert w._sky is not None

    w.btn_now.setChecked(True)
    assert w.btn_now.isChecked() and not w.dt_when.isEnabled()
    assert abs((w._targets_when() - before).total_seconds()) < 120


def test_the_filters_narrow_the_list(window):
    w = window
    w.rail.select("targets")
    everything = w.targets.rowCount()

    w.cb_family.setCurrentIndex(w.cb_family.findData("planetary"))
    only_pn = w.targets.rowCount()
    assert only_pn < everything
    assert all(s.obj.kind == "PN" for s in w.targets._rows)

    w.cb_family.setCurrentIndex(w.cb_family.findData("all"))
    w.sp_min_alt.setValue(80.0)
    assert w.targets.rowCount() <= everything

    # A magnitude limit this tight leaves nothing in the catalogue — but the
    # planets are brighter than any of it, and the family is what excludes them.
    w.sp_min_alt.setValue(25.0)
    w.sp_max_mag.setValue(3.0)
    w.cb_family.setCurrentIndex(w.cb_family.findData("galaxy"))
    assert w.targets.rowCount() == 0
    assert w.targets.current() is None and not w.btn_use.isEnabled()


def test_an_empty_list_says_so_instead_of_going_blank(window):
    w = window
    w._catalog = Catalog(w.settings.catalog_path())     # no objects at all
    w.rail.select("targets")
    # Whatever is up right now, the family that holds no bodies leaves nothing.
    w.cb_family.setCurrentIndex(w.cb_family.findData("galaxy"))
    assert w.targets.rowCount() == 0
    assert not w.btn_use.isEnabled()
    assert w.lbl_sug.text()


def _planet(lst_deg, dec, key="jupiter", diameter_arcmin=0.75) -> BodyState:
    """A body on the meridian, so the window factor does not zero its score."""
    return BodyState(body=key, when=datetime.now(UTC), alt=80.0 - abs(dec),
                     az=0.0, ra=lst_deg, dec=dec, illum=1.0, waxing=False,
                     diameter_arcmin=diameter_arcmin, distance_km=6e8)


def test_a_planet_is_in_the_list_and_is_pointed_at_by_its_ephemeris(
        window, monkeypatch):
    """The Moon and the planets rank with the catalogue, and choosing one goes
    through the body path: their coordinates are only true for the minute they
    were computed in."""
    from astrodoro.ui import main as ui_main

    w = window
    w.rail.select("targets")               # a first pass, for the sidereal time
    lst = w._sky.lst_deg
    monkeypatch.setattr(ui_main.lucky, "bodies_at",
                        lambda *a, **k: [_planet(lst, w.sp_lat.value())])
    w._bodies_key = None
    w._refresh_targets()

    row = next((s for s in w.targets._rows if s.body == "jupiter"), None)
    assert row is not None, "the planet did not reach the list"
    assert row.obj.kind == "Planet"

    w.targets.selectRow(w.targets._rows.index(row))
    # No survey picture of something that moves: the panel says so.
    assert w.preview._pix is None and w.preview._note

    w._use_suggestion(row)
    assert w.cb_body.currentData() == "jupiter"
    assert w._body_target, "pointing at it did not start following it"
    assert w._mode == "frame"


def test_the_loupe_opens_over_the_image_and_pins_a_star(window):
    w = window
    w.set_mode("frame")
    w._q = np.zeros((120, 160), dtype=np.uint16)

    w.btn_loupe.setChecked(True)
    assert w._loupe_on
    w.loupe.place()
    assert w.loupe.geometry().right() <= w.view.width()

    # A click on the image pins the star under the cursor; "auto" lets it go.
    scene_pos = w.vb.mapViewToScene(QPointF(40.0, 50.0))
    w._image_clicked(_FakeClick(scene_pos))
    assert w._loupe_xy is not None
    w._pin_loupe(None)
    assert w._loupe_xy is None

    # A click outside the frame is not a star.
    w._image_clicked(_FakeClick(w.vb.mapViewToScene(QPointF(-40.0, -50.0))))
    assert w._loupe_xy is None

    w.btn_loupe.setChecked(False)
    assert not w._loupe_on


def test_the_loupe_hides_itself_over_the_map_and_the_list(window):
    """It magnifies the last frame: over a chart there is nothing to magnify."""
    w = window
    w.set_mode("frame")
    w.btn_loupe.setChecked(True)
    w._set_view("map")
    assert not w._loupe_on
    w._set_view("live")
    assert w._loupe_on
    w.rail.select("targets")
    assert not w._loupe_on


def test_the_focus_readout_survives_without_the_focus_mode(window):
    """The HFR used to live on a screen that no longer exists; it has to keep
    reaching the vitals bar, and the loupe when it is open."""
    w = window
    w.btn_loupe.setChecked(True)
    crop = np.linspace(0, 1, 64 * 64, dtype=np.float32).reshape(64, 64)
    w.on_focus(crop, {"hfr": 3.21, "best": 2.90, "ratio": 1.11, "trend": -0.4,
                      "verdict": "improving", "series": (np.empty(0),
                                                         np.empty(0))})
    assert "3.21" in w.st_hfr.val.text()
    assert "3.21" in w.loupe.lbl_hfr.text()

    # No stars in the frame: every readout has to fall back, never blank out.
    w.on_focus(None, {"hfr": float("nan"), "best": float("nan"),
                      "ratio": float("nan"), "trend": float("nan"),
                      "verdict": "not enough stars",
                      "series": (np.empty(0), np.empty(0))})
    assert w.st_hfr.val.text() == "—"


class _FakeClick:
    """The one thing `_image_clicked` reads out of a pyqtgraph click event."""

    def __init__(self, scene_pos):
        self._p = scene_pos

    def scenePos(self):
        return self._p


def test_the_alignment_star_is_suggested_and_follows_the_target(window):
    """The card that answers "which star do I align on" — and the answer has to
    change when the target does, since the correction is local to the star."""
    w = window
    w.show()                       # the suggestion waits for a visible window
    w.set_mode("frame")
    w._refresh_align_pick(force=True)
    if not w._align_picks:
        pytest.skip("no alignment star above the horizon right now")

    first = w._align_pick()
    assert first.star.full in w.lbl_align_pick.text()
    assert w.btn_align_pick.isEnabled()

    # "another one" cycles without falling off the end of the list.
    for _ in range(len(w._align_picks) + 2):
        w._align_next_pick()
    assert w._align_pick() is not None
    assert w._align_pick().star.full in w.lbl_align_pick.text()

    # Naming a target re-answers the question and states the distance.
    w.rail.select("targets")
    w.targets.selectRow(0)
    w._use_suggestion(w.targets.current())
    assert w._align_picks, "the suggestion was lost when the target was set"
    assert np.isfinite(w._align_pick().target_sep)
    assert "°" in w.lbl_align_pick.text()

    # Showing it on the map is the other way to find it in the sky.
    w._align_pick_on_map()
    assert w._found is w._align_pick().star and w._view == "map"


def test_aligning_on_the_suggestion_goes_through_the_usual_guard(window):
    """No sensor connected: it must refuse, not align on a reading it does not
    have."""
    w = window
    w.show()
    w._refresh_align_pick(force=True)
    if not w._align_picks:
        pytest.skip("no alignment star above the horizon right now")
    w._align_on_pick()
    assert not w.point.aligned


def test_the_preview_appears_for_the_selected_object(window, tmp_path,
                                                     monkeypatch):
    """The picture panel, without touching the network: a cached file is what
    the field always has anyway."""
    from astrodoro.core import previews as core_previews

    jpg = tmp_path / "fake.jpg"
    QPixmap(64, 64).save(str(jpg), "JPG")
    monkeypatch.setattr(core_previews, "cached", lambda *a, **k: jpg)

    w = window
    w.rail.select("targets")
    w.targets.selectRow(0)
    assert w.preview._pix is not None, "a cached picture was not shown"

    # Unticking stops it, and the panel says so instead of going blank.
    w.chk_previews.setChecked(False)
    assert w.preview._pix is None and w.preview._note
    assert not w.btn_prefetch.isEnabled()
    w.chk_previews.setChecked(True)
    assert w.preview._pix is not None


def test_a_late_download_for_another_object_is_ignored(window, tmp_path):
    """The list moves while a fetch is in flight; the picture that arrives has
    to be the one being looked at, not the one that was."""
    w = window
    w.rail.select("targets")
    w.targets.selectRow(0)
    current = w.targets.current().obj.name

    jpg = tmp_path / "late.jpg"
    QPixmap(64, 64).save(str(jpg), "JPG")
    w.preview.clear("waiting")
    w._preview_ready("SOMETHING/ELSE", str(jpg))
    assert w.preview._pix is None, "showed a picture of another object"

    w._preview_want = current
    w._preview_ready(current, str(jpg))
    assert w.preview._pix is not None


def test_the_frame_rectangle_is_dropped_when_it_would_not_fit(window, tmp_path):
    """A rectangle bigger than the picture is four lines outside it."""
    from astrodoro.core import previews as core_previews

    w = window
    w.rail.select("targets")
    jpg = tmp_path / "tiny.jpg"
    QPixmap(64, 64).save(str(jpg), "JPG")

    small = _obj("NGC0001", major=1.0, minor=1.0)     # 10' cutout, 51' frame
    w._show_preview(small, str(jpg))
    assert w.preview._frame is None

    big = _obj("NGC0002", major=45.0, minor=30.0)     # 100' cutout
    w._show_preview(big, str(jpg))
    assert w.preview._frame is not None
    assert max(w.preview._frame) < 1.0
    assert core_previews.cutout_fov(45.0, w._fov_arcmin()) > max(w._fov_arcmin())
