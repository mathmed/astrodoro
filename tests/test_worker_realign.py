"""`_realign_info`'s screen-space math, isolated from Qt and from the camera.

The worker inverts the affine `register.estimate()` returns to find where the
target currently sits in THIS frame's own pixels — the point the on-image
arrow is drawn at. That inversion is worth locking down on its own: get the
sign wrong and the arrow points away from the target instead of at it.
"""
from __future__ import annotations

import numpy as np

from astrodoro.core.stacker import LiveStacker
from astrodoro.core.stars import detect
from astrodoro.ui.worker import CaptureWorker

H = W = 400
RNG = np.random.default_rng(3)
STARS = np.column_stack([RNG.uniform(20, W - 20, 80),
                         RNG.uniform(20, H - 20, 80)])
FLUX = 10 ** RNG.uniform(1.8, 3.2, len(STARS))
SKY = 100.0
SIGMA_PSF = 1.8


def render(shift):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.full((H, W), SKY, dtype=np.float32)
    for (x0, y0), f in zip(STARS, FLUX, strict=True):
        x, y = x0 + shift[0], y0 + shift[1]
        img += f * np.exp(-((xx - x) ** 2 + (yy - y) ** 2)
                          / (2 * SIGMA_PSF ** 2))
    return RNG.poisson(np.clip(img, 0, None)).astype(np.float32)


def _worker_with_reference():
    # __new__ without __init__ skips QObject construction on purpose:
    # _realign_info touches only self.stacker/self.info, no event loop needed.
    st = LiveStacker((H, W), channels=1, min_matched=8, max_rms=2.0)
    st.add(render((0.0, 0.0))[:, :, None], render((0.0, 0.0)), exposure=1.0,
           lum_scale=1.0)
    assert st.started
    w = CaptureWorker.__new__(CaptureWorker)
    w.stacker = st
    w.info = {"width": W, "height": H}
    return w


def test_realign_info_points_at_the_targets_current_position():
    w = _worker_with_reference()
    shift = (18.0, -11.0)
    stars = detect(render(shift), scale=1.0)
    info = w._realign_info(stars)
    assert info["ok"]
    assert abs(info["dx"] - shift[0]) < 1.0
    assert abs(info["dy"] - shift[1]) < 1.0
    assert abs(info["distance"] - float(np.hypot(*shift))) < 1.0


def test_realign_info_reports_on_target_for_a_tiny_offset():
    w = _worker_with_reference()
    stars = detect(render((0.3, -0.2)), scale=1.0)
    info = w._realign_info(stars)
    assert info["ok"]
    assert info["distance"] < 2.0


def test_realign_info_fails_gracefully_with_no_reference():
    w = CaptureWorker.__new__(CaptureWorker)
    w.stacker = LiveStacker((H, W), channels=1)
    w.info = {"width": W, "height": H}
    info = w._realign_info(detect(render((0.0, 0.0)), scale=1.0))
    assert not info["ok"]
    assert info["reason"]


def test_realign_pauses_and_resumes_without_losing_the_stack(qapp, settings):
    """The core requirement: pausing to recentre must not cost the stack."""
    from astrodoro.ui.worker import Config

    w = CaptureWorker(Config.from_settings(settings, mode="stack", record=False))
    st = LiveStacker((H, W), channels=1, min_matched=8, max_rms=2.0)
    st.add(render((0.0, 0.0))[:, :, None], render((0.0, 0.0)), exposure=1.0,
           lum_scale=1.0)
    w.stacker = st
    w.integrating = True
    w.platform.started = True
    n_before, accum_before = st.n_stacked, st.accum.copy()

    w.flag("realign_on")
    w._apply_pending()
    assert w._realigning and not w.integrating
    assert st.n_stacked == n_before
    assert np.array_equal(st.accum, accum_before)

    w.flag("realign_off")
    w._apply_pending()
    assert not w._realigning and w.integrating
    assert st.n_stacked == n_before, "resuming must not lose the stack"
    assert np.array_equal(st.accum, accum_before)
    assert st._relax == 5, "resuming must relax thresholds like new_segment()"


def test_realign_on_without_a_reference_is_a_no_op(qapp, settings):
    from astrodoro.ui.worker import Config

    w = CaptureWorker(Config.from_settings(settings, mode="stack", record=False))
    w.stacker = LiveStacker((H, W), channels=1)
    w.integrating = True

    w.flag("realign_on")
    w._apply_pending()
    assert not w._realigning
    assert w.integrating, "nothing to realign against, so nothing should pause"


def test_realigning_is_gated_by_can_integrate(qapp, settings):
    """Leaving stack/config mode mid-pause (e.g. to check FRAME or TARGETS)
    must not leave REALIGNING and the on-image arrow stuck there: reported
    "realigning" is gated by can_integrate exactly like `stacking` already
    gates on integrating AND can_integrate. The pause itself (_realigning)
    survives the switch — only what gets shown does not."""
    from astrodoro.ui.worker import Config

    w = CaptureWorker(Config.from_settings(settings, mode="stack", record=False))
    w.stacker = LiveStacker((H, W), channels=1)
    w._realigning = True
    assert w._stats_snapshot()["realigning"] is True

    w.cfg.mode = "frame"
    assert w._stats_snapshot()["realigning"] is False
    assert w._realigning, "the pause itself must survive a mode switch"

    w.cfg.mode = "stack"
    assert w._stats_snapshot()["realigning"] is True
