import numpy as np
import pytest

from astrodoro.core import platform_align as pa
from astrodoro.core.catalog import radec_to_vec

Z = np.array([0.0, 0.0, 1.0])


def _rotate_about(v, axis, angle_deg):
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    th = np.deg2rad(angle_deg)
    return v * np.cos(th) + np.cross(a, v) * np.sin(th) + a * (a @ v) * (1 - np.cos(th))


def _tangent(u):
    a = np.cross(Z, u)
    a /= np.linalg.norm(a)
    return a, np.cross(u, a)


def _axis(x_off_deg: float, y_off_deg: float) -> np.ndarray:
    p = Z + np.array([np.deg2rad(x_off_deg), np.deg2rad(y_off_deg), 0.0])
    return p / np.linalg.norm(p)


def _angles(ra_ground, dec, axis, minutes=5.0, n=40):
    u = radec_to_vec(np.array([ra_ground]), np.array([dec]))[0]
    a, b = _tangent(u)
    s = 0.01 * (b - a)
    t = np.linspace(0.0, minutes, n)
    out = []
    for minute in t:
        w = pa.SIDEREAL_DEG_PER_MIN * minute
        ax = _rotate_about(_rotate_about(a, Z, -w), axis, +w)
        ay = _rotate_about(_rotate_about(b, Z, -w), axis, +w)
        out.append(np.degrees(np.arctan2(s @ ay, s @ ax)))
    return list(t * 60.0), list(np.unwrap(out, period=360.0))


def _run(ra, dec, axis, minutes=5.0, n=40, noise=0.0, seed=1):
    live = pa.LiveAlign()
    t, ang = _angles(ra, dec, axis, minutes, n)
    rng = np.random.default_rng(seed)
    for when, a in zip(t, ang, strict=True):
        live.add_rotation(a + rng.normal(0.0, noise), when)
    return live


def _stars(n=24, seed=5):
    rng = np.random.default_rng(seed)
    return rng.uniform(200.0, 1800.0, size=(n, 2))


def _turned(xy, deg, centre=1000.0):
    th = np.deg2rad(deg)
    r = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    return (xy - centre) @ r.T + centre


def test_a_perfectly_aligned_platform_reads_nothing():
    live = _run(0.0, 0.0, Z)
    assert live.settled
    assert live.status()["error_deg"] == pytest.approx(0.0, abs=0.01)


def test_the_reading_is_the_error_projected_on_this_field():
    axis = _axis(1.0, 0.0)
    head_on = _run(0.0, 0.0, axis).status()
    sideways = _run(90.0, 0.0, axis).status()

    assert head_on["error_deg"] == pytest.approx(1.0, rel=0.02)
    assert sideways["error_deg"] < 0.1, "a field at 90° sees almost none of it"


def test_the_reading_sharpens_with_the_baseline_not_with_frames():
    axis = _axis(1.0, 0.0)
    short = _run(0.0, 0.0, axis, minutes=1.5, n=30, noise=0.003).status()
    long = _run(0.0, 0.0, axis, minutes=6.0, n=30, noise=0.003).status()
    more = _run(0.0, 0.0, axis, minutes=1.5, n=120, noise=0.003).status()

    assert long["error_sigma_deg"] < short["error_sigma_deg"] / 3
    assert more["error_sigma_deg"] > long["error_sigma_deg"]


def test_nothing_is_reported_before_the_first_baseline():
    live = pa.LiveAlign()
    t, ang = _angles(0.0, 0.0, _axis(1.0, 0.0), minutes=0.5, n=6)
    for when, a in zip(t, ang, strict=True):
        live.add_rotation(a, when)

    st = live.status()
    assert not st["settled"]
    assert not np.isfinite(st["error_deg"])
    assert pa.reading(st) == "—"
    assert "measuring" in pa.advice(st)


def test_turning_a_screw_restarts_the_reading_and_keeps_the_old_one():
    live = _run(0.0, 0.0, _axis(2.0, 0.0))
    before = live.status()["error_deg"]
    assert before == pytest.approx(2.0, rel=0.02)

    live.add_rotation(live._rot[-1] + 10.0, live._t[-1] + 3.0)

    st = live.status()
    assert st["before_deg"] == pytest.approx(before, rel=1e-6)
    assert st["adjustments"] == 1
    assert not st["settled"], "the window starts over after the tube moves"


def test_the_verdict_compares_the_two_readings():
    live = pa.LiveAlign()
    live.before_deg = 2.0
    for when, a in zip(*_angles(0.0, 0.0, _axis(0.5, 0.0)), strict=True):
        live.add_rotation(a, when)
    assert "better" in pa.verdict(live.status())

    live.before_deg = 0.1
    assert "other way" in pa.verdict(live.status())


def test_a_field_too_poor_to_measure_says_so():
    live = pa.LiveAlign()
    assert not live.add(_stars(3), 0.0)
    assert "too poor" in live.reason
    assert live.n_rejected == 1


def test_stars_give_the_same_rotation_the_register_sees():
    live = pa.LiveAlign()
    xy = _stars()
    for i in range(12):
        assert live.add(_turned(xy, 0.01 * i), float(i * 10))
    assert live.settled
    assert abs(live.fit().rate) == pytest.approx(0.01 * 6, rel=0.05)
    assert live.status()["error_deg"] == pa.error_deg(live.fit().rate)


def test_the_window_forgets_what_is_older_than_itself():
    live = pa.LiveAlign(window_s=120.0)
    for i in range(60):
        live.add_rotation(0.001 * i, float(i * 10))
    assert live.span_s <= 120.0
    assert live.n < 60
