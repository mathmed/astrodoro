"""Walks every gesture of the PLANETS screen.

Like `test_gui_framing`, it does not check appearance — it checks that each
path runs with no display, no camera and no network. The mode has its own
render path and its own readouts, which means it is the one part of the window
that a change to the deep-sky stretch cannot break loudly.
"""
from __future__ import annotations

import numpy as np
import pytest


@pytest.fixture
def window(qapp, settings):
    from astrodoro.ui.main import MainWindow
    w = MainWindow(settings)
    yield w
    w.close()


def _frame(w, **moon):
    """One frame's worth of stats, as the worker's lucky path emits them."""
    m = {"peak": 0.7, "clipped": 0.0, "lit": 0.4, "factor": 1.2,
         "advice": "peak at 70% of scale", "sharpness": 14.2, "best": 15.0,
         "window": 120, "cx": float("nan"), "cy": float("nan"),
         "recording": False, "frames": 0, "elapsed": 0.0, "progress": 0.0,
         "folder": ""}
    m.update(moon)
    live = np.clip(np.random.default_rng(1).random((40, 60, 3)), 0, 1)
    w.on_frame(live.astype(np.float32), None, None,
               {"mode": "lucky", "lucky": m, "frame_index": 7})
    return m


def test_lucky_mode_paths(window):
    w = window
    w.set_mode("lucky")
    assert w._view == "live"
    # There is no HFR and no integration here, and the vitals bar has to say so.
    assert "SHARP" in w.st_hfr.lab.text()

    w._refresh_body()
    assert w.lbl_body.text() and w.lbl_body_fit.text()

    w._body_preset()
    assert w.sp_exp.value() == pytest.approx(window.settings.lucky_exposure_s)
    assert w.sp_gain.value() == window.settings.lucky_gain

    _frame(w)
    assert w.st_peak.val.text() == "70%"
    assert w.st_sharp.val.text() == "14.2"

    before = w.sp_exp.value()
    w._apply_suggested_exposure()
    assert w.sp_exp.value() == pytest.approx(before * 1.2, rel=1e-3)

    w._lucky_fit_white()
    lo, hi = 10, 100
    assert lo <= w.sl_lucky_white.value() <= hi


# ------------------------------------------------------------- the body choice
def test_choosing_a_planet_changes_the_panel_and_tells_the_worker(window):
    """The worker measures inside a window the size of a disc, and the next
    body's disc is not this one's. The best sharpness goes with it: the two are
    not on one scale, and a Moon that scored 40 would leave Jupiter reading 3%
    of the session's best all night."""
    w = window
    w.set_mode("lucky")
    w.worker = _FakeWorker()
    w.cb_body.setCurrentIndex(w.cb_body.findData("jupiter"))

    assert w._body == "jupiter"
    assert w.worker.pending["body"] == "jupiter"
    assert "reset_focus_best" in w.worker.flags
    assert "Jupiter" in w.btn_body_point.text()
    assert "Jupiter" in w.btn_body_preset.text()
    w.worker = None


def test_the_exposure_preset_follows_the_body(window):
    """One number is kept true — the Moon's — and the rest is the ratio of
    surface brightnesses, stopped where a longer frame would average the seeing
    instead of freezing it."""
    from astrodoro.core import lucky

    w = window
    w.set_mode("lucky")
    w._body_preset()
    moon = w.sp_exp.value()
    assert moon == pytest.approx(w.settings.lucky_exposure_s)

    w.cb_body.setCurrentIndex(w.cb_body.findData("jupiter"))
    w._body_preset()
    assert w.sp_exp.value() > moon

    w.cb_body.setCurrentIndex(w.cb_body.findData("saturn"))
    w._body_preset()
    assert w.sp_exp.value() == pytest.approx(lucky.FREEZE_S)


