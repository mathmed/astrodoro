from __future__ import annotations

import numpy as np

from astrodoro.drivers import Bayer
from astrodoro.ui.history import FrameHistory

H, W = 64, 96


def _cfa(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 4000, size=(H, W), dtype=np.uint16)


def _stats(index: int, accepted: bool, reason: str = "") -> dict:
    return {
        "mode": "stack",
        "stacking": True,
        "integrating": True,
        "can_integrate": True,
        "accepted": accepted,
        "reason": reason,
        "frame_index": index,
        "bayer": Bayer.GR,
        "exposure": 5.0,
        "n_stars": 40 if accepted else 3,
        "hfr": 1.8,
        "fwhm": 2.4,
        "elong": 1.2 if accepted else 2.4,
        "halo": 1.1,
        "weight": 0.8,
        "limits": {
            "elongation": 2.5,
            "fwhm": 4.3,
            "halo": 8.0,
            "weight": 0.4,
            "stars": 3,
        },
        "n_stacked": 1 if accepted else 0,
        "n_rejected": 0 if accepted else 1,
        "rejections": {} if accepted else {"stars": 1},
        "integration": 5.0 if accepted else 0.0,
        "platform": {},
        "platform_advice": "—",
        "recording": "",
    }


def test_history_respects_the_memory_budget():
    h = FrameHistory(budget_mb=1)
    big = np.zeros((512, 512), np.uint16)
    for i in range(3):
        h.push(i, big.copy(), Bayer.GR, {"index": i})
    assert len(h) == 2, f"kept {len(h)} frames with a 1 MB budget"
    assert h.get(0) is None and 2 in h
    assert h.nbytes <= h.budget

    before = h.nbytes
    h.push(2, big.copy(), Bayer.GR, {"index": 2})
    assert h.nbytes == before and len(h) == 2

    h.clear()
    assert len(h) == 0 and h.nbytes == 0


def test_the_reviewed_frame_is_the_frame_that_went_through():
    from astrodoro.core import debayer

    cfa = _cfa(7)
    h = FrameHistory()
    h.push(12, cfa, Bayer.GR, {"index": 12})
    expected = (
        debayer.to_rgb(cfa, Bayer.GR, quality="linear").astype(np.float32) / 65535.0
    )
    assert np.array_equal(h.get(12).rgb(), expected)


def test_clicking_the_strip_shows_the_frame(window):
    w = window

    for i, (acc, reason) in enumerate(
        ((True, ""), (False, "too few stars (3)")), start=1
    ):
        rgb = np.zeros((H, W, 3), np.float32) + i / 10.0
        w.on_frame(rgb, rgb, _cfa(i), _stats(i, acc, reason))

    assert len(w.health.marks) == 2 and len(w._hist) == 2
    live = w.image.source()

    w.health.chosen.emit(1)
    assert w.image._revisit is not None and w.image._revisit["index"] == 2
    assert w.health.sel == 1
    assert w.image.source().shape == (H, W, 3)
    assert w.image.source() is not live
    assert "REVIEWING #2" in w.image.lbl_view.text()
    assert "too few stars" in w.image.lbl_view.text()
    assert "elong 2.40/2.50" in w.image.lbl_view.text(), w.image.lbl_view.text()
    assert "halo 1.10/8.00" in w.image.lbl_view.text()
    assert "weight 0.80/0.40" in w.image.lbl_view.text()
    assert "FAIL" not in w.health.tooltip(0), "an accepted frame fails nothing"

    st = _stats(9, False, "little signal")
    st["weight"] = 0.20
    w.on_frame(np.zeros((H, W, 3), np.float32), None, _cfa(9), st)
    assert "FAIL" in w.health.tooltip(len(w.health.marks) - 1)

    w.on_frame(np.zeros((H, W, 3), np.float32), None, _cfa(3), _stats(3, True))
    assert w.image._revisit is not None and w.image.source() is w.image._revisit["rgb"]

    w.image.exit_review()
    assert w.image._revisit is None and w.health.sel is None
    assert "REVIEWING" not in w.image.lbl_view.text()

    w.health.chosen.emit(0)
    assert w.image._revisit is not None
    w._set_view("live")
    assert w.image._revisit is None and w.health.sel is None

    w._hist.clear()
    w.health.chosen.emit(0)
    assert w.image._revisit is None
    assert "no longer in memory" in w.log.toPlainText()
