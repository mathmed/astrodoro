"""Walks the paths of the FRAME screen.

It does not check appearance — it checks that every gesture of the mode runs.
This is the test that was missing: a block of code accidentally duplicated
inside `_fov_deg` passed the import check and only showed up in the field, as an
`UnboundLocalError` when computing the target direction.

Runs with no display, no camera, no phone and no network: the sensor is fed
straight into `Pointing`, as the server would.
"""
from __future__ import annotations

import time

import pytest

from astrodoro.pointing import brightstars as bs


@pytest.fixture
def window(qapp, settings):
    from astrodoro.ui.main import MainWindow
    w = MainWindow(settings)
    yield w
    w.close()


def _feed(w, alt, az):
    """Half a second of still samples, like a tube at rest."""
    t0 = time.monotonic()
    for i in range(16):
        w.point.feed((360.0 - az) % 360.0, alt, 0.0, None, t0 - 0.8 + i * 0.05)


def test_framing_mode_paths(window, settings):
    w = window
    visible = bs.visible(settings.latitude, settings.longitude, min_alt=20.0)
    if not visible:
        pytest.skip("no alignment star above 20 degrees right now")
    star, alt, az = visible[0]

    _feed(w, alt, az)
    w._sensor_tick()
    w._set_view("map")
    _feed(w, alt, az)
    w.align_on(star)
    assert w.point.aligned, "did not align with the tube at rest"
    assert w.skymap.aligned_on == star.label

    # Searching a star aligns on it; searching an object makes it the target.
    w.search(star.label)
    assert w.point.aligned and w.skymap.aligned_on == star.label
    w.search("M8")
    assert w._target is not None and w._found is not None
    w.search("does not exist in the sky")
    assert w._found is None

    w.ed_goto.setText("M8")
    w.set_goto()
    assert w._target is not None
    w._update_goto()
    w._fov_deg()
    w._objects_in_field()
    w._update_map()

    w.clear_target()
    assert w._target is None
    w.reset_align()
    assert not w.point.aligned
    w._update_goto()
    w._update_map()
    w.ed_goto.setText("M8")
    w.set_goto()
    w._drag_sky(3.0)
    w.skymap.grab()


def test_zoom_serves_all_three_views(window):
    w = window
    w._set_view("map")
    fov = w.skymap.fov
    w.zoom_in()
    assert w.skymap.fov < fov
    w.zoom_out()
    w.fit_view()
    assert w.skymap.fov == 40.0
    w.zoom_one()
    assert w.skymap.fov < 2.0
    w.fit_view()


def test_health_strip_keeps_the_reason(window):
    w = window
    w.health.push(True, {"index": 1, "n_stars": 40, "hfr": 1.8,
                         "time": "21:00:00"})
    w.health.push(False, {"index": 2, "reason": "trailed stars (1.9)"})
    assert w.health.index_at(0) == 0
    assert "trailed" in w.health.tooltip(1)
    assert "accepted" in w.health.tooltip(0)


def test_night_theme_repaints_the_map(window):
    w = window
    w._theme = "night"
    w.apply_theme()
    w.skymap.grab()


def test_the_target_name_is_asked_when_integration_starts(window,
                                                          monkeypatch):
    w = window
    monkeypatch.setattr(w, "_ask_target_name",
                        lambda: (setattr(w, "_target_name", "M22"), True)[-1])
    w.btn_integrate.setChecked(True)
    assert w.btn_integrate.isChecked() and w._target_name == "M22"
    w.btn_integrate.setChecked(False)
    monkeypatch.setattr(w, "_ask_target_name", lambda: False)  # cancelled
    w.btn_integrate.setChecked(True)
    assert not w.btn_integrate.isChecked()


def test_no_panel_is_wider_than_its_column(window):
    """A panel wider than the column means a horizontal scrollbar, which in the
    dark hides half the controls without warning."""
    w = window
    for mode in ("targets", "stack", "lucky", "frame"):
        w.set_mode(mode)
        sa = w.panels.currentWidget()
        width = sa.widget().minimumSizeHint().width()
        assert width <= sa.viewport().width(), (
            f"the {mode} panel asks for {width}px in {sa.viewport().width()}px")


def test_output_folders_come_from_settings(window, settings, tmp_path):
    w = window
    target = tmp_path / "elsewhere"
    settings.export_dir = str(target)
    assert w._output_dir() == target
    assert target.is_dir(), "the export folder is created on demand"