def test_grey_world_belongs_to_the_moon_alone(window):
    """On the Moon it is a measurement — the surface really is grey. On Mars it
    would be correcting the camera for a colour the planet has."""
    w = window
    w.set_mode("lucky")
    w.cb_body.setCurrentIndex(w.cb_body.findData("mars"))
    assert not w.btn_body_balance.isEnabled()

    w._live = _cast_disc(0.5, 2.0)
    w._q = w._q_src = None
    w._lucky_balance()
    assert w._lucky_gains() == [1.0, 1.0, 1.0]


def test_the_framing_question_is_a_different_one_for_a_planet(window):
    """For the Moon it is whether the disc fits, and at 1200 mm it does not.
    For Jupiter it never arises: what decides the session is the image scale."""
    w = window
    w.set_mode("lucky")
    w._refresh_body()
    assert "frame" in w.lbl_body_fit.text() or "mosaic" in w.lbl_body_fit.text()

    w.cb_body.setCurrentIndex(w.cb_body.findData("jupiter"))
    assert "px across" in w.lbl_body_fit.text()


def test_the_body_is_remembered(window):
    w = window
    w.set_mode("lucky")
    w.cb_body.setCurrentIndex(w.cb_body.findData("saturn"))
    w.close()
    assert w.settings.lucky_body == "saturn"


# --------------------------------------------------------------- stabilisation
def _view_centre(w):
    r = w.vb.viewRect()
    return r.center().x(), r.center().y()


def test_the_view_follows_the_body(window):
    """Wind and seeing walk a planet across the screen at the magnification it
    needs. What moves is the rectangle being looked at, never the frame."""
    w = window
    w.set_mode("lucky")
    w.chk_follow.setChecked(True)
    _frame(w, cx=900.0, cy=500.0)
    before = w.img.image.copy()

    _frame(w, cx=940.0, cy=470.0)
    assert _view_centre(w) == pytest.approx((940.0, 470.0), abs=1.0)
    _frame(w, cx=860.0, cy=530.0)
    assert _view_centre(w) == pytest.approx((860.0, 530.0), abs=1.0)
    # The image itself is untouched: following is a view, not a resampling.
    assert w.img.image.shape == before.shape


def test_following_keeps_the_zoom(window):
    w = window
    w.set_mode("lucky")
    w.chk_follow.setChecked(True)
    _frame(w, cx=900.0, cy=500.0)
    size = w.vb.viewRect().width()
    _frame(w, cx=1200.0, cy=200.0)
    assert w.vb.viewRect().width() == pytest.approx(size, rel=1e-6)


def test_following_can_be_switched_off(window):
    w = window
    w.set_mode("lucky")
    w.chk_follow.setChecked(True)
    _frame(w, cx=900.0, cy=500.0)
    w.chk_follow.setChecked(False)
    parked = _view_centre(w)
    _frame(w, cx=1500.0, cy=100.0)
    assert _view_centre(w) == pytest.approx(parked, abs=0.01)


def test_fitting_the_view_stops_the_following(window):
    """Fit is "show me the whole frame", which is the opposite of following one
    body inside it — and the next frame would undo it."""
    w = window
    w.set_mode("lucky")
    w.chk_follow.setChecked(True)
    _frame(w, cx=900.0, cy=500.0)
    w.fit_view()
    assert not w.chk_follow.isChecked()


def test_a_frame_with_no_body_leaves_the_view_alone(window):
    """Cloud, a slew, the body off the edge: the last known framing is a better
    answer than jumping to a corner."""
    w = window
    w.set_mode("lucky")
    w.chk_follow.setChecked(True)
    _frame(w, cx=900.0, cy=500.0)
    parked = _view_centre(w)
    _frame(w, cx=float("nan"), cy=float("nan"), window=0)
    assert _view_centre(w) == pytest.approx(parked, abs=0.01)


