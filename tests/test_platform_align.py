"""Polar alignment from the residual field rotation.

The rates fed to `solve` here are not produced by its own formula: they come
from turning the Earth and the mount and reading the angle off a simulated
sensor, which is what makes the test able to catch a wrong sign — and what
caught the frame the maths has to live in.

Everything is done in the hour-angle frame, where the platform's axis stands
still: `_axis` is a mechanical axis, fixed against the ground, and the stars
are what move past it.
"""
import numpy as np
import pytest

from astrodoro.core import platform_align as pa
from astrodoro.core import polar
from astrodoro.core.catalog import radec_to_vec
from astrodoro.core.equatorial import fit_rate
from astrodoro.core.polar import rotate_about
from astrodoro.core.tonight import lst_at

LAT, LON = -6.7003, -36.9436
WHEN = "2026-09-05T02:00:00"
Z = np.array([0.0, 0.0, 1.0])


def _tangent(u):
    a = np.cross(Z, u)
    a /= np.linalg.norm(a)
    return a, np.cross(u, a)


def _axis(x_off_deg: float, y_off_deg: float) -> np.ndarray:
    """A platform axis missing the pole by a known amount, in the hour-angle
    frame: `x_off` tilts it towards the meridian, `y_off` at right angles."""
    p = Z + np.array([np.deg2rad(x_off_deg), np.deg2rad(y_off_deg), 0.0])
    return p / np.linalg.norm(p)


def _sensor_rate(ra_ground: float, dec: float, axis: np.ndarray,
                 minutes: float = 10.0, n: int = 25) -> float:
    """Field rotation an ideal sensor would report, in degrees per minute.

    `ra_ground` is the target's right ascension minus the sidereal time at the
    start of the run: its place in the frame the platform is bolted to. The
    sensor's axes are carried round by the Earth and carried back by the
    platform, so in this frame it is the stars that turn, and the angle of a
    pair of them measured against those axes is what registration reports.

    The basis is right-handed with the line of sight, which is what makes the
    module's `parity = 1` the unmirrored case.
    """
    u = radec_to_vec(np.array([ra_ground]), np.array([dec]))[0]
    a, b = _tangent(u)
    s = 0.01 * (b - a)                      # separation of two nearby stars
    t = np.linspace(0.0, minutes, n)
    ang = []
    for minute in t:
        w = pa.SIDEREAL_DEG_PER_MIN * minute
        ax = rotate_about(rotate_about(a, Z, -w), axis, +w)
        ay = rotate_about(rotate_about(b, Z, -w), axis, +w)
        ang.append(np.degrees(np.arctan2(s @ ay, s @ ax)))
    return fit_rate(list(t * 60.0), list(np.unwrap(ang, period=360.0))).rate


def _station(ra, dec, axis, minutes=10.0, lst=0.0):
    """A station on the target at `ra`, measured from sidereal time `lst`.

    The rate a run reports is the average over its span, so the sidereal time
    the station carries is the one at its middle — the whole reason the solver
    works against the ground and not against the stars.
    """
    mid = pa.SIDEREAL_DEG_PER_MIN * minutes / 2.0
    return pa.Station(ra=ra, dec=dec, lst_deg=lst + mid, when=0.0,
                      rate_deg_min=_sensor_rate(ra - lst, dec, axis, minutes),
                      r2=1.0, n=25, span_s=minutes * 60.0)


def _solve(stations, lst=0.0, parity=1):
    return pa.solve(stations, LAT, LON, lst_deg=lst, when=WHEN, parity=parity)


def test_a_perfectly_aligned_platform_rotates_nothing():
    for ha, dec in ((0.0, 0.0), (90.0, -30.0), (200.0, 40.0)):
        assert abs(_sensor_rate(ha, dec, Z)) < 1e-9


def test_the_rotation_rate_follows_the_line_of_sight():
    """The signal is the projection of the error on the line of sight: it peaks
    where the axis error points and all but vanishes 90° away from it."""
    axis = _axis(1.0, 0.0)
    peak = abs(_sensor_rate(0.0, 0.0, axis))
    null = abs(_sensor_rate(90.0, 0.0, axis))
    assert peak == pytest.approx(pa.SIDEREAL_DEG_PER_MIN * np.deg2rad(1.0),
                                 rel=0.01)
    assert null < peak / 20


