from __future__ import annotations

from astrodoro.pointing.orientation import (
    Alignment,
    Smoother,
    altaz_to_enu,
    bearing,
    camera_rays,
    enu_to_altaz,
    separation,
)


def test_altaz_roundtrip():
    for alt, az in [(0, 0), (37.5, 212.0), (-12.0, 359.9), (89.0, 45.0)]:
        a, z = enu_to_altaz(altaz_to_enu(alt, az))
        assert abs(a - alt) < 1e-9, (a, alt)
        assert abs((z - az + 180) % 360 - 180) < 1e-7, (z, az)


def test_the_sighting_axis_is_the_top_of_the_device():
    _top, _left, fwd = camera_rays(0.0, 0.0, 0.0)
    alt, az = enu_to_altaz(fwd)
    assert abs(alt) < 1e-9 and abs(az) < 1e-7, (alt, az)
    _top, _left, fwd = camera_rays(0.0, 90.0, 0.0)
    alt, _az = enu_to_altaz(fwd)
    assert abs(alt - 90.0) < 1e-7, alt


def test_alignment_zeroes_the_error():
    for alpha, beta, gamma in [
        (0.0, 45.0, 0.0),
        (137.0, 20.0, 25.0),
        (300.0, 70.0, -40.0),
    ]:
        _top, left, fwd = camera_rays(alpha, beta, gamma)
        alt, az = enu_to_altaz(fwd)
        star = altaz_to_enu(alt + 3.0, az + 7.0)
        al = Alignment.solve(fwd, left, star, "test")
        _t, _l, corrected = camera_rays(alpha, beta, gamma, al)
        assert separation(corrected, star) < 1e-6, separation(corrected, star)
        assert abs(al.delta_alt_deg - 3.0) < 1e-6, al.delta_alt_deg
        assert al.error_deg > 3.0


def test_the_correction_does_not_double_the_error():
    _top, left, fwd = camera_rays(90.0, 35.0, 0.0)
    alt, az = enu_to_altaz(fwd)
    al = Alignment.solve(fwd, left, altaz_to_enu(alt + 2.0, az + 6.0))
    _t2, _l2, f2 = camera_rays(95.0, 35.0, 0.0)
    _t3, _l3, c2 = camera_rays(95.0, 35.0, 0.0, al)
    assert separation(f2, altaz_to_enu(*enu_to_altaz(f2))) < 1e-9
    raw_error = separation(f2, al.apply(f2))
    assert 5.0 < raw_error < 8.0, raw_error
    assert separation(c2, al.apply(f2)) < 1e-9


def test_alignment_with_a_large_error():
    for az_error in (30.0, 95.0, 150.0, -170.0):
        _top, left, fwd = camera_rays(0.0, 40.0, 0.0)
        alt, az = enu_to_altaz(fwd)
        star = altaz_to_enu(alt - 1.0, az + az_error)
        al = Alignment.solve(fwd, left, star)
        _t, _l, corrected = camera_rays(0.0, 40.0, 0.0, al)
        assert separation(corrected, star) < 1e-6, (
            az_error,
            separation(corrected, star),
        )


def test_bearing_right_and_up():
    rays = camera_rays(0.0, 45.0, 0.0)
    _top, _left, fwd = rays
    alt, az = enu_to_altaz(fwd)
    x, _y, z = bearing(altaz_to_enu(alt, az + 2.0), rays)
    assert z > 0.99, z
    assert x > 0, x
    _x, y, _z = bearing(altaz_to_enu(alt + 2.0, az), rays)
    assert y > 0, y


def test_smoothing_does_not_jump_at_north():
    s = Smoother(tau=0.1)
    s.update(altaz_to_enu(10.0, 359.0), 0.0)
    v = s.update(altaz_to_enu(10.0, 1.0), 0.05)
    _alt, az = enu_to_altaz(v)
    assert az > 358.0 or az < 2.0, az