def test_the_worker_reports_where_the_body_is(qapp, settings, tmp_path):
    """The number the view follows, in the recorded frame's own pixels."""
    folder = _replay_session(settings, tmp_path, size=256, radius=8)
    _w, seen = _run_worker(settings, folder, display_fps=0.0)
    m = seen[-1]["lucky"]
    assert m["cx"] == pytest.approx(128, abs=3)
    assert m["cy"] == pytest.approx(128, abs=3)


def test_the_lunar_view_is_linear(window):
    """No autostretch: two frames differing only in level must differ on screen.

    The deep-sky path renormalises per frame, which would make a dimming disc
    look identical — the exact signal you are watching for while focusing.
    """
    w = window
    w.set_mode("lucky")
    for value in (0.2, 0.6):
        w._live = np.full((20, 30, 3), value, np.float32)
        w._q = w._q_src = None
        w._render(True)
    w._live = np.full((20, 30, 3), 0.2, np.float32)
    w._q = w._q_src = None
    w._quantize(w._src())
    dark = w._stretch().mean()
    w._live = np.full((20, 30, 3), 0.6, np.float32)
    w._q = w._q_src = None
    w._quantize(w._src())
    assert w._stretch().mean() > dark


def _cast_disc(red: float, blue: float) -> np.ndarray:
    """A lit disc on black sky, with a colour cast to correct."""
    y, x = np.mgrid[0:64, 0:64]
    disc = (((x - 32) ** 2 + (y - 32) ** 2 < 26 ** 2) * 0.5).astype(np.float32)
    return np.stack([disc * red, disc, disc * blue], axis=2)


def test_colour_gains_reach_the_lunar_view(window):
    w = window
    w.set_mode("lucky")
    w._live = _cast_disc(1.0, 1.0)
    w._q = w._q_src = None
    w._quantize(w._src())
    before = w._stretch()
    w.sl_lucky_b.setValue(200)
    w._quantize(w._src())
    after = w._stretch()
    assert after[..., 2].mean() > before[..., 2].mean()
    assert after[..., 1].mean() == pytest.approx(before[..., 1].mean())


def test_balance_measures_on_the_disc_not_on_the_sky(window):
    """The frame is mostly black sky, whose median says nothing about colour."""
    w = window
    w.set_mode("lucky")
    w._live = _cast_disc(0.5, 2.0)
    w._q = w._q_src = None
    w._lucky_balance()
    red, green, blue = w._lucky_gains()
    assert green == 1.0
    assert red == pytest.approx(2.0, rel=0.02)
    assert blue == pytest.approx(0.5, rel=0.02)


def test_balance_refuses_a_frame_with_no_disc(window):
    w = window
    w.set_mode("lucky")
    w._live = np.zeros((64, 64, 3), np.float32)
    w._q = w._q_src = None
    w._lucky_balance()
    assert w._lucky_gains() == [1.0, 1.0, 1.0]


def test_saturation_spreads_the_channels(window):
    """The mineral Moon: real colour, a few percent apart, amplified."""
    w = window
    w.set_mode("lucky")
    w._live = _cast_disc(1.06, 0.94)
    w._q = w._q_src = None
    w._quantize(w._src())

    def spread(img):
        return float(np.ptp(img.reshape(-1, 3).mean(axis=0)))

    plain = spread(w._stretch())
    w.sl_lucky_sat.setValue(30)
    assert spread(w._stretch()) > plain


def test_the_histogram_follows_the_lunar_gains(window):
    """Curves that ignore what is on screen stop being an exposure meter."""
    w = window
    w.set_mode("lucky")
    w.sl_lucky_r.setValue(180)
    assert w._channel_gains() == [1.8, 1.0, 1.0]
    w.set_mode("stack")
    assert w._channel_gains() == [1.0, 1.0, 1.0]


