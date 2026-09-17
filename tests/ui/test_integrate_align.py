from __future__ import annotations

import numpy as np
import pytest

from astrodoro.core import platform_align as pa


def _status(**kw) -> dict:
    live = pa.LiveAlign()
    for i in range(40):
        live.add_rotation(0.0044 * i * 5.0 / 60.0, float(i * 8))
    st = live.status()
    st.update(kw)
    return st


def test_aligning_needs_a_session(window):
    w = window
    assert w.worker is None

    w.integrate.btn_align.setChecked(True)

    assert not w.integrate.btn_align.isChecked()
    assert "start the capture" in w.log.toPlainText()


def test_the_reading_carries_its_own_uncertainty(window):
    w = window
    w.integrate.update_align(_status())

    text = w.integrate.lbl_align.text()
    assert "±" in text, "a reading without its uncertainty invites over-trusting it"
    assert w.integrate.lbl_align_step.text()


def test_nothing_is_shown_before_the_first_baseline(window):
    w = window
    live = pa.LiveAlign()
    for i in range(4):
        live.add_rotation(0.001 * i, float(i * 5))

    w.integrate.update_align(live.status())

    assert w.integrate.lbl_align.text() == "—"
    assert "measuring" in w.integrate.lbl_align_step.text()


def test_a_big_error_and_a_small_one_do_not_look_alike(window):
    w = window
    w.integrate.update_align(_status(error_deg=2.0, error_sigma_deg=0.1))
    big = w.integrate.lbl_align.styleSheet()
    w.integrate.update_align(_status(error_deg=0.05, error_sigma_deg=0.02))
    small = w.integrate.lbl_align.styleSheet()

    assert w.pal.warn in big and w.pal.ok in small


def test_stopping_clears_the_readout(window):
    w = window
    w.integrate.update_align(_status())
    w.integrate.update_align(None)

    assert w.integrate.lbl_align.text() == "—"
    assert w.integrate.lbl_align_verdict.text() == ""


def test_the_error_is_the_rate_over_the_sidereal_rate():
    one_degree = pa.SIDEREAL_DEG_PER_MIN * np.deg2rad(1.0)
    assert pa.error_deg(one_degree) == pytest.approx(1.0)
    assert pa.error_deg(-one_degree) == pa.error_deg(one_degree)


def test_ending_the_session_releases_the_button(window):
    w = window
    w.integrate.btn_align.blockSignals(True)
    w.integrate.btn_align.setChecked(True)
    w.integrate.btn_align.blockSignals(False)
    w.integrate.update_align(_status())

    w.on_finished()

    assert not w.integrate.btn_align.isChecked()
    assert w.integrate.lbl_align.text() == "—"
