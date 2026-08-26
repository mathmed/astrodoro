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


def _point_at(p, alt, az, t0, compass_error=None, gyro_offset=0.0):
    """Feed 15 samples with the tube on (alt, az).

    `gyro_offset` imitates the relative `alpha` of a real handset, whose origin
    is wherever the page happened to load; `compass_error` is the magnetometer's
    own error next to a metal tube. Both default to the ideal device, which is
    what most of these tests want.
    """
    for i in range(15):
        a, b, g = _sensor(alt, az)
        compass = (None if compass_error is None
                   else (a + compass_error) % 360.0)
        p.feed((a - gyro_offset) % 360.0, b, g, compass, t0 + i * 0.05)


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


def test_the_compass_does_not_move_the_sky_after_aligning():
    """A phone with a magnetometer must not point worse than one without.

    On iOS the compass (`webkitCompassHeading`) and the orientation event's
    `alpha` share no origin: the difference between them is whatever the page
    happened to load at. While the compass *replaced* alpha instead of offsetting
    it, the alignment solved for one source and everything after it read the
    other, so the sky jumped by that whole difference the instant the alignment
    reported success — with no error message and no way to tell from the screen.
    A hundred degrees off is not "a bit out": nothing is ever found again.
    """
    visible = bs.visible(LAT, LON, min_alt=30.0)
    if len(visible) < 3:
        pytest.skip("the sky is too empty right now for this test")
    ref = visible[0]
    alt, az = ref[1] + 6.0, ref[2] + 9.0        # somewhere near the anchor

    ideal = Pointing(LAT, LON)
    _point_at(ideal, ref[1], ref[2], 0.0)
    ideal.align_on(ref[0])
    _point_at(ideal, alt, az, 20.0)

    # The same night through a handset with a compass 8 degrees out and an alpha
    # 130 degrees from north — an ordinary iPhone.
    phone = Pointing(LAT, LON)
    _point_at(phone, ref[1], ref[2], 0.0, compass_error=8.0, gyro_offset=130.0)
    phone.align_on(ref[0])
    _point_at(phone, alt, az, 20.0, compass_error=8.0, gyro_offset=130.0)

    assert phone.aligned and ideal.aligned
    drift = o.separation(o.altaz_to_enu(*phone.altaz),
                         o.altaz_to_enu(*ideal.altaz))
    assert drift < 0.05, f"the compass moved the sky by {drift:.1f} degrees"


def test_the_compass_still_positions_the_map_before_aligning():
    """The offset has to keep doing the job the substitution did: without it the
    map comes up rotated by the gyro's arbitrary origin and no star on screen is
    where the sky has it, which is what makes the first alignment possible."""
    p = Pointing(LAT, LON)
    _point_at(p, 40.0, 120.0, 0.0, compass_error=0.0, gyro_offset=130.0)
    _alt, az = p.altaz
    assert abs((az - 120.0 + 180) % 360 - 180) < 4.0, az   # 2.6 deg is the mount


def test_without_an_alignment_no_position_is_invented():
    p = Pointing(LAT, LON)
    assert p.altaz is None and p.radec is None
    _point_at(p, 40.0, 120.0, 0.0)
    assert not p.aligned
    assert p.altaz is not None          # altitude already valid: from gravity


# ------------------------------------------------------- choosing that star
def test_naming_a_target_pulls_the_suggestion_towards_it():
    """The dominant term is distance to the target: one star corrects two axes,
    so its accuracy is local — the test above measures exactly that decay.

    What is asserted is the *movement*, not a particular star: which stars are
    up depends on the hour the suite runs at."""
    from astrodoro.core.catalog import angular_sep

    blind = bs.for_alignment(LAT, LON, min_alt=20.0, limit=20)
    if len(blind) < 3:
        pytest.skip("fewer than three alignment stars up right now")

    # A target six degrees from the star the blind ranking liked least.
    outsider = blind[-1].star
    target = (outsider.ra, outsider.dec + 6.0)
    aimed = bs.for_alignment(LAT, LON, target=target, min_alt=20.0, limit=20)

    order_before = [p.star.label for p in blind]
    order_after = [p.star.label for p in aimed]
    assert order_after.index(outsider.label) < order_before.index(
        outsider.label), "naming a target beside it did not promote the star"

    # And the winner is now closer to the target than the blind winner was.
    was = angular_sep(blind[0].star.ra, blind[0].star.dec, *target)
    assert aimed[0].target_sep <= was


def test_a_star_low_or_at_the_zenith_is_not_suggested_first():
    picks = bs.for_alignment(LAT, LON, min_alt=25.0)
    if not picks:
        pytest.skip("no alignment star up right now")
    assert all(p.alt >= 25.0 for p in picks), "suggested a star below the floor"
    # The zenith penalty is the same 80 degrees push-to warns about.
    top = picks[0]
    assert top.alt < 88.0 or len(picks) == 1


def test_the_confusable_pair_loses_to_the_lone_star():
    """Aligning on the wrong star of a close pair produces a confident, wrong
    position — so isolation is a factor, not a nicety."""
    assert bs._f_isolation(0.5) < bs._f_isolation(3.0) < bs._f_isolation(12.0)
    assert bs._f_isolation(12.0) == 1.0


def test_brightness_only_breaks_ties():
    """Vega and Antares are both simply obvious; second magnitude and fainter is
    where it starts to cost something."""
    assert bs._f_mag(0.03) == bs._f_mag(1.0) == 1.0
    assert bs._f_mag(2.5) < bs._f_mag(1.5) < 1.0


def test_no_target_means_no_distance_term():
    picks = bs.for_alignment(LAT, LON, min_alt=20.0)
    if not picks:
        pytest.skip("no alignment star up right now")
    assert all(np.isnan(p.target_sep) for p in picks)
    assert bs._f_target(float("nan")) == 1.0