def test_the_lunar_colour_is_remembered(window):
    w = window
    w.set_mode("lucky")
    w.sl_lucky_r.setValue(120)
    w.sl_lucky_b.setValue(90)
    w.sl_lucky_sat.setValue(25)
    w.close()
    assert w.settings.lucky_wb_red == pytest.approx(1.2)
    assert w.settings.lucky_wb_blue == pytest.approx(0.9)
    assert w.settings.lucky_saturation == pytest.approx(2.5)


def test_burst_button_needs_a_capture(window):
    w = window
    w.set_mode("lucky")
    w.btn_burst.setChecked(True)
    # No worker: the button must not sit there claiming to be recording.
    assert not w.btn_burst.isChecked()


class _FakeWorker:
    """Just enough worker to accept the button's commands."""

    def __init__(self):
        self.flags = []
        self.pending = {}

    def request(self, **kw):
        self.pending.update(kw)

    def flag(self, name):
        self.flags.append(name)


def test_a_finished_burst_releases_the_button(window):
    w = window
    w.set_mode("lucky")
    w.on_burst(True)
    _frame(w, recording=True, frames=120, elapsed=12.0, progress=0.4)
    assert w.btn_burst.isChecked()
    assert not w.lbl_burst.isHidden()
    # A burst ends at its limit with nobody pressing anything.
    w.on_burst(False)
    assert not w.btn_burst.isChecked()


def test_a_stale_frame_does_not_cancel_a_burst_just_started(window):
    """The regression: the burst recorded exactly one frame and stopped.

    A frame leaves the worker before it applies `burst_start`, so it still says
    "not recording", and it reaches the GUI after the click. Inferring the
    button state from it unchecked the button, which sent `burst_stop`.
    """
    w = window
    w.set_mode("lucky")
    w.worker = _FakeWorker()
    w.btn_burst.setChecked(True)                  # the user presses record
    assert w.worker.flags == ["burst_start"]

    _frame(w, recording=False)                    # the in-flight frame lands
    assert w.btn_burst.isChecked()
    assert w.worker.flags == ["burst_start"]      # and nothing was cancelled

    w.on_burst(True)                              # the worker confirms
    _frame(w, recording=True, frames=3, elapsed=0.2, progress=0.01)
    assert w.btn_burst.isChecked()
    w.worker = None


def test_a_refused_burst_lets_the_button_back_up(window):
    """Replay cannot record one, and the button must not stay lit for ever."""
    w = window
    w.set_mode("lucky")
    w.worker = _FakeWorker()
    w.btn_burst.setChecked(True)
    w.on_burst(False)
    assert not w.btn_burst.isChecked()
    w.worker = None


def test_releasing_the_button_does_not_re_issue_the_command(window):
    w = window
    w.set_mode("lucky")
    w.worker = _FakeWorker()
    w.btn_burst.setChecked(True)
    w.on_burst(True)
    w.on_burst(False)
    assert w.worker.flags == ["burst_start"]      # no echoed burst_stop
    w.worker = None


def test_clipping_raises_an_alert(window):
    w = window
    w.set_mode("lucky")
    _frame(w, clipped=0.02, peak=1.0, advice="CLIPPING")
    w._check_alerts()          # the tick's job, and the tick needs a capture
    assert not w.alert.isHidden()
    assert "clipping" in w.alert.text()


def test_leaving_the_mode_restores_the_deep_sky_readouts(window):
    w = window
    w.set_mode("lucky")
    w.set_mode("stack")
    assert "HFR" in w.st_hfr.lab.text()
    assert w._view == "stack"


def test_lunar_capture_values_do_not_become_the_deep_sky_defaults(window):
    """Closing in MOON must not leave the next session opening at 8 ms."""
    w = window
    before = w.settings.exposure_s
    w.set_mode("lucky")
    w._body_preset()
    w.close()
    assert w.settings.exposure_s == pytest.approx(before)


