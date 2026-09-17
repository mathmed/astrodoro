from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest

from astrodoro.core import tonight
from astrodoro.core.catalog import Obj
from astrodoro.core.lucky import BodyState
from astrodoro.i18n import gettext as _

LAT = -6.7003
LON = -36.9436


def obj(
    name="NGC0001",
    kind="G",
    ra=0.0,
    dec=0.0,
    major=10.0,
    minor=6.0,
    mag=9.0,
    messier="",
    common="",
) -> Obj:
    return Obj(
        name=name,
        kind=kind,
        ra=ra,
        dec=dec,
        major_arcmin=major,
        minor_arcmin=minor,
        mag=mag,
        messier=messier,
        common=common,
    )


def sky(
    lst=0.0, sun_alt=-30.0, moon_alt=-20.0, moon_ra=0.0, moon_dec=0.0, moon_illum=0.0
) -> tonight.Sky:
    return tonight.Sky(
        when=datetime(2026, 8, 25, 23, 0, tzinfo=UTC),
        lst_deg=lst,
        sun_alt=sun_alt,
        moon_alt=moon_alt,
        moon_ra=moon_ra,
        moon_dec=moon_dec,
        moon_illum=moon_illum,
    )


def test_the_hour_angle_formula_agrees_with_astropy():
    from astrodoro.pointing.model import sky_vectors

    when = datetime(2026, 8, 25, 23, 30, tzinfo=UTC)
    s = tonight.sky_at(LAT, LON, when)
    ra = np.array([270.9, 201.36, 83.82, 10.68, 45.0])
    dec = np.array([-24.38, -43.02, -5.39, 41.27, -70.0])

    alt, az, _ha = tonight.horizon(ra, dec, LAT, s.lst_deg)
    v = sky_vectors(ra, dec, LAT, LON, when=when)
    alt_ref = np.degrees(np.arcsin(np.clip(v[:, 2], -1, 1)))
    az_ref = np.degrees(np.arctan2(v[:, 0], v[:, 1])) % 360.0

    assert np.max(np.abs(alt - alt_ref)) < 0.5
    high = alt_ref < 85
    d_az = np.abs(((az - az_ref + 180) % 360) - 180)[high]
    assert np.max(d_az) < 1.0


def test_an_object_below_the_minimum_altitude_is_not_suggested():
    s = sky(lst=0.0)
    low = obj(name="LOW", dec=75.0, ra=180.0)
    high = obj(name="HIGH", dec=LAT, ra=0.0)
    out = tonight.rank([low, high], s, LAT, min_alt=25.0)
    assert [x.obj.name for x in out] == ["HIGH"]


def test_the_meridian_object_reports_no_time_to_transit():
    s = sky(lst=120.0)
    out = tonight.rank([obj(ra=120.0, dec=LAT)], s, LAT)
    assert abs(out[0].minutes_to_transit) < 1.0
    assert out[0].alt > 89.0


def test_a_full_moon_nearby_costs_a_galaxy_more_than_a_cluster():
    s = sky(lst=0.0, moon_alt=60.0, moon_ra=5.0, moon_dec=LAT, moon_illum=1.0)
    galaxy = obj(name="G", kind="G", ra=0.0, dec=LAT)
    cluster = obj(name="OC", kind="OCl", ra=0.0, dec=LAT)
    out = {x.obj.name: x for x in tonight.rank([galaxy, cluster], s, LAT)}
    assert out["G"].factors["moon"] < out["OC"].factors["moon"]
    assert out["G"].moon_sep < 10.0

    dark = tonight.rank([galaxy, cluster], sky(lst=0.0), LAT)
    assert all(x.factors["moon"] == 1.0 for x in dark)


def test_the_moon_far_away_costs_almost_nothing():
    s = sky(lst=0.0, moon_alt=60.0, moon_ra=180.0, moon_dec=-LAT, moon_illum=1.0)
    out = tonight.rank([obj(kind="G", ra=0.0, dec=LAT)], s, LAT)
    assert out[0].moon_sep > 120.0
    assert out[0].factors["moon"] == 1.0


def test_an_object_about_to_set_loses_to_an_identical_one_still_up():
    s = sky(lst=0.0)
    setting = obj(name="SETTING", ra=(0.0 - 62.0) % 360.0, dec=LAT)
    rising = obj(name="RISING", ra=(0.0 + 30.0) % 360.0, dec=LAT)
    out = {x.obj.name: x for x in tonight.rank([setting, rising], s, LAT, min_alt=25.0)}
    assert out["SETTING"].minutes_left < out["RISING"].minutes_left
    assert out["SETTING"].score < out["RISING"].score
    assert out["RISING"].rising and not out["SETTING"].rising


def test_a_circumpolar_object_has_no_end_of_window():
    s = sky(lst=0.0)
    out = tonight.rank([obj(ra=0.0, dec=-89.0)], s, LAT, min_alt=5.0)
    assert np.isinf(out[0].minutes_left)
    assert any("all night" in r for r in out[0].reasons())


def test_the_family_filter_only_lets_its_own_types_through():
    s = sky(lst=0.0)
    objs = [
        obj(name="G", kind="G", ra=0.0, dec=LAT),
        obj(name="PN", kind="PN", ra=0.0, dec=LAT),
        obj(name="OC", kind="OCl", ra=0.0, dec=LAT),
    ]
    assert [x.obj.name for x in tonight.rank(objs, s, LAT, family="planetary")] == [
        "PN"
    ]
    assert len(tonight.rank(objs, s, LAT, family="all")) == 3


