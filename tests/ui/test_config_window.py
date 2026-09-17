from __future__ import annotations

import pytest


@pytest.fixture
def window(make_window):
    return make_window(show=True)


def test_config_is_not_a_mode(window):
    from astrodoro.ui.main import MODES

    keys = [m[0] for m in MODES]
    assert "config" not in keys, "CONFIG is a window, not a step"
    assert keys == ["frame", "targets", "stack", "lucky"]
    assert set(window.rail.buttons) == set(keys)
    assert set(window._panel_index) == set(keys)
    assert set(window._ctx_index) == set(keys)


def test_opening_config_leaves_the_session_alone(window):
    w = window
    w.set_mode("stack")
    view, panel = w._view, w.panels.currentIndex()

    w.config_window.open_it()
    assert w.config_window.isVisible()
    assert w._mode == "stack"
    assert w._view == view
    assert w.panels.currentIndex() == panel

    w.config_window.close()
    assert w._mode == "stack"


def test_the_worker_only_stacks_in_stack_mode(settings):
    from astrodoro.ui.worker import CaptureWorker, Config

    w = CaptureWorker(Config.from_settings(settings, mode="stack", record=False))
    assert w.can_integrate
    for mode in ("frame", "targets", "lucky", "config"):
        w.cfg.mode = mode
        assert not w.can_integrate, f"{mode} must not stack"


def test_night_controls_stay_on_the_top_bar(window):
    w = window
    for b in (w.btn_night, w.btn_full, w.btn_log, w.btn_config):
        assert b.isVisible()
        assert not w.config_window.isAncestorOf(b)
    w.btn_full.setChecked(True)
    assert w.btn_full.isVisible() and not w._left_col.isVisible()
    w.btn_full.setChecked(False)


def test_config_window_holds_what_is_set_once(window, settings):
    w = window
    cfg = w.config_window
    for field in (
        cfg.sp_lat,
        cfg.sp_lon,
        cfg.sp_elev,
        cfg.sp_focal,
        cfg.sp_pixel,
        cfg.cb_lang,
        cfg.sl_night,
        cfg.chk_touch,
    ):
        assert cfg.isAncestorOf(field)

    w.config_window.open_it()
    cfg.sp_lat.setValue(-23.5505)
    cfg.sp_focal.setValue(1500)
    w.config_window.close()
    assert settings.latitude == pytest.approx(-23.5505)
    assert settings.focal_length_mm == pytest.approx(1500)


def test_without_a_site_the_list_says_so_instead_of_ranking(window, settings):
    settings.site_set = False
    w = window
    w.rail.select("targets")
    assert w.targets.rowCount() == 0
    assert "site" in w.targets_panel.lbl_sky.text().lower()
    assert w.config_window.lbl_site_missing is not None


def test_editing_the_site_is_what_marks_it_as_supplied(window, settings):
    settings.site_set = False
    cfg = window.config_window
    cfg.sp_lat.setValue(-23.5505)
    assert settings.has_site()
    assert not cfg.lbl_site_missing.isVisible()