def _replay_session(settings, tmp_path, n=3, size=64, radius=26):
    """A tiny recorded session of a lit disc, for the worker to replay.

    The defaults are a body filling its frame — the Moon. A small radius in a
    big frame is the other case, and the one the measuring window exists for.
    """
    from astrodoro.core.recorder import Recorder
    from astrodoro.core.source import FrameMeta
    from astrodoro.drivers.svbony.sdk import Bayer

    y, x = np.mgrid[0:size, 0:size]
    disc = (((x - size // 2) ** 2 + (y - size // 2) ** 2 < radius ** 2)
            * 40000).astype(np.uint16)
    rec = Recorder(root=tmp_path / "sessions", target="Moon", compress=False)
    rec.begin({"name": "synthetic", "pixel_um": 4.63}, {})
    for i in range(1, n + 1):
        rec.write_sub(disc, FrameMeta(index=i, timestamp=0.0, exposure=0.008,
                                      gain=100, offset=20, bin=1,
                                      full_scale=65532, bayer=Bayer.RG))
    return rec.session_dir


def _run_worker(settings, folder, **overrides):
    from astrodoro.ui.worker import CaptureWorker, Config
    seen = []
    w = CaptureWorker(Config.from_settings(
        settings, mode="lucky", source="replay", replay_folder=str(folder),
        record=False, **overrides))
    w.frame.connect(lambda live, stack, cfa, st: seen.append(st))
    w.run()
    return w, seen


def test_the_worker_runs_the_lunar_path_end_to_end(qapp, settings, tmp_path):
    """A replayed session through the worker in MOON mode.

    The branch out of `_process` is the whole feature, and it is the one part
    that no panel test reaches: it has to measure the frame, leave the stacker
    untouched and never reach the registration.
    """
    folder = _replay_session(settings, tmp_path)
    w, seen = _run_worker(settings, folder, display_fps=0.0)

    assert len(seen) == 3
    for st in seen:
        assert st["stacking"] is False and st["can_integrate"] is False
        m = st["lucky"]
        assert m["sharpness"] > 0.0
        assert 0.5 < m["peak"] < 0.7          # 40000 of a 65532 full scale
        assert m["clipped"] == 0.0
        assert m["recording"] is False
    assert w.stacker.n_stacked == 0


def test_the_worker_measures_a_planet_inside_a_window(qapp, settings, tmp_path):
    """The same frame, measured two ways. Over the whole frame a planet is a
    fraction of a percent of the pixels and reads as an empty frame; inside the
    window it
    is a disc with headroom, and the sharpness is the body's."""
    from astrodoro.core import lucky

    folder = _replay_session(settings, tmp_path, size=256, radius=8)
    _w, seen = _run_worker(settings, folder, display_fps=0.0)

    m = seen[-1]["lucky"]
    assert 0 < m["window"] < 256
    assert 0.5 < m["peak"] < 0.7
    assert m["lit"] > 0.05
    assert "nothing bright" not in m["advice"]

    # And what the frame would have said without one.
    frame = np.zeros((256, 256), np.float32)
    y, x = np.mgrid[0:256, 0:256]
    frame[(x - 128) ** 2 + (y - 128) ** 2 < 8 ** 2] = 0.61
    assert "nothing bright" in lucky.levels(frame).verdict()


def test_the_display_is_throttled_but_the_measurement_is_not(qapp, settings,
                                                             tmp_path):
    """Every frame is measured; only some are drawn.

    The `frame` signal is queued across threads and unbounded, so at the frame
    rate of a millisecond exposure the queue would grow by a full RGB frame —
    140 MB at bin1 — for each one the GUI cannot absorb.
    """
    folder = _replay_session(settings, tmp_path, n=12)
    w, seen = _run_worker(settings, folder, display_fps=1.0)

    assert len(seen) == 1                      # a replay is far faster than 1 s
    assert len(w.sharp_meter.samples) == 12    # but all twelve were measured


def test_the_burst_does_not_compress_by_default(settings):
    """RICE caps the burst at 8 fps against 31 — measured at bin1."""
    from astrodoro.ui.worker import Config
    assert Config.from_settings(settings).burst_compress is False


def test_the_burst_writes_every_frame_it_is_given(qapp, settings, tmp_path):
    """Eight frames in, eight files out — the complaint that started this."""
    from astrodoro.core.recorder import Burst, Recorder
    from astrodoro.ui.worker import CaptureWorker, Config

    folder = _replay_session(settings, tmp_path, n=8)
    w = CaptureWorker(Config.from_settings(
        settings, mode="lucky", source="replay", replay_folder=str(folder),
        record=False, display_fps=0.0))
    w._open()
    w.src.start()
    w.burst = Burst(recorder=Recorder(root=tmp_path / "bursts", target="Moon",
                                      compress=False),
                    max_seconds=600.0)
    w.burst.begin(w.info, {})
    subs = w.burst.session_dir / "subs"

    while True:
        got = w.src.read()
        if got is None:
            break
        w._process(*got)

    assert w.burst is not None, "the burst stopped on its own"
    assert sorted(p.name for p in subs.glob("*.fits")) == [
        f"sub_{i:05d}.fits" for i in range(1, 9)]
    w.src.close()


def test_the_field_is_the_real_sensor_before_a_capture_opens(window):
    """The Moon fits at 1200 mm; a placeholder sensor said it needed a mosaic.

    Every field-of-view answer — does the target fit, how big is the disc, the
    rectangle on the sky map — is wanted before Start, which is when the frame
    on screen does not exist yet.
    """
    w = window
    w.set_mode("lucky")
    w.cb_bin.setCurrentText("1")
    wide, tall = w._fov_arcmin()
    assert (wide, tall) == pytest.approx((55.0, 37.4), abs=0.2)
    # Binning changes the pixels and the scale together: the field does not move.
    w.cb_bin.setCurrentText("2")
    assert w._fov_arcmin() == pytest.approx((wide, tall), abs=0.1)

    w._refresh_body()
    assert "mosaic" not in w.lbl_body_fit.text()
    assert "mosaico" not in w.lbl_body_fit.text()


def test_the_sensor_size_is_corrected_from_the_camera(window):
    w = window
    w.on_opened({"name": "x", "width": 1000, "height": 800, "bin": 2,
                 "bayer": "GRBG", "full_scale": 65532, "live": True,
                 "gain_range": (0, 570)})
    assert (w.settings.sensor_width, w.settings.sensor_height) == (2000, 1600)


def test_the_burst_records_what_the_camera_is_doing_now(qapp, settings, tmp_path):
    """`session.json` must agree with the frames' own headers.

    The config holds what Start was pressed with; on the Moon the exposure is
    changed a dozen times after that, and the frames record the camera while the
    session record was recording the config.
    """
    import json

    from astrodoro.core.source import CameraSource
    from astrodoro.ui.worker import CaptureWorker, Config

    class _Geometry:
        bin = 1

    class _Cam:
        exposure, gain, offset = 0.1, 250, 20
        geometry = _Geometry()

    w = CaptureWorker(Config.from_settings(
        settings, mode="lucky", exposure=1.0, gain=120, bin=2,
        record_root=str(tmp_path)))
    w.src = CameraSource.__new__(CameraSource)     # only the type is checked
    w.src.cam = _Cam()
    w.info = {"name": "SV405CC", "pixel_um": 4.63}
    w._start_burst()

    assert w.burst is not None
    written = json.loads((w.burst.session_dir / "session.json").read_text())
    assert written["config"]["exposure"] == 0.1
    assert written["config"]["gain"] == 250
    assert written["config"]["bin"] == 1


# ------------------------------------------------------- calibration honesty
def test_a_refused_dark_is_not_reported_as_loaded(window):
    """A dark of the wrong geometry is refused by the worker and the frames go
    out uncalibrated. The panel used to show the chosen name anyway."""
    w = window
    w.on_calibration({"kind": "dark", "path": "",
                      "reason": "dark ignored: (1410, 2072) != (2822, 4144)"})
    assert "ignored" in w.lbl_dark.text()
    assert w.pal.bad in w.lbl_dark.styleSheet()


def test_an_accepted_dark_is_named(window):
    w = window
    w.on_calibration({"kind": "dark", "path": "/d/dark_g120_bin2.fits"})
    assert "dark_g120_bin2.fits" in w.lbl_dark.text()
    assert w.pal.ok in w.lbl_dark.styleSheet()


def test_no_calibration_says_none(window):
    w = window
    w.on_calibration({"kind": "flat", "path": ""})
    assert w.lbl_flat.text() == "flat: none"


def test_a_dark_picked_before_start_is_marked_unverified(window):
    """Nothing has checked it yet, and saying so is the difference between
    'loaded' and 'chosen'."""
    w = window
    w._show_calibration("dark", "/d/dark_g120_bin2.fits", pending=True)
    assert "Start" in w.lbl_dark.text()
    assert w.pal.warn in w.lbl_dark.styleSheet()


# ------------------------------------------------------------- histogram scale
def _sky_with_stars(n=100_000, sky=0.08, hot=0.0):
    """A frame-shaped sample: a sky level, a bright tail, and hot pixels."""
    rng = np.random.default_rng(0)
    a = np.clip(rng.normal(sky, 0.004, n), 0, 1).astype(np.float32)
    a[:60] = rng.uniform(0.2, 0.9, 60)                 # stars
    if hot:
        a[60:60 + int(n * hot)] = 0.95                 # hot pixels
    return a


def test_one_bright_star_does_not_set_the_histogram_scale(qapp):
    """Measured on a real sub: 0.1% of the pixels were taking 84% of the plot."""
    from astrodoro.ui.main import _histogram_top

    a = _sky_with_stars()
    top = _histogram_top([a])
    assert top < float(a.max()) / 2
    # The sky has to land somewhere readable, not against either edge.
    assert 0.2 < 0.08 / top < 0.6


def test_hot_pixels_cannot_move_the_scale(qapp):
    """This camera delivers a few hundredths of a percent of them, which is
    exactly the population a 99.99th percentile would land in."""
    from astrodoro.ui.main import _histogram_top

    clean = _histogram_top([_sky_with_stars()])
    dirty = _histogram_top([_sky_with_stars(hot=0.0004)])
    assert dirty == pytest.approx(clean, rel=0.02)


def test_a_saturated_frame_still_shows_its_wall(qapp):
    """The histogram is also how you see you are clipping; cropping the top out
    would hide exactly that."""
    from astrodoro.ui.main import _histogram_top

    blown = _sky_with_stars()
    blown[:20_000] = 1.0
    assert _histogram_top([blown]) >= 1.0


def test_a_few_saturated_pixels_are_not_a_blown_frame(qapp):
    from astrodoro.ui.main import _histogram_top

    frame = _sky_with_stars()
    frame[:20] = 1.0                                   # 0.02%, below the floor
    assert _histogram_top([frame]) < 0.5


def test_an_empty_frame_has_a_usable_scale(qapp):
    from astrodoro.ui.main import _histogram_top

    assert _histogram_top([np.zeros(1000, np.float32)]) > 0


def test_the_lucky_histogram_is_themed_and_carries_its_lines(window):
    """It was added without reaching either of the loops that style the other
    two, so it drew with default pens and a white-point line stuck at zero."""
    w = window
    w.set_mode("lucky")
    w._live = np.full((40, 60, 3), 0.3, np.float32)
    w._q = w._q_src = None
    w._render(True)
    assert w.hist3_white.value() == pytest.approx(
        w.sl_white.value() / w.sl_white._div)
    assert w.hist3_rgb[0].opts["pen"] is not None
    assert w.hist3_black.value() == pytest.approx(w._black)