def test_an_object_larger_than_the_frame_is_demoted_but_kept():
    s = sky(lst=0.0)
    huge = obj(name="HUGE", ra=0.0, dec=LAT, major=180.0, minor=120.0, mag=5.0)
    fits = obj(name="FITS", ra=0.0, dec=LAT, major=20.0, minor=14.0, mag=9.0)
    names = [
        x.obj.name for x in tonight.rank([huge, fits], s, LAT, fov_arcmin=(55.0, 37.0))
    ]
    assert names == ["FITS", "HUGE"]
    assert "HUGE" not in [
        x.obj.name for x in tonight.rank([huge, fits], s, LAT, fits_only=True)
    ]


def test_the_magnitude_filter_excludes_the_unmeasured():
    s = sky(lst=0.0)
    objs = [
        obj(name="KNOWN", ra=0.0, dec=LAT, mag=9.0),
        obj(name="UNKNOWN", ra=0.0, dec=LAT, mag=float("nan")),
    ]
    assert [x.obj.name for x in tonight.rank(objs, s, LAT)] == ["KNOWN"]


def test_a_messier_outranks_an_identical_anonymous_object():
    s = sky(lst=0.0)
    m = obj(name="NGC6523", ra=0.0, dec=LAT, messier="8", common="Lagoon")
    anon = obj(name="NGC9999", ra=0.0, dec=LAT)
    out = tonight.rank([anon, m], s, LAT)
    assert [x.obj.name for x in out] == ["NGC6523", "NGC9999"]


def test_surface_brightness_matches_the_catalogue_value_for_m31():
    sb = tonight._surface_brightness(
        np.array([3.4]), np.array([190.0]), np.array([60.0])
    )
    assert abs(float(sb[0]) - 22.2) < 0.2


def body(key="jupiter", ra=0.0, dec=LAT, diameter_arcmin=0.75) -> BodyState:
    return BodyState(
        body=key,
        when=datetime(2026, 8, 25, 23, 0, tzinfo=UTC),
        alt=90.0 - abs(LAT - dec),
        az=0.0,
        ra=ra,
        dec=dec,
        illum=1.0,
        waxing=False,
        diameter_arcmin=diameter_arcmin,
        distance_km=6e8,
    )


def test_a_planet_is_ranked_into_the_same_list_as_the_catalogue():
    s = sky(lst=0.0)
    out = tonight.rank(
        [obj(name="NGC0001")], s, LAT, bodies=[body()], arcsec_per_px=1.53
    )
    assert {x.obj.name for x in out} == {"NGC0001", _("Jupiter")}
    planet = next(x for x in out if x.body)
    assert planet.body == "jupiter"
    assert planet.obj.kind == "Planet"


def test_the_solar_family_is_the_bodies_and_nothing_else():
    s = sky(lst=0.0)
    objs = [obj(name="NGC0001", kind="G", ra=0.0, dec=LAT)]
    solar = tonight.rank(
        objs, s, LAT, bodies=[body()], arcsec_per_px=1.53, family="solar"
    )
    assert [x.body for x in solar] == ["jupiter"]
    assert not any(
        x.body
        for x in tonight.rank(
            objs, s, LAT, bodies=[body()], arcsec_per_px=1.53, family="galaxy"
        )
    )


def test_moonlight_costs_a_body_nothing():
    s = sky(lst=0.0, moon_alt=60.0, moon_ra=0.0, moon_dec=LAT, moon_illum=1.0)
    out = tonight.rank(
        [obj(kind="G", ra=0.0, dec=LAT)],
        s,
        LAT,
        bodies=[body(), body("moon", diameter_arcmin=31.0)],
        arcsec_per_px=1.53,
    )
    galaxy = next(x for x in out if not x.body)
    assert galaxy.factors["moon"] < 0.5
    for b in (x for x in out if x.body):
        assert b.factors["moon"] == 1.0
        assert np.isnan(b.moon_sep)


def test_a_disc_the_scale_cannot_resolve_loses_to_one_it_can():
    s = sky(lst=0.0)
    out = tonight.rank(
        [],
        s,
        LAT,
        arcsec_per_px=1.53,
        bodies=[
            body("neptune", diameter_arcmin=2.3 / 60),
            body("jupiter", diameter_arcmin=45.0 / 60),
        ],
    )
    assert [x.body for x in out] == ["jupiter", "neptune"]
    assert out[0].factors["size"] > 3 * out[1].factors["size"]


def test_a_body_below_the_minimum_altitude_is_not_suggested():
    s = sky(lst=0.0)
    assert (
        tonight.rank(
            [], s, LAT, min_alt=25.0, bodies=[body(dec=LAT - 70.0)], arcsec_per_px=1.53
        )
        == []
    )


def test_the_twilight_text_names_the_stage_of_the_night():
    assert "Sun is up" in sky(sun_alt=5.0).twilight_text()
    assert not sky(sun_alt=-10.0).dark
    assert sky(sun_alt=-20.0).dark
    assert sky(sun_alt=-20.0).twilight_text() == ""


@pytest.mark.parametrize(
    "az,expected", [(0, "N"), (90, "E"), (180, "S"), (270, "W"), (359, "N")]
)
def test_the_compass_points_the_way_the_map_does(az, expected):
    assert tonight.compass_point(az) == expected
