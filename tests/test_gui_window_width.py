"""The window's minimum width must not follow its text.

A plain `QLabel` reports the width of its whole text as its *minimum* width, and
Qt satisfies a layout's minimum by resizing the window: opening TARGETS, or a
frame arriving with "READING AND PROCESSING  +7.9s" in the exposure phase, used
to widen the window past the screen and push its right edge out of sight.

The measurement here is the central widget's minimum width — what Qt then forces
on the window. It must be the same before and after every text that changes
during a night.
"""
from __future__ import annotations

import numpy as np
import pytest

from astrodoro.drivers.svbony.sdk import Bayer

H, W = 64, 96


def _cfa(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 4000, size=(H, W), dtype=np.uint16)


def _stats(index: int, accepted: bool = True, reason: str = "") -> dict:
    return {
        "mode": "stack", "stacking": True, "integrating": True,
        "can_integrate": True, "accepted": accepted, "reason": reason,
        "frame_index": index, "bayer": Bayer.GR, "exposure": 5.0,
        "n_stars": 40 if accepted else 3, "hfr": 1.8, "fwhm": 2.4,
        "elong": 1.2 if accepted else 2.4, "halo": 1.1, "weight": 0.8,
        "limits": {"elongation": 2.5, "fwhm": 4.3, "halo": 8.0,
                   "weight": 0.4, "stars": 3},
        "n_stacked": 1 if accepted else 0,
        "n_rejected": 0 if accepted else 1,
        "rejections": {} if accepted else {"stars": 1},
        "integration": 5.0 if accepted else 0.0, "platform": {},
        "platform_advice": "—", "recording": "",
    }


@pytest.fixture
def window(qapp, settings):
    from astrodoro.ui.main import MainWindow
    w = MainWindow(settings)
    w.show()
    yield w
    w.close()


def _min_width(w) -> int:
    """What Qt would force on the window right now.

    The layout has to be activated and the pending layout requests delivered
    before asking: without both, the answer is the previous one and the test
    passes with the bug in place.
    """
    from PySide6.QtWidgets import QApplication
    w.centralWidget().layout().activate()
    QApplication.instance().processEvents()
    return w.centralWidget().minimumSizeHint().width()


def test_switching_modes_does_not_widen_the_window(window):
    w = window
    base = _min_width(w)
    for mode in ("frame", "targets", "stack", "config", "frame"):
        w.rail.select(mode)
        assert _min_width(w) == base, f"the {mode} mode widened the window"
    for view in ("map", "targets", "stack"):
        w._set_view(view)
        assert _min_width(w) == base, f"the {view} view widened the window"


def test_long_readouts_do_not_widen_the_window(window):
    w = window
    base = _min_width(w)

    # Every text that grows during a night, in its longest real form.
    w.st_target.set("NGC 6543 (Nebulosa Planetária do Olho de Gato)")
    w.phase.setText("READING AND PROCESSING  +7.9s")
    w.lbl_view.setText("REVIEWING #128 · rejected — trailed · 41 stars · "
                       "elong 2.40/2.50 · halo 1.10/8.00 · weight 0.80/0.40")
    assert _min_width(w) == base, "a long readout widened the window"

    # And the text is not lost: only what is painted gets elided.
    assert w.st_target.val.text().endswith("Olho de Gato)")
    assert "weight 0.80/0.40" in w.lbl_view.text()


def test_arriving_frames_do_not_widen_the_window(window):
    w = window
    w.rail.select("stack")
    base = _min_width(w)
    rgb = np.zeros((H, W, 3), np.float32)
    for i in range(1, 6):
        w.on_frame(rgb, rgb, _cfa(i), _stats(i, accepted=i % 2 == 1,
                                             reason="too few stars (3)"))
        w._on_tick()
        assert _min_width(w) == base, f"frame {i} widened the window"
