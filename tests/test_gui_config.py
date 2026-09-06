"""CONFIG is a window, not a step of the night.

The rail is the sequence of a night — find, choose, integrate, the Moon — and a
settings screen was never part of it: as step 5 it took a slot in that sequence
and, being a mode, it replaced the panel of whatever you were doing to show
folder paths. It now opens over the image from the top bar, which leaves the
mode, the view and the running session untouched.

What is asserted here is that separation: the rail no longer offers it, opening
it changes nothing about the session, and the controls that *are* touched during
a night (night mode, image only, the log) stayed out of it, on the top bar.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def window(qapp, settings):
    from astrodoro.ui.main import MainWindow
    w = MainWindow(settings)
    w.show()
    yield w
    w.config_window.close()
    w.close()


def test_config_is_not_a_mode(window):
    from astrodoro.ui.main import MODES
    keys = [m[0] for m in MODES]
    assert "config" not in keys, "CONFIG is a window, not a step"
    assert keys == ["frame", "targets", "stack", "lucky"]
    assert set(window.rail.buttons) == set(keys)
    assert set(window._panel_index) == set(keys)
    assert set(window._ctx_index) == set(keys)


def test_opening_config_leaves_the_session_alone(window):
    """The point of taking it out of the rail: what the mode decides — which
    panel, which view, and whether the worker may stack — must not move because
    somebody opened the folder paths."""
    w = window
    w.set_mode("stack")
    view, panel = w._view, w.panels.currentIndex()

    w.open_config()
    assert w.config_window.isVisible()
    assert w._mode == "stack"
    assert w._view == view
    assert w.panels.currentIndex() == panel

    w.config_window.close()
    assert w._mode == "stack"


def test_the_worker_only_stacks_in_stack_mode(settings):
    """`can_integrate` used to accept "config" too, because CONFIG was a mode
    you passed through mid-integration. There is no such mode any more."""
    from astrodoro.ui.worker import CaptureWorker, Config

    w = CaptureWorker(Config.from_settings(settings, mode="stack", record=False))
    assert w.can_integrate
    for mode in ("frame", "targets", "lucky", "config"):
        w.cfg.mode = mode
        assert not w.can_integrate, f"{mode} must not stack"


def test_night_controls_stay_on_the_top_bar(window):
    """They are touched in the dark, mid-session: behind a window that has to be
    opened and closed they would be worse than where they were."""
    w = window
    for b in (w.btn_night, w.btn_full, w.btn_log, w.btn_config):
        assert b.isVisible()
        assert not w.config_window.isAncestorOf(b)
    # "image only" hides the panels, but not the bar holding its own toggle.
    w.btn_full.setChecked(True)
    assert w.btn_full.isVisible() and not w._left_col.isVisible()
    w.btn_full.setChecked(False)


def test_config_window_holds_what_is_set_once(window, settings):
    """And writes it when it closes: "applies to the next session" is only true
    if closing the window is enough to persist it."""
    w = window
    for field in (w.sp_lat, w.sp_lon, w.sp_elev, w.sp_focal, w.sp_pixel,
                  w.cb_lang, w.sl_night, w.chk_touch):
        assert w.config_window.isAncestorOf(field)

    w.open_config()
    w.sp_lat.setValue(-23.5505)
    w.sp_focal.setValue(1500)
    w.config_window.close()
    assert settings.latitude == pytest.approx(-23.5505)
    assert settings.focal_length_mm == pytest.approx(1500)
