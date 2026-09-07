"""The platform alignment procedure.

Like CONFIG it is a window and not a mode, and for one reason more: measuring a
station takes minutes during which the tube has to be pointed, which happens in
FRAME. A mode would have to be left — taking its own readout with it — halfway
through every station.

What is held here is that separation, and the two ends of the procedure that
are easy to break silently: the request that starts a measurement must not stop
the session, and a measurement must never be kept without the sidereal time
that makes it mean anything.
"""
from __future__ import annotations

import numpy as np
import pytest

from astrodoro.core import platform_align as pa


@pytest.fixture
def window(qapp, settings):
    from astrodoro.ui.main import MainWindow
    w = MainWindow(settings)
    w.show()
    yield w
    w.align_window.close()
    w.close()


def _status(ready=True, n=40, span=420.0, rate=0.0031, middle=1.7e9):
    return {"name": "M8", "ra": 271.0, "dec": -24.4, "n": n, "span_s": span,
            "progress": 1.0 if ready else 0.4, "middle": middle,
            "rate_deg_min": rate, "r2": 0.93, "ready": ready,
            "rejected": 0, "reason": ""}


def test_alignment_is_not_a_mode(window):
    from astrodoro.ui.main import MODES

    assert [m[0] for m in MODES] == ["frame", "stack", "lucky", "targets"]
    assert window.align_window.isAncestorOf(window.btn_align_measure)


def test_opening_it_leaves_the_session_alone(window):
    w = window
    w.set_mode("stack")
    view, panel = w._view, w.panels.currentIndex()
    w.open_align()
    assert w.align_window.isVisible()
    assert w._mode == "stack" and w._view == view
    assert w.panels.currentIndex() == panel


def test_measuring_needs_a_target_and_a_capture(window):
    """Without a target the rotation cannot be attributed to a direction, and
    the button must come back up rather than sit on with nothing running."""
    w = window
    w.open_align()
    w._target = None
    w.btn_align_measure.setChecked(True)
    assert not w.btn_align_measure.isChecked()


def test_the_worker_measures_without_stacking(settings):
    """A station costs no disk and no platform travel: it runs on the star
    lists the capture already produces, in a mode that cannot integrate."""
    from astrodoro.ui.worker import CaptureWorker, Config

    w = CaptureWorker(Config.from_settings(settings, mode="frame",
                                           record=False))
    assert w.align is None
    w.request(align={"ra": 271.0, "dec": -24.4, "name": "M8",
                     "min_span_s": 120.0})
    w._apply_pending()
    assert w.align is not None and w.align.min_span_s == 120.0
    assert not w.can_integrate
    assert w._stats_snapshot()["align"]["name"] == "M8"

    w.request(align=None)
    w._apply_pending()
    assert w.align is None and w._stats_snapshot()["align"] is None


def test_a_kept_station_carries_the_sidereal_time_of_its_middle(window):
    """The platform's error stands still against the ground, not against the
    stars: a station without the sidereal time of its own middle cannot be
    combined with another taken an hour later."""
    from astrodoro.core.tonight import lst_at

    w = window
    w.open_align()
    w._align_status = _status()
    w._align_keep()

    assert len(w._align_stations) == 1
    st = w._align_stations[0]
    assert st.lst_deg == pytest.approx(lst_at(w.sp_lon.value(),
                                              __import__("datetime").datetime
                                              .fromtimestamp(1.7e9, tz=__import__
                                                             ("datetime").UTC)),
                                       abs=1e-6)
    assert st.rate_deg_min == pytest.approx(0.0031)
    # Keeping ends the run: nothing should still be measuring the field that
    # was just filed away.
    assert not w.btn_align_measure.isChecked()


def test_measuring_the_same_field_again_checks_the_correction(window):
    """The second measurement of a field is not a second equation — it is the
    test of whether the correction went the right way."""
    w = window
    w.open_align()
    w._align_status = _status(rate=0.004)
    w._align_keep()
    w._align_status = _status(rate=0.009, middle=1.7e9 + 3600)
    w._align_keep()

    assert len(w._align_stations) == 1, "the same field replaces itself"
    assert "invert" in w._align_check.lower()


def test_inverting_the_parity_flips_the_correction_and_is_remembered(window,
                                                                     settings):
    w = window
    w.open_align()
    for ra, dec, rate in ((271.0, -24.4, 0.0031), (10.0, 5.0, -0.0021)):
        w._align_status = {**_status(rate=rate), "ra": ra, "dec": dec,
                           "name": f"{ra}"}
        w._align_keep()
    before = w._align_result
    assert before is not None

    w._align_flip_parity()
    assert settings.platform_parity == -1
    assert w._align_result.alt_error_deg == pytest.approx(
        -before.alt_error_deg, rel=0.02)


def test_the_live_readout_does_not_widen_the_window(window):
    """Same floor as everywhere else, measured the same way: a rejection reason
    is arbitrarily long and must not become the window's minimum width."""
    from PySide6.QtWidgets import QApplication

    def min_width():
        w.align_window.layout().activate()
        QApplication.instance().processEvents()
        return w.align_window.minimumSizeHint().width()

    w = window
    w.open_align()
    base = min_width()
    w._update_align({**_status(ready=False),
                     "reason": "astroalign: MaxIterError; voting: only 2 stars "
                               "voted together after removing duplicates"})
    assert min_width() == base, "the live readout widened the window"

    w._align_status = _status()
    w._align_keep()
    assert min_width() == base, "a kept station widened the window"


def test_the_suggested_field_is_the_one_still_missing(window):
    """With one station kept, what the window offers to point at has to be the
    direction that station says nothing about."""
    w = window
    w.open_align()
    w._align_status = _status()
    w._align_keep()
    assert w._align_result is not None and not w._align_result.confident

    from astrodoro.core.tonight import lst_at
    lst = lst_at(w.sp_lon.value())
    pick = pa.ideal_next(w._align_stations, w.sp_lat.value(), lst)
    if pick is None:
        pytest.skip("nothing above the horizon at this hour")
    kept = w._align_stations[0]
    a = np.array([np.cos(np.deg2rad(kept.ra - kept.lst_deg)),
                  np.sin(np.deg2rad(kept.ra - kept.lst_deg))])
    b = np.array([np.cos(np.deg2rad(pick[0] - lst)),
                  np.sin(np.deg2rad(pick[0] - lst))])
    assert abs(a @ b) < 0.8
