from __future__ import annotations

import numpy as np

from astrodoro.core.stacker import LiveStacker
from astrodoro.core.stars import StarField, detect

H = W = 700


def _gauss(img, x, y, a, sig=1.5):
    r = int(sig * 4 + 1)
    x0, y0 = int(round(x)), int(round(y))
    yy, xx = np.mgrid[y0 - r : y0 + r + 1, x0 - r : x0 + r + 1]
    m = (yy >= 0) & (yy < img.shape[0]) & (xx >= 0) & (xx < img.shape[1])
    g = a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sig**2))
    img[yy[m], xx[m]] += g[m]


def field(
    smear: float = 0.0, blobs: int = 8, seed: int = 3, noise: int = 0
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    xs = rng.uniform(0.22 * W, 0.78 * W, 45)
    ys = rng.uniform(0.22 * H, 0.78 * H, 45)
    amps = rng.uniform(80, 500, 45)
    img = (
        np.random.default_rng(1000 + noise).normal(20.0, 1.0, (H, W)).astype(np.float32)
    )
    dirs = rng.uniform(-1, 1, (blobs, 2))
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    radii = np.linspace(11.0, 24.0, blobs)
    for x, y, a in zip(xs, ys, amps, strict=True):
        _gauss(img, x, y, a)
        if smear:
            for (dx, dy), r in zip(dirs, radii, strict=True):
                _gauss(img, x + dx * r, y + dy * r, a * smear)
    return img


def test_old_metrics_are_blind_to_spread_flux():
    clean = detect(field(0.0), scale=2.0, max_stars=60, central=0.70)
    bad = detect(field(0.40), scale=2.0, max_stars=60, central=0.70)

    assert abs(bad.median_elongation - clean.median_elongation) < 0.05, (
        f"elongation {clean.median_elongation:.2f} -> {bad.median_elongation:.2f}"
    )
    assert bad.median_elongation < 1.4, "would pass even at the tightest setting"
    assert abs(bad.median_fwhm / clean.median_fwhm - 1) < 0.05
    assert abs(bad.median_hfr / clean.median_hfr - 1) < 0.05

    assert clean.median_halo < 1.2, f"halo floor at {clean.median_halo:.2f}"
    assert bad.median_halo > 3.0, f"smear halo only {bad.median_halo:.2f}"


def _add(st: LiveStacker, lum: np.ndarray):
    rgb = np.zeros((2 * H, 2 * W, 3), np.float32)
    return st.add(rgb, lum, exposure=5.0, lum_scale=2.0)


def _a_night(st: LiveStacker, n: int = 7):
    for k in range(n):
        o = _add(st, field(0.0, noise=k))
        assert o.accepted, f"clean frame {k} refused: {o.reason}"


def test_stacker_rejects_and_names_the_dominant_defect():
    st = LiveStacker((2 * H, 2 * W), channels=3)
    st.set_strictness("normal")
    _a_night(st)

    o = _add(st, field(0.40))
    assert not o.accepted, "the frame with spread flux was accepted"
    assert o.kind == "smear", f"blamed {o.kind}, and the defect is the smear"
    assert "spread out" in o.reason and "worth" in o.reason
    assert st.rejections["smear"] == 1

    st2 = LiveStacker((2 * H, 2 * W), channels=3, min_weight=0.0)
    _a_night(st2)
    o = _add(st2, field(0.40))
    assert o.accepted, f"without a weight floor it should pass: {o.reason}"


def test_a_uniform_night_discards_nothing():
    st = LiveStacker((2 * H, 2 * W), channels=3)
    st.set_strictness("strict")
    for k in range(12):
        o = _add(st, field(0.0, noise=50 + k))
        assert o.accepted, f"frame {k} of a uniform night fell: {o.reason}"
    assert st.n_stacked == 12 and st.n_rejected == 0


def test_the_fwhm_ruler_only_moves_on_an_accepted_frame():
    st = LiveStacker((2 * H, 2 * W), channels=3)
    st.set_strictness("normal")
    _a_night(st)
    ruler = st.best_fwhm
    assert np.isfinite(ruler)

    o = st.add(
        np.zeros((2 * H, 2 * W, 3), np.float32),
        field(0.40, noise=7),
        exposure=5.0,
        lum_scale=2.0,
    )
    assert not o.accepted and o.kind == "smear"
    assert st.best_fwhm == ruler, (
        f"a rejected frame moved the ruler: {ruler:.2f} -> {st.best_fwhm:.2f}"
    )

    st2 = LiveStacker((2 * H, 2 * W), channels=3, min_ref_stars=1)
    few = field(0.0)
    o = _add(st2, few)
    assert o.accepted
    st2.min_ref_stars = o.n_stars + 1
    st2.best_fwhm = float("inf")
    _add(st2, few)
    assert st2.best_fwhm == float("inf"), "a frame with no material set the ruler"


def _fake_field(halo: float, fwhm: float = 4.0) -> StarField:
    n = 20
    return StarField(
        xy=np.zeros((n, 2)),
        flux=np.full(n, 1000.0),
        fwhm=np.full(n, fwhm),
        background=10.0,
        noise=2.0,
        halo=np.full(n, halo),
    )


def test_halo_penalises_the_weight_but_with_a_deadband():
    st = LiveStacker((10, 10), channels=3)
    base = st.frame_score(_fake_field(1.0))

    for h in (0.9, 1.2, 1.5):
        assert st.frame_score(_fake_field(h)) == base, f"halo {h} penalised"

    mid = st.frame_score(_fake_field(3.0))
    assert abs(mid / base - 1 / 2.0) < 1e-6, f"halo 3.0 gave {mid / base:.3f} of base"
    strong = st.frame_score(_fake_field(6.0))
    assert abs(strong / base - 1 / 4.0) < 1e-6

    assert st.frame_score(_fake_field(5.1, fwhm=4.0)) < st.frame_score(
        _fake_field(1.2, fwhm=6.0)
    )


def test_strictness_is_a_single_axis():
    values = [
        LiveStacker.STRICTNESS[n]["min_weight"] for n in ("lenient", "normal", "strict")
    ]
    assert values == sorted(values), f"minimum weight out of order: {values}"
    assert all(0.0 < x < 1.0 for x in values)
    for name, d in LiveStacker.STRICTNESS.items():
        assert set(d) <= {"min_weight", "max_background_jump"}, (
            f"{name} touches a threshold that is not part of strictness: {sorted(d)}"
        )
