"""The calibration flow of a session: record a master, load it, apply it.

`tests/test_calibration.py` covers the arithmetic. What is asserted here is the
flow around it, which is where the mistakes are cheap to make and expensive to
notice in the field: a master recorded with the wrong parameters in its name, a
bias silently applied on top of a dark, a flat that kept the offset pedestal
because the only dark available was of the session's exposure rather than the
flat's.

There is no camera and no `ReplaySource` here: replay has no `cam`, and
recording a master is exactly the path that needs one. The stand-in below is
the smallest surface the master flow touches — geometry, gain, offset,
exposure, temperature, and frames that arrive — and it returns frames built to
a known pedestal so the written master can be checked against arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from astrodoro.core.recorder import read_fits
from astrodoro.core.source import FrameMeta
from astrodoro.drivers.svbony.camera import Geometry
from astrodoro.drivers.svbony.sdk import Bayer

H, W = 24, 32
SCALE = 4095
PEDESTAL = 500.0
MIN_EXPOSURE = 3.6e-5


class _Cam:
    supports_cooler = True
    bayer = Bayer.GR
    full_scale = SCALE
    min_exposure = MIN_EXPOSURE
    temperature = -10.0

    def __init__(self, exposure=5.0, gain=250, offset=20, bin=2):
        self.exposure = exposure
        self.gain = gain
        self.offset = offset
        self.geometry = Geometry(x=0, y=0, width=W, height=H, bin=bin)


class _Source:
    """Frames of `level` plus the pedestal, as a capped or lit sensor would."""

    live = True

    def __init__(self, level=0.0, noise=0.0, vignette=None):
        self.cam = _Cam()
        self.level = level
        self.noise = noise
        self.vignette = vignette
        self.applied: list[dict] = []
        self.n = 0
        self.rng = np.random.default_rng(3)

    def open(self):
        return {"width": W, "height": H, "bin": self.cam.geometry.bin,
                "full_scale": SCALE, "bayer": self.cam.bayer, "live": True}

    def read(self):
        self.n += 1
        frame = np.full((H, W), PEDESTAL + self.level, dtype=np.float32)
        if self.vignette is not None:
            frame = PEDESTAL + self.level * self.vignette
        if self.noise:
            frame = frame + self.rng.normal(0, self.noise, (H, W))
        meta = FrameMeta(index=self.n, timestamp=0.0,
                         exposure=self.cam.exposure, gain=self.cam.gain,
                         offset=self.cam.offset, bin=self.cam.geometry.bin,
                         full_scale=SCALE, bayer=self.cam.bayer)
        return np.clip(frame, 0, SCALE).astype(np.uint16), meta

    def apply(self, **kw):
        self.applied.append(dict(kw))
        for k, v in kw.items():
            setattr(self.cam, k, v)
        return []

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


@dataclass
class _Log:
    lines: list

    def __call__(self, line):
        self.lines.append(str(line))

    def has(self, fragment):
        return any(fragment in line for line in self.lines)


@pytest.fixture
def worker(settings, tmp_path):
    """A worker wired to the stand-in source, with its own master folders."""
    from astrodoro.ui.worker import CaptureWorker, Config

    settings.bias_dir = str(tmp_path / "bias")
    settings.dark_dir = str(tmp_path / "darks")
    settings.flat_dir = str(tmp_path / "flats")
    w = CaptureWorker(Config.from_settings(settings, mode="stack",
                                           record=False))
    w.src = _Source()
    w.info = w.src.open()
    w.logged = _Log([])
    w.log.connect(w.logged)
    return w


def _master(path, kind, value, *, gain=250, offset=20, exposure=5.0, bin=2,
            temperature=-10.0, shape=(H, W)):
    from astrodoro.core import masters
    s = masters.Setup(gain=gain, offset=offset, exposure=exposure, bin=bin,
                      temperature=temperature, bayer="GRBG",
                      full_scale=SCALE)
    return masters.write(path, kind, np.full(shape, value, np.float32), s, 20)


# ------------------------------------------------------------------- loading
def test_a_matching_bias_is_loaded_and_announced(worker, tmp_path):
    path = _master(tmp_path, "bias", PEDESTAL)
    worker.cfg.bias_path = str(path)
    worker._load_bias()

    assert worker._bias is not None
    assert float(np.median(worker._bias)) == pytest.approx(PEDESTAL)
    assert worker.logged.has("bias: bias_g250_o20_bin2.fits")


def test_a_bias_of_another_bin_is_refused_not_broadcast(worker, tmp_path):
    path = _master(tmp_path, "bias", PEDESTAL, shape=(H // 2, W // 2), bin=4)
    worker.cfg.bias_path = str(path)
    worker._load_bias()

    assert worker._bias is None
    assert worker.logged.has("ignored")


def test_an_unreadable_master_leaves_the_session_uncalibrated(worker, tmp_path):
    bad = tmp_path / "not-a-fits.fits"
    bad.write_text("nope")
    worker.cfg.bias_path = str(bad)
    worker._load_bias()

    assert worker._bias is None
    assert worker.logged.has("unreadable bias")


def test_a_bias_at_another_offset_warns(worker, tmp_path):
    """The offset is the pedestal: at offset 8 the bias removes a constant the
    frames never had, and the frames go out with a residual."""
    path = _master(tmp_path, "bias", 200.0, offset=8)
    worker.cfg.bias_path = str(path)
    worker._load_bias()

    assert worker._bias is not None, "a warning, not a refusal"
    assert worker.logged.has("offset")


def test_a_bias_next_to_a_dark_says_it_is_not_applied(worker, tmp_path):
    """Both loaded is a legitimate state — the dark may be refused later, or
    the user may swap it — but the panel must not claim two corrections when
    `calibrate` applies one."""
    worker.cfg.dark_path = str(_master(tmp_path, "dark", PEDESTAL + 40.0))
    worker._load_dark()
    worker.cfg.bias_path = str(_master(tmp_path, "bias", PEDESTAL))
    worker._load_bias()

    assert worker._dark is not None and worker._bias is not None
    assert worker.logged.has("already contains the bias")


def test_the_flat_is_normalised_at_load_time(worker, tmp_path):
    path = _master(tmp_path, "flat", 20000.0)
    worker.cfg.flat_path = str(path)
    worker._load_flat()

    assert float(np.median(worker._flat)) == pytest.approx(1.0, abs=1e-4)


def test_a_flat_with_no_signal_is_refused(worker, tmp_path):
    worker.cfg.flat_path = str(_master(tmp_path, "flat", 0.0))
    worker._load_flat()

    assert worker._flat is None
    assert worker.logged.has("median <= 0")


# ------------------------------------------------------------------ recording
def test_recording_a_bias_uses_the_shortest_exposure_and_puts_it_back(worker):
    worker.src.level = 0.0
    worker._capture_bias(6)

    assert [a.get("exposure") for a in worker.src.applied] == [MIN_EXPOSURE, 5.0]
    assert worker.src.cam.exposure == pytest.approx(5.0), (
        "the session's exposure has to come back, or the next light is a bias")
    # One stale frame before, six kept, one thrown away after: the frame in
    # flight while the exposure changes belongs to the previous state, both
    # times, and the second one would otherwise be a black sub on disk.
    assert worker.src.n == 1 + 6 + 1


def test_a_recorded_bias_is_named_for_what_it_must_match(worker):
    worker._capture_bias(6)

    path = worker.cfg.bias_path
    assert path is not None and path.endswith("bias_g250_o20_bin2.fits")
    data, hdr = read_fits(path)
    assert hdr["IMAGETYP"] == "BIAS"
    assert hdr["GAIN"] == 250 and hdr["OFFSET"] == 20
    assert hdr["NCOMBINE"] == 6
    # The exposure recorded is the one it was taken at, not the session's.
    assert hdr["EXPTIME"] == pytest.approx(MIN_EXPOSURE)
    assert float(np.median(data)) == pytest.approx(PEDESTAL)


def test_a_recorded_bias_is_loaded_straight_away(worker):
    seen = []
    worker.bias_saved.connect(seen.append)
    worker._capture_bias(6)

    assert worker._bias is not None, "recorded and then not applied is a trap"
    assert seen == [worker.cfg.bias_path]


def test_a_bias_needs_the_camera(worker):
    worker.src.cam = None
    worker._capture_bias(6)

    assert worker.cfg.bias_path is None
    assert worker.logged.has("needs the camera")


def test_too_few_frames_is_not_a_master(worker):
    worker._capture_bias(1)
    assert worker.cfg.bias_path is None
    assert worker.logged.has("not enough frames")


def test_a_recorded_dark_still_carries_its_temperature(worker):
    worker.src.level = 40.0
    worker._capture_dark(6)

    path = worker.cfg.dark_path
    assert path.endswith("dark_g250_o20_e5.00s_bin2_-10C.fits")
    _d, hdr = read_fits(path)
    assert hdr["CCD-TEMP"] == pytest.approx(-10.0)
    assert worker._hot is not None, "the hot pixel map comes with the dark"


# ------------------------------------------- the pedestal a flat is corrected by
def _vignette(depth=0.5):
    y, x = np.mgrid[0:H, 0:W]
    r = np.hypot((y - H / 2) / (H / 2), (x - W / 2) / (W / 2)) / np.sqrt(2)
    return (1.0 - (1.0 - depth) * r**2).astype(np.float32)


def test_a_flat_is_corrected_by_the_bias_not_by_a_five_second_dark(worker,
                                                                  tmp_path):
    """The whole reason bias exists in this program. A flat is milliseconds
    long; the session's dark is seconds and carries thermal signal the flat
    never collected. Subtracting it would dig a hole in the correction."""
    worker.cfg.bias_path = str(_master(tmp_path, "bias", PEDESTAL))
    worker._load_bias()
    worker.cfg.dark_path = str(_master(tmp_path, "dark", PEDESTAL + 300.0))
    worker._load_dark()

    worker.src.level = 2000.0
    worker.src.cam.exposure = 0.05
    worker._capture_flat(6)

    assert worker.logged.has("bias subtracted from the flat")
    data, _hdr = read_fits(worker.cfg.flat_path)
    assert float(np.median(data)) == pytest.approx(2000.0, abs=2.0)


def test_a_dark_of_the_flats_own_exposure_does_qualify(worker, tmp_path):
    worker.src.cam.exposure = 0.05
    worker.cfg.dark_path = str(_master(tmp_path, "dark", PEDESTAL,
                                       exposure=0.05))
    worker._load_dark()

    worker.src.level = 2000.0
    worker._capture_flat(6)

    assert worker.logged.has("session dark subtracted from the flat")
    data, _hdr = read_fits(worker.cfg.flat_path)
    assert float(np.median(data)) == pytest.approx(2000.0, abs=2.0)


def test_a_flat_with_no_pedestal_available_says_so(worker):
    worker.src.level = 2000.0
    worker.src.cam.exposure = 0.05
    worker._capture_flat(6)

    assert worker.logged.has("pedestal stays baked into the flat")
    data, _hdr = read_fits(worker.cfg.flat_path)
    assert float(np.median(data)) == pytest.approx(PEDESTAL + 2000.0, abs=2.0)


def test_the_recorded_flat_carries_the_vignetting_it_measured(worker, tmp_path):
    worker.cfg.bias_path = str(_master(tmp_path, "bias", PEDESTAL))
    worker._load_bias()
    worker.src.cam.exposure = 0.05
    worker.src.level = 2000.0
    worker.src.vignette = _vignette(depth=0.5)
    worker._capture_flat(6)

    worker._load_flat()
    assert worker._flat is not None
    assert worker._flat.min() == pytest.approx(0.5 / float(
        np.median(worker.src.vignette)), rel=0.03)


# ------------------------------------------------------- applied to the frames
def test_the_session_applies_bias_and_flat_to_the_frame_it_emits(worker,
                                                                 tmp_path):
    """End of the flow, through `_process` itself: the mosaic the stacker and
    the frame history receive has the pedestal removed and the vignetting
    divided out."""
    from astrodoro.core import masters

    vig = _vignette(depth=0.6)
    worker.cfg.bias_path = str(_master(tmp_path, "bias", PEDESTAL))
    worker._load_bias()
    worker.cfg.flat_path = str(masters.write(
        tmp_path, "flat", (20000.0 * vig).astype(np.float32),
        masters.Setup(gain=250, offset=20, exposure=0.05, bin=2,
                      bayer="GRBG", full_scale=SCALE), 20))
    worker._load_flat()
    worker._new_stacker()

    worker.src.level = 300.0
    worker.src.vignette = vig
    raw, meta = worker.src.read()

    seen = []
    worker.frame.connect(lambda *a: seen.append(a))
    worker._process(raw, meta)

    assert seen, "the frame has to reach the GUI even with nothing to stack"
    cfa = seen[0][2].astype(np.float32) / 65535.0
    expected = 300.0 * float(np.median(vig)) / SCALE
    assert np.allclose(cfa, expected, atol=3.0 / SCALE)
    assert cfa.std() < 1.5 / SCALE, "the vignetting should be gone"


def test_without_masters_the_frame_still_carries_the_pedestal(worker):
    """The control: the same frame with nothing loaded comes out as the sensor
    delivered it, pedestal included. Otherwise the test above could pass on a
    pipeline that subtracts something else."""
    worker._new_stacker()
    worker.src.level = 300.0
    raw, meta = worker.src.read()

    seen = []
    worker.frame.connect(lambda *a: seen.append(a))
    worker._process(raw, meta)

    cfa = seen[0][2].astype(np.float32) / 65535.0
    assert np.allclose(cfa, (PEDESTAL + 300.0) / SCALE, atol=2.0 / SCALE)


# ------------------------------------------------ the same masters, headless
def test_the_headless_session_loads_all_three_masters(tmp_path):
    """`astrodoro run` used to accept only --dark: a flat could be recorded and
    then had nowhere to be applied outside the GUI. Both front ends now read
    the masters through the same helper."""
    from types import SimpleNamespace

    from astrodoro.cli.stack import load_masters

    args = SimpleNamespace(
        bias=str(_master(tmp_path, "bias", PEDESTAL)),
        dark=str(_master(tmp_path, "dark", PEDESTAL + 40.0)),
        flat=str(_master(tmp_path, "flat", 20000.0)))
    bias, dark, flat, _hot = load_masters(args, shape=(H, W))

    assert float(np.median(bias)) == pytest.approx(PEDESTAL)
    assert float(np.median(dark)) == pytest.approx(PEDESTAL + 40.0)
    # Normalised on the way in, like the GUI does at load time.
    assert float(np.median(flat)) == pytest.approx(1.0, abs=1e-4)


def test_the_headless_session_refuses_a_master_of_another_bin(tmp_path):
    """Headless there is nobody watching a panel, so a mismatch is fatal rather
    than a line that scrolls past."""
    from types import SimpleNamespace

    from astrodoro.cli.stack import load_masters

    args = SimpleNamespace(bias=None, dark=None,
                           flat=str(_master(tmp_path, "flat", 20000.0,
                                            shape=(H // 2, W // 2), bin=4)))
    with pytest.raises(SystemExit, match="flat"):
        load_masters(args, shape=(H, W))


def test_a_flat_with_no_signal_is_dropped_headless_too(tmp_path):
    from types import SimpleNamespace

    from astrodoro.cli.stack import load_masters

    args = SimpleNamespace(bias=None, dark=None,
                           flat=str(_master(tmp_path, "flat", 0.0)))
    assert load_masters(args, shape=(H, W))[2] is None