def test_the_rate_is_read_at_the_middle_of_the_run():
    """The rate is an average over the span, and the field's rotation is not
    quite constant: reading it at the start instead biases a ten-minute run by
    3% and a forty-minute one by 13%."""
    axis = _axis(0.6, 0.9)
    eps = axis - Z
    u = radec_to_vec(np.array([0.0]), np.array([0.0]))[0]
    at_start = -pa.SIDEREAL_DEG_PER_MIN * (eps @ u)
    for minutes, bias in ((10.0, 0.03), (40.0, 0.13)):
        measured = _sensor_rate(0.0, 0.0, axis, minutes=minutes)
        assert abs(measured / at_start - 1.0) == pytest.approx(bias, abs=0.01)
        mid = rotate_about(u, Z, -pa.SIDEREAL_DEG_PER_MIN * minutes / 2.0)
        at_middle = -pa.SIDEREAL_DEG_PER_MIN * (eps @ mid)
        assert measured == pytest.approx(at_middle, rel=0.005)


def test_two_targets_recover_a_known_error():
    axis = _axis(0.8, -0.5)
    r = _solve([_station(10.0, 0.0, axis), _station(100.0, -10.0, axis)])
    assert r is not None and r.confident
    assert r.total_error_deg == pytest.approx(np.hypot(0.8, 0.5), rel=0.01)


def test_the_correction_matches_the_axis_the_platform_really_has():
    """The two screws the solver asks for must be the two the real axis is
    away from, which `polar` computes independently from that axis."""
    lst = lst_at(LON, WHEN)
    axis = _axis(0.6, 0.9)
    r = _solve([_station(30.0, 10.0, axis), _station(120.0, -20.0, axis)],
               lst=lst)
    truth = polar.correction(-pa._rotate_z(axis, lst), LAT, LON, WHEN)
    assert r.alt_error_deg == pytest.approx(truth.alt_error_deg, abs=0.01)
    assert r.az_error_deg == pytest.approx(truth.az_error_deg, abs=0.01)


def test_the_parity_of_the_optical_train_flips_the_answer():
    axis = _axis(0.8, -0.5)
    stations = [_station(10.0, 0.0, axis), _station(100.0, -10.0, axis)]
    right = _solve(stations)
    wrong = _solve(stations, parity=-1)
    assert wrong.alt_error_deg == pytest.approx(-right.alt_error_deg, rel=0.01)
    assert wrong.az_error_deg == pytest.approx(-right.az_error_deg, rel=0.01)


def test_one_target_alone_says_so():
    r = _solve([_station(45.0, 0.0, _axis(1.0, 0.4))])
    assert not r.confident
    assert r.n_stations == 1 and r.note


def test_two_targets_in_the_same_direction_are_refused():
    axis = _axis(1.0, 0.4)
    r = _solve([_station(30.0, 0.0, axis), _station(33.0, 2.0, axis)])
    assert r.condition > pa.MAX_CONDITION
    assert not r.confident and r.note


def test_the_next_target_closes_the_direction_still_missing():
    """One station leaves half the error unmeasured. What `ideal_next` points
    at has to be the half that makes the pair solvable."""
    axis = _axis(1.0, 0.0)
    lst = 40.0
    first = _station(0.0, -10.0, axis, lst=lst)
    assert not _solve([first], lst=lst).confident

    ra, dec, alt, _az = pa.ideal_next([first], LAT, lst_deg=lst)
    assert alt >= 25.0
    second = _station(ra, dec, axis, lst=lst)
    assert _solve([first, second], lst=lst).confident


def test_a_backwards_correction_is_reported_as_a_parity_problem():
    def st(rate):
        return pa.Station(ra=0.0, dec=0.0, lst_deg=0.0, when=0.0,
                          rate_deg_min=rate, r2=0.9, n=30, span_s=600.0)

    v = pa.verify(st(0.004), st(0.008))
    assert v["worse"] and v["parity_suspect"]
    assert pa.verify(st(0.004), st(0.0005))["improved"]
