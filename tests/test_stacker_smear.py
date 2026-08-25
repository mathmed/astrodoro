"""A frame with spread-out flux: the smear that elongation cannot see.

The real case that motivated the filter had the tube sitting still for part of a
5 s exposure and wandering for the rest, so each star kept a **point-like core**
with a faint trail around it. Median elongation 1.26, FWHM 4.0 px, HFR 2.0 px —
better than the session average — which is why the frame was accepted at all
three strictness levels, while frames with 4x less spread flux were rejected on
elongation.

Here that is reproduced in a synthetic field: the same field, with and without
the trail. The test is that the old metrics do not move and the halo does.
"""
from __future__ import annotations

import numpy as np

from astrodoro.core.stacker import LiveStacker
from astrodoro.core.stars import StarField, detect

H = W = 700          # luminance; the halo's background annulus needs room


def _gauss(img, x, y, a, sig=1.5):
    r = int(sig * 4 + 1)
    x0, y0 = int(round(x)), int(round(y))
    yy, xx = np.mgrid[y0 - r:y0 + r + 1, x0 - r:x0 + r + 1]
    m = (yy >= 0) & (yy < img.shape[0]) & (xx >= 0) & (xx < img.shape[1])
    g = a * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sig ** 2))
    img[yy[m], xx[m]] += g[m]


def field(smear: float = 0.0, blobs: int = 8, seed: int = 3,
          noise: int = 0) -> np.ndarray:
    """A star field; `smear` is the fraction of the peak going to each trail blob.

    The blobs sit 11-24 px from the core: close enough to fall inside the halo
    aperture, far enough not to merge with the core — which is exactly the
    geometry of the real frame.

    `noise` changes only the noise, keeping the same sky: that is how a sequence
    of frames from one session is simulated. With the stars elsewhere the
    registration would fail and the test would measure the wrong thing.
    """
    rng = np.random.default_rng(seed)
    xs = rng.uniform(0.22 * W, 0.78 * W, 45)
    ys = rng.uniform(0.22 * H, 0.78 * H, 45)
    amps = rng.uniform(80, 500, 45)
    img = np.random.default_rng(1000 + noise).normal(
        20.0, 1.0, (H, W)).astype(np.float32)
    # The same path for every star: it is the tube that moves, not the sky.
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

    # Elongation, FWHM and HFR barely move — that is the point of the exercise.
    assert abs(bad.median_elongation - clean.median_elongation) < 0.05, \
        f"elongation {clean.median_elongation:.2f} -> {bad.median_elongation:.2f}"
    assert bad.median_elongation < 1.4, "would pass even at the tightest setting"
    assert abs(bad.median_fwhm / clean.median_fwhm - 1) < 0.05
    assert abs(bad.median_hfr / clean.median_hfr - 1) < 0.05

    # The halo does move: a perfect field sits near 1 (all flux in the core).
    assert clean.median_halo < 1.2, f"halo floor at {clean.median_halo:.2f}"
    assert bad.median_halo > 3.0, f"smear halo only {bad.median_halo:.2f}"


def _add(st: LiveStacker, lum: np.ndarray):
    rgb = np.zeros((2 * H, 2 * W, 3), np.float32)
    return st.add(rgb, lum, exposure=5.0, lum_scale=2.0)


def _a_night(st: LiveStacker, n: int = 7):
    """Feed n clean frames: the decision is relative to what the night delivers,
    so the night has to be seen before anything can be judged."""
    for k in range(n):
        o = _add(st, field(0.0, noise=k))
        assert o.accepted, f"clean frame {k} refused: {o.reason}"


def test_stacker_rejects_and_names_the_dominant_defect():
    st = LiveStacker((2 * H, 2 * W), channels=3)
    st.set_strictness("normal")
    _a_night(st)

    o = _add(st, field(0.40))
    assert not o.accepted, "the frame with spread flux was accepted"
    # The decision is by weight, but the reason has to tell you what to do.
    assert o.kind == "smear", f"blamed {o.kind}, and the defect is the smear"
    assert "spread out" in o.reason and "worth" in o.reason
    assert st.rejections["smear"] == 1

    # With the minimum weight at zero the SAME frame gets in: proof that the
    # score rejected it, not an absolute limit or the registration.
    st2 = LiveStacker((2 * H, 2 * W), channels=3, min_weight=0.0)
    _a_night(st2)
    o = _add(st2, field(0.40))
    assert o.accepted, f"without a weight floor it should pass: {o.reason}"


