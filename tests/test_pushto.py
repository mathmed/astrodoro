"""Following the arrow has to bring you closer to the target.

The path sensor -> alt/az -> RA/Dec -> `pushto.guide` -> arrow crosses four
convention changes, and each one is a chance to flip a sign. A flipped sign
raises no error: the arrow points confidently the wrong way and the target runs
away. This test closes the loop — it pushes the tube in the indicated direction
and requires the distance to fall.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.optimize import least_squares

from astrodoro.pointing import brightstars as bs
from astrodoro.pointing import orientation as o
from astrodoro.pointing.model import Pointing
from astrodoro.pointing.pushto import guide

LAT, LON = -6.7003, -36.9436
#: Phone mounted crooked on the tube: the defect the alignment must absorb.
MOUNT = o.normalize(np.array([0.045, 1.0, 0.018]))


def _sensor(alt, az):
    """The alpha/beta the device would report with the tube on (alt, az)."""
    target = o.altaz_to_enu(alt, az)

    def residual(x):
        return o.normalize(
            o.rotation_matrix(x[0], x[1], 0.0) @ MOUNT) - target

    x = least_squares(residual, [(360.0 - az) % 360.0, alt], method="lm").x
    return float(x[0]), float(x[1]), 0.0


def _point_at(p, alt, az, t0):
    for i in range(15):
        p.feed(*_sensor(alt, az), None, t0 + i * 0.05)


def test_following_the_arrow_converges():
    """Align on a star and walk to a target ~10 degrees away, which is the
    normal use: a one-star correction holds well in its neighbourhood."""
    visible = bs.visible(LAT, LON, min_alt=30.0)
    if len(visible) < 6:
        pytest.skip("the sky is too empty right now for this test")
    ref = visible[0]
    # A target near the alignment star, but not on top of it.
    candidates = [v for v in visible[1:]
                  if 4.0 < o.separation(o.altaz_to_enu(v[1], v[2]),
                                        o.altaz_to_enu(ref[1], ref[2])) < 25.0]
    if not candidates:
        pytest.skip("no star at a useful distance right now")
    target = min(candidates, key=lambda v: v[0].mag)[0]

    p = Pointing(LAT, LON)
    _point_at(p, ref[1], ref[2], 0.0)
    p.align_on(ref[0])
    assert p.aligned

    alt, az = ref[1], ref[2]
    distances = []
    for k in range(6):
        _point_at(p, alt, az, 20 + k * 5)
        g = guide(p.radec, (target.ra, target.dec), LAT, LON, fov_deg=0.9)
        distances.append(g.separation_deg)
        # Push the tube the way the screen said, with a human hand (80% of the
        # request, which is how a Dobsonian actually gets pushed).
        alt += g.delta_alt_deg * 0.8
        az += g.delta_az_deg * 0.8

    assert distances[1] < distances[0], \
        f"the arrow moved away from the target: {distances[:2]}"
    # At 25 degrees from the alignment star, a 2.6-degree crooked mount leaves
    # tens of arcminutes — what is required is landing inside the field, not on
    # the pixel.
    assert distances[-1] * 60 < 45.0, \
        f"did not converge: {distances[-1]*60:.1f}' at the end"


def test_without_an_alignment_no_position_is_invented():
    p = Pointing(LAT, LON)
    assert p.altaz is None and p.radec is None
    _point_at(p, 40.0, 120.0, 0.0)
    assert not p.aligned
    assert p.altaz is not None          # altitude already valid: from gravity
