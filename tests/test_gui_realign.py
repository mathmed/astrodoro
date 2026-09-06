"""Pause & realign: the on-image arrow and the state label.

Drives MainWindow.on_frame directly with synthetic stats dicts, the same
pattern as test_gui_frame_review.py — no camera, no worker thread.
"""
from __future__ import annotations

import numpy as np
import pytest

from astrodoro.drivers.svbony.sdk import Bayer

H, W = 64, 96


def _cfa(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 4000, size=(H, W), dtype=np.uint16)


def _stats(realign=None, realigning=False) -> dict:
    return {
        "mode": "stack", "stacking": False, "integrating": False,
        "can_integrate": True, "realigning": realigning, "realign": realign,
        "accepted": None, "reason": "", "n_stars": 20, "hfr": 1.5,
        "fwhm": 2.0, "elong": 1.1, "halo": 1.0, "weight": 1.0, "limits": {},
        "n_stacked": 3, "n_rejected": 0, "rejections": {},
        "integration": 10.0, "platform": {}, "platform_advice": "—",
        "recording": "", "bayer": Bayer.GR, "exposure": 5.0,
    }


class _FakeWorker:
    """Just enough worker to accept the button's flags (test_gui_moon.py)."""

    def __init__(self):
        self.flags = []

    def flag(self, name):
        self.flags.append(name)

    def stop(self):
        pass


@pytest.fixture
def window(qapp, settings):
    from astrodoro.ui.main import MainWindow
    w = MainWindow(settings)
    yield w
    w.close()


def test_arrow_hidden_outside_realign(window):
    rgb = np.zeros((H, W, 3), np.float32)
    window.on_frame(rgb, None, _cfa(), _stats(realign=None))
    assert not window.realign_arrow.isVisible()


def test_arrow_shown_pointing_at_a_correction(window):
    rgb = np.zeros((H, W, 3), np.float32)
    info = {"ok": True, "dx": 12.0, "dy": -5.0, "distance": 13.0,
            "rotation_deg": 0.2, "reason": ""}
    window.on_frame(rgb, None, _cfa(), _stats(realign=info, realigning=True))
    assert window.realign_arrow.isVisible()
    assert window._state == "realigning"


def test_arrow_reports_on_target_below_the_threshold(window):
    rgb = np.zeros((H, W, 3), np.float32)
    info = {"ok": True, "dx": 1.0, "dy": 0.5, "distance": 1.1,
            "rotation_deg": 0.0, "reason": ""}
    window.on_frame(rgb, None, _cfa(), _stats(realign=info, realigning=True))
    assert window.realign_arrow.isVisible()
    assert window.realign_arrow._pointing is False    # circled, not an arrow


def test_arrow_flags_a_failed_registration_with_the_specific_reason(window):
    """Auditable on screen, per CLAUDE.md: not a generic "reframe" message —
    the actual reason register.estimate() failed with."""
    rgb = np.zeros((H, W, 3), np.float32)
    info = {"ok": False, "dx": 0.0, "dy": 0.0, "distance": 0.0,
            "rotation_deg": 0.0, "reason": "only 2 stars detected"}
    window.on_frame(rgb, None, _cfa(), _stats(realign=info, realigning=True))
    assert window.realign_arrow.isVisible()
    assert window.realign_arrow._pointing is False
    assert window.realign_arrow._label == "only 2 stars detected"


def test_toggling_the_button_flips_label_and_hides_the_arrow(window):
    window.worker = _FakeWorker()
    window.btn_realign.setChecked(True)
    assert "Resume" in window.btn_realign.text()
    assert window.worker.flags == ["realign_on"]

    rgb = np.zeros((H, W, 3), np.float32)
    info = {"ok": True, "dx": 8.0, "dy": 0.0, "distance": 8.0,
            "rotation_deg": 0.0, "reason": ""}
    window.on_frame(rgb, None, _cfa(), _stats(realign=info, realigning=True))
    assert window.realign_arrow.isVisible()

    window.btn_realign.setChecked(False)
    assert "Pause & realign" in window.btn_realign.text()
    assert not window.realign_arrow.isVisible()
    assert window.worker.flags == ["realign_on", "realign_off"]


def test_checking_realign_before_a_session_exists_reverts_and_logs(window):
    """No worker, no stack to pause — the button must not get stuck on
    "Resume" with nothing actually paused (test_gui_moon.py's btn_burst
    guard is the precedent for this pattern)."""
    assert window.worker is None
    window.btn_realign.setChecked(True)
    assert not window.btn_realign.isChecked()
    assert "start the capture first" in window.log.toPlainText()
