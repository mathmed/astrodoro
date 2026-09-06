"""The calibration panel: three kinds, and a label that says what is in force.

Picking a file is not the same as it being applied — a master of the wrong bin
is refused by the worker and the frames go out uncalibrated — so the label is
driven by the worker's `calibration` signal and never by the file dialog. What
is asserted here is that the three kinds stay independent (a bias refusal must
not blank the dark's line) and that the buttons reach the worker with the right
request.
"""
from __future__ import annotations

import pytest

from astrodoro.core import masters


@pytest.fixture
def window(qapp, settings):
    from astrodoro.ui.main import MainWindow
    w = MainWindow(settings)
    yield w
    w.config_window.close()
    w.close()


def test_the_panel_offers_all_three_kinds(window):
    w = window
    assert set(w._calib_labels) == set(masters.KINDS)
    for kind, label in w._calib_labels.items():
        assert label.text() == f"{kind}: none"
    for b in (w.btn_bias, w.btn_capture_bias, w.btn_dark, w.btn_capture_dark,
              w.btn_flat, w.btn_capture_flat):
        assert b.isEnabled()


def test_each_kind_writes_only_its_own_line(window):
    w = window
    w.on_calibration({"kind": "bias", "path": "/x/bias_g250_o20_bin2.fits"})

    assert w.lbl_bias.text() == "bias: bias_g250_o20_bin2.fits"
    assert w.lbl_dark.text() == "dark: none"
    assert w.lbl_flat.text() == "flat: none"


def test_a_refusal_is_shown_where_the_file_name_would_be(window):
    """The refusal used to be a line in a log that had already scrolled, while
    the panel kept showing the chosen name."""
    w = window
    w.on_calibration({"kind": "dark", "path": "",
                      "reason": "dark ignored: (100, 100) != (200, 200)"})

    assert "ignored" in w.lbl_dark.text()
    assert w.pal.bad in w.lbl_dark.styleSheet()


def test_a_file_picked_before_start_is_marked_as_unchecked(window, tmp_path,
                                                           monkeypatch):
    """Nothing has read it yet: it is verified when the camera opens, and the
    label has to say so rather than claim a correction."""
    from PySide6.QtWidgets import QFileDialog

    w = window
    picked = tmp_path / "bias_g250_o20_bin2.fits"
    seen = {}

    def fake(parent, title, folder, filt):
        seen["folder"] = folder
        return str(picked), filt

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake))
    w.pick_bias()

    assert seen["folder"] == str(w.settings.path("bias_dir"))
    assert w._bias_path == str(picked)
    assert "checked at Start" in w.lbl_bias.text()


def test_recording_a_master_needs_a_session(window):
    """All three say so instead of doing nothing: without a camera open there
    is nothing to record, and the click has to be answered."""
    w = window
    assert w.worker is None
    for call, word in ((w.capture_bias, "bias"), (w.capture_dark, "dark"),
                       (w.capture_flat, "flat")):
        w.log.clear()
        call()
        text = w.log.toPlainText()
        assert "start the capture" in text and word in text