def test_a_uniform_night_discards_nothing():
    """The ruler is relative but it is not a percentile: if the whole night is
    the same, nobody is the worst. A cut by fraction would discard good frames."""
    st = LiveStacker((2 * H, 2 * W), channels=3)
    st.set_strictness("strict")
    for k in range(12):
        o = _add(st, field(0.0, noise=50 + k))
        assert o.accepted, f"frame {k} of a uniform night fell: {o.reason}"
    assert st.n_stacked == 12 and st.n_rejected == 0


def test_the_fwhm_ruler_only_moves_on_an_accepted_frame():
    """`best_fwhm` is monotonic: one bad measurement contaminates the session.

    The update used to sit next to the test, and a frame rejected later — by the
    reference, by registration, or here by the halo — still became the ruler for
    the ones that followed. Since a worse frame has fewer stars and a less
    stable median, the asymmetry ran the wrong way.
    """
    st = LiveStacker((2 * H, 2 * W), channels=3)
    st.set_strictness("normal")
    _a_night(st)
    ruler = st.best_fwhm
    assert np.isfinite(ruler)

    # Thinner stars (lower FWHM, so a tempting ruler) but with the flux spread
    # out: it is rejected, and it must not move the ruler.
    o = st.add(np.zeros((2 * H, 2 * W, 3), np.float32), field(0.40, noise=7),
               exposure=5.0, lum_scale=2.0)
    assert not o.accepted and o.kind == "smear"
    assert st.best_fwhm == ruler, \
        f"a rejected frame moved the ruler: {ruler:.2f} -> {st.best_fwhm:.2f}"

    # Accepted, but with too few stars for the measurement to mean anything:
    # also not.
    st2 = LiveStacker((2 * H, 2 * W), channels=3, min_ref_stars=1)
    few = field(0.0)
    o = _add(st2, few)
    assert o.accepted
    st2.min_ref_stars = o.n_stars + 1        # as if the field were poor
    st2.best_fwhm = float("inf")
    _add(st2, few)
    assert st2.best_fwhm == float("inf"), "a frame with no material set the ruler"


def _fake_field(halo: float, fwhm: float = 4.0) -> StarField:
    n = 20
    return StarField(xy=np.zeros((n, 2)), flux=np.full(n, 1000.0),
                     fwhm=np.full(n, fwhm), background=10.0, noise=2.0,
                     halo=np.full(n, halo))


def test_halo_penalises_the_weight_but_with_a_deadband():
    st = LiveStacker((10, 10), channels=3)
    base = st.frame_score(_fake_field(1.0))

    # Clean field: the halo oscillates between 0.9 and 1.4 on measurement noise
    # alone, and penalising there cost 4% of the SNR gain in the synthetic test.
    for h in (0.9, 1.2, 1.5):
        assert st.frame_score(_fake_field(h)) == base, f"halo {h} penalised"

    # Above the deadband the penalty is linear.
    mid = st.frame_score(_fake_field(3.0))
    assert abs(mid / base - 1 / 2.0) < 1e-6, f"halo 3.0 gave {mid/base:.3f} of base"
    strong = st.frame_score(_fake_field(6.0))
    assert abs(strong / base - 1 / 4.0) < 1e-6

    # And the defect must not pay better than a fatter clean frame: a thin core
    # (FWHM 4) with a trail loses to a clean FWHM 6.
    assert st.frame_score(_fake_field(5.1, fwhm=4.0)) < \
        st.frame_score(_fake_field(1.2, fwhm=6.0))


def test_strictness_is_a_single_axis():
    """Strictness moves the minimum weight and nothing else: that is what gives
    the selector a predictable meaning (more frames and more SNR on one side,
    fewer frames and more sharpness on the other)."""
    values = [LiveStacker.STRICTNESS[n]["min_weight"]
              for n in ("lenient", "normal", "strict")]
    assert values == sorted(values), f"minimum weight out of order: {values}"
    assert all(0.0 < x < 1.0 for x in values)
    for name, d in LiveStacker.STRICTNESS.items():
        assert set(d) <= {"min_weight", "max_background_jump"}, \
            f"{name} touches a threshold that is not part of strictness: {sorted(d)}"
