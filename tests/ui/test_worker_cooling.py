from __future__ import annotations

import pytest

from astrodoro.drivers import Bayer


class _Cam:
    supports_cooler = True
    bayer = Bayer.GR
    full_scale = 4095
    min_target_temperature = -40.0
    max_target_temperature = 30.0

    def __init__(self, temperature=30.4):
        self.temperature = temperature
        self.cooler_power = 0
        self.cooler = False
        self._target = temperature

    @property
    def target_temperature(self):
        return self._target

    @target_temperature.setter
    def target_temperature(self, celsius):
        if not (self.min_target_temperature <= celsius <= self.max_target_temperature):
            raise ValueError(
                f"TARGET_TEMPERATURE={celsius} outside "
                f"[{self.min_target_temperature}, "
                f"{self.max_target_temperature}]"
            )
        self._target = celsius


class _Source:
    live = True

    def __init__(self, cam):
        self.cam = cam


@pytest.fixture
def worker(settings):
    from astrodoro.ui.worker import CaptureWorker, Config

    w = CaptureWorker(Config.from_settings(settings, mode="stack", record=False))
    w.src = _Source(_Cam())
    return w


def test_hot_ambient_does_not_raise(worker):
    worker.cool.set_target(-5.0, current=worker.src.cam.temperature)
    worker._step_cooling(force=True)
    assert worker.src.cam.target_temperature == pytest.approx(30.0)
    assert worker.cool.state.phase == "cooling"


def test_ramps_down_into_range_after_clamped_start(worker, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("astrodoro.ui.worker.time.time", lambda: clock[0])

    worker.cool.set_target(-5.0, current=worker.src.cam.temperature)
    for _ in range(3):
        clock[0] += 60.0
        worker._t_cool = 0.0
        worker._step_cooling(force=True)
    assert worker.src.cam.target_temperature < 30.0
