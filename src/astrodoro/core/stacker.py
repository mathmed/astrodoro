from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from ..i18n import gettext as _
from . import register
from .stars import StarField, detect


@dataclass
class FrameOutcome:
    accepted: bool
    reason: str
    n_stars: int
    fwhm: float
    alignment: register.Alignment | None
    n_stacked: int
    elapsed_ms: float
    hfr: float = float("nan")
    brightest: tuple | None = None
    weight: float = 1.0
    score: float = float("nan")
    kind: str = "ok"
    elong: float = float("nan")
    halo: float = float("nan")
    limits: dict = field(default_factory=dict)


class LiveStacker:
    STRICTNESS: ClassVar[dict[str, dict[str, float]]] = {
        "lenient": {"min_weight": 0.20, "max_background_jump": 4.0},
        "normal": {"min_weight": 0.40, "max_background_jump": 2.5},
        "strict": {"min_weight": 0.60, "max_background_jump": 1.8},
    }

    HALO_DEADBAND = 1.5
    ELONGATION_DEADBAND = 1.3
    SCORE_WINDOW = 40

    def __init__(
        self,
        shape: tuple[int, int],
        channels: int = 3,
        ref_refresh: int = 10,
        min_matched: int = 8,
        min_matched_floor: int = 5,
        min_ref_stars: int = 10,
        max_rms: float = 2.0,
        fwhm_tolerance: float = 3.0,
        max_stars: int = 60,
        central: float = 0.70,
        saturation: float | None = None,
        sigma_clip: float | None = None,
        quality_weighting: bool = True,
        weight_floor: float = 0.05,
        min_weight: float = 0.40,
        max_elongation: float = 2.5,
        max_halo: float = 8.0,
        max_background_jump: float = 2.5,
        settle_shift_px: float = 40.0,
        settle_frames: int = 2,
        settle_strictness: float = 0.65,
    ):
        h, w = shape
        self.shape = (h, w)
        self.channels = channels
        self.accum = np.zeros((h, w, channels), dtype=np.float32)
        self.weight = np.zeros((h, w), dtype=np.float32)

        self.ref_refresh = ref_refresh
        self.min_matched = min_matched
        self.min_matched_floor = min_matched_floor
        self.min_ref_stars = min_ref_stars
        self._relax = 0
        self.max_rms = max_rms
        self.fwhm_tolerance = fwhm_tolerance
        self.max_stars = max_stars
        self.central = central
        self.saturation = saturation

        self.min_weight = min_weight
        self.max_elongation = max_elongation
        self.max_halo = max_halo
        self.max_background_jump = max_background_jump
        self.settle_shift_px = settle_shift_px
        self.settle_frames = settle_frames
        self.settle_strictness = settle_strictness
        self._bg_history: list[float] = []
        self._settle_left = 0
        self._last_shift: tuple[float, float] | None = None
        self.quality_weighting = quality_weighting
        self.weight_floor = weight_floor
        self._scores: deque = deque(maxlen=self.SCORE_WINDOW)
        self.sigma_clip = sigma_clip
        self._mean: np.ndarray | None = None
        self._m2: np.ndarray | None = None
        self._count: np.ndarray | None = None
        if sigma_clip:
            self._mean = np.zeros((h, w, channels), dtype=np.float32)
            self._m2 = np.zeros((h, w, channels), dtype=np.float32)
            self._count = np.zeros((h, w), dtype=np.float32)

        self.ref_stars: StarField | None = None
        self.n_stacked = 0
        self.n_rejected = 0
        self.best_fwhm = float("inf")
        self.total_exposure = 0.0
        self.weighted_exposure = 0.0
        self.history: list[FrameOutcome] = []
        self.rejections: dict[str, int] = {}
        self._since_ref = 0

    @property
    def started(self) -> bool:
        return self.n_stacked > 0

    def result(self) -> np.ndarray:
        w = np.maximum(self.weight, 1e-6)[..., None]
        out = self.accum / w
        peak = float(out.max())
        return (out / peak).astype(np.float32) if peak > 0 else out

    def coverage(self) -> np.ndarray:
        return self.weight / max(self.n_stacked, 1)

    def drift(self) -> tuple[float, float]:
        for o in reversed(self.history):
            if o.accepted and o.alignment is not None:
                return o.alignment.shift
        return (0.0, 0.0)

    def overlap_fraction(self) -> float:
        if not self.n_stacked:
            return 0.0
        return float(
            np.count_nonzero(self.weight >= self.n_stacked * 0.9) / self.weight.size
        )

    def set_strictness(self, level: str) -> None:
        for k, v in self.STRICTNESS.get(level, self.STRICTNESS["normal"]).items():
            setattr(self, k, v)

    def frame_score(self, stars) -> float:
        if not len(stars):
            return 0.0
        flux = float(np.median(stars.flux))
        noise = max(float(stars.noise), 1e-9)
        fwhm = max(float(stars.median_fwhm), 0.5)
        if not np.isfinite(flux) or not np.isfinite(fwhm):
            return 0.0
        halo = stars.median_halo
        penalty = (
            max(float(halo) / self.HALO_DEADBAND, 1.0) if np.isfinite(halo) else 1.0
        )
        el = stars.median_elongation
        if np.isfinite(el):
            penalty *= max(float(el) / self.ELONGATION_DEADBAND, 1.0)
        return flux / (noise**2 * fwhm**2 * penalty)

    def ref_score(self) -> float:
        if not self._scores:
            return 0.0
        return float(np.percentile(self._scores, 90))

    def frame_weight(self, stars) -> tuple[float, float]:
        sc = self.frame_score(stars)
        if np.isfinite(sc) and sc > 0:
            self._scores.append(sc)
        if not self.quality_weighting or sc <= 0:
            return 1.0, sc
        ref = self.ref_score()
        if ref <= 0:
            return 1.0, sc
        return float(np.clip(sc / ref, self.weight_floor, 1.0)), sc

    def dominant_kind(self, stars) -> tuple[str, str]:
        el = stars.median_elongation
        halo = stars.median_halo
        fwhm = stars.median_fwhm
        cand = []
        if np.isfinite(el):
            cand.append(
                (
                    float(el) / self.ELONGATION_DEADBAND,
                    "trailed",
                    _("trailed stars (elongation {value:.2f})").format(value=el),
                )
            )
        if np.isfinite(halo):
            cand.append(
                (
                    float(halo) / self.HALO_DEADBAND,
                    "smear",
                    _("flux spread out ({value:.1f}x the core)").format(value=halo),
                )
            )
        if np.isfinite(fwhm) and np.isfinite(self.best_fwhm) and self.best_fwhm > 0:
            cand.append(
                (
                    float(fwhm) / self.best_fwhm,
                    "fwhm",
                    _("FWHM {value:.1f}px vs best {best:.1f}px").format(
                        value=fwhm, best=self.best_fwhm
                    ),
                )
            )
        worst = max(cand, default=(0.0, "signal", _("little signal")))
        if worst[0] <= 1.05:
            return "signal", _("little signal (transparency or sky background)")
        return worst[1], worst[2]

    def add(
        self,
        rgb: np.ndarray,
        lum: np.ndarray,
        exposure: float = 0.0,
        lum_scale: float = 2.0,
    ) -> FrameOutcome:
        t0 = time.perf_counter()
        stars = detect(
            lum,
            scale=lum_scale,
            max_stars=self.max_stars,
            central=self.central,
            saturation=self.saturation,
        )

        w_frame, score = self.frame_weight(stars)

        def done(acc: bool, reason: str, al=None, kind: str = "ok") -> FrameOutcome:
            bright = None
            if len(stars):
                i = int(np.argmax(stars.flux))
                bright = (float(stars.xy[i, 0]), float(stars.xy[i, 1]))
            o = FrameOutcome(
                acc,
                reason,
                len(stars),
                stars.median_fwhm,
                al,
                self.n_stacked,
                (time.perf_counter() - t0) * 1e3,
                hfr=stars.median_hfr,
                brightest=bright,
                weight=w_frame,
                score=score,
                kind="ok" if acc else kind,
                elong=stars.median_elongation,
                halo=stars.median_halo,
                limits={
                    "elongation": self.max_elongation,
                    "halo": self.max_halo,
                    "fwhm": self.best_fwhm * self.fwhm_tolerance,
                    "weight": self.min_weight,
                    "stars": 3,
                },
            )
            self.history.append(o)
            if not acc:
                self.n_rejected += 1
                self.rejections[o.kind] = self.rejections.get(o.kind, 0) + 1
            elif np.isfinite(o.fwhm) and o.n_stars >= self.min_ref_stars:
                self.best_fwhm = min(self.best_fwhm, o.fwhm)
            return o

        if len(stars) < 3:
            return done(
                False, _("only {n} stars detected").format(n=len(stars)), kind="stars"
            )

        el = stars.median_elongation
        if np.isfinite(el) and el > self.max_elongation:
            return done(
                False,
                _("trailed stars (elongation {value:.2f})").format(value=el),
                kind="trailed",
            )

        halo = stars.median_halo
        if np.isfinite(halo) and halo > self.max_halo:
            return done(
                False,
                _("flux spread out ({value:.1f}x the core)").format(value=halo),
                kind="smear",
            )

        bg = float(stars.background)
        if np.isfinite(bg) and len(self._bg_history) >= 5:
            base = float(np.median(self._bg_history))
            if base > 0 and bg > base * self.max_background_jump:
                return done(
                    False,
                    _("background {ratio:.1f}x above normal").format(ratio=bg / base),
                    kind="background",
                )
        if np.isfinite(bg):
            self._bg_history.append(bg)
            if len(self._bg_history) > 40:
                del self._bg_history[0]

        fwhm = stars.median_fwhm
        if (
            np.isfinite(fwhm)
            and np.isfinite(self.best_fwhm)
            and fwhm > self.best_fwhm * self.fwhm_tolerance
        ):
            return done(
                False,
                _("FWHM {value:.1f}px vs best {best:.1f}px").format(
                    value=fwhm, best=self.best_fwhm
                ),
                kind="fwhm",
            )

        floor = self.min_weight
        if self._settle_left > 0:
            floor = min(floor / self.settle_strictness, 0.95)
        self._settle_left = max(self._settle_left - 1, 0)
        if (
            self.quality_weighting
            and self.started
            and len(self._scores) >= 5
            and w_frame < floor
        ):
            kind, text = self.dominant_kind(stars)
            extra = _(" (settling)") if floor != self.min_weight else ""
            return done(
                False,
                _("{text} — worth {weight:.2f} of tonight{extra}").format(
                    text=text, weight=w_frame, extra=extra
                ),
                kind=kind,
            )

        if not self.started:
            if len(stars) < self.min_ref_stars:
                return done(
                    False,
                    _("reference needs >={need} stars, found {found}").format(
                        need=self.min_ref_stars, found=len(stars)
                    ),
                    kind="reference",
                )
            self._accumulate(rgb, np.ones(self.shape, dtype=np.float32), w_frame)
            self.ref_stars = stars
            self.n_stacked = 1
            self.total_exposure += exposure
            self.weighted_exposure += exposure * w_frame
            self._since_ref = 0
            return done(
                True,
                _("reference started"),
                register.Alignment(
                    np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]),
                    len(stars),
                    0.0,
                    0.0,
                    1.0,
                    (0.0, 0.0),
                ),
            )

        need = min(self.min_matched, max(self.min_matched_floor, int(0.4 * len(stars))))
        if self._relax > 0:
            need = self.min_matched_floor
        if self.ref_stars is None:
            return done(False, _("no reference yet"), kind="reference")
        al = register.estimate(
            stars.xy, self.ref_stars.xy, min_matched=need, max_rms=self.max_rms
        )
        if al.matrix is None:
            return done(False, al.reason, al, kind="registration")
        if self._relax > 0:
            self._relax -= 1

        if self._last_shift is not None:
            d = float(
                np.hypot(
                    al.shift[0] - self._last_shift[0], al.shift[1] - self._last_shift[1]
                )
            )
            if d > self.settle_shift_px:
                self._settle_left = self.settle_frames
        self._last_shift = al.shift

        warped = register.warp(rgb, al.matrix, self.shape)
        cov = register.coverage_mask(self.shape, al.matrix)
        self._accumulate(warped, cov, w_frame)
        self.n_stacked += 1
        self.total_exposure += exposure
        self.weighted_exposure += exposure * w_frame

        self._since_ref += 1
        if self._since_ref >= self.ref_refresh:
            self._refresh_reference()

        return done(True, "ok", al)

    def _accumulate(self, rgb: np.ndarray, cov: np.ndarray, w: float) -> None:
        cov = cov.astype(np.float32)
        mask = cov > 0.5
        wm = (mask * w).astype(np.float32)

        if self.sigma_clip and self._count is not None:
            n = self._count
            active = n >= 4
            if np.any(active):
                sigma = np.sqrt(
                    np.maximum(self._m2 / np.maximum(n[..., None] - 1, 1), 0)
                )
                dev = np.abs(rgb - self._mean)
                outlier = (dev > self.sigma_clip * np.maximum(sigma, 1e-6)).any(axis=2)
                wm = np.where(active & outlier, 0.0, wm).astype(np.float32)

            upd = wm > 0
            if np.any(upd):
                n_new = n + upd
                delta = rgb - self._mean
                self._mean += np.where(
                    upd[..., None], delta / np.maximum(n_new, 1)[..., None], 0.0
                )
                self._m2 += np.where(upd[..., None], delta * (rgb - self._mean), 0.0)
                self._count = n_new

        self.accum += rgb * wm[..., None]
        self.weight += wm

    def _refresh_reference(self) -> None:
        stack = self.result()
        lum = stack.sum(axis=2) if stack.ndim == 3 else stack
        try:
            fresh = detect(
                lum,
                scale=1.0,
                max_stars=self.max_stars,
                central=self.central,
                saturation=None,
            )
        except Exception:
            self._since_ref = 0
            return
        if len(fresh) >= max(self.min_matched, 6):
            self.ref_stars = fresh
        self._since_ref = 0

    def preview_alignment(self, stars: StarField) -> register.Alignment | None:
        if not self.started or self.ref_stars is None or len(stars) < 3:
            return None
        return register.estimate(
            stars.xy,
            self.ref_stars.xy,
            min_matched=self.min_matched_floor,
            max_rms=self.max_rms,
        )

    def new_segment(self) -> None:
        self.best_fwhm = float("inf")
        self._bg_history.clear()
        self._last_shift = None
        self._settle_left = self.settle_frames
        self._since_ref = self.ref_refresh
        self._relax = 5
