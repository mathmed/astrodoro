"""Live stacking: acumulador com mapa de peso e referência tirada do próprio stack."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

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
    kind: str = "ok"          # categoria da rejeição, para contagem


class LiveStacker:
    """Empilha frames alinhados sobre um acumulador em float32.

    Decisão central de projeto: **a referência de registro é o próprio stack
    acumulado**, não o primeiro frame. O stack tem SNR muito maior que qualquer
    sub individual, o que dá três coisas de que este setup precisa:

    * casamento robusto quando você encosta no tubo e o campo salta;
    * retomar o stack depois de resetar a plataforma equatorial (ou em outra
      noite, no mesmo alvo);
    * degradação suave — quanto mais frames, melhor a referência fica.

    A geometria do referencial é travada no primeiro frame aceito; a lista de
    estrelas é re-extraída periodicamente do stack, nunca a geometria.
    """

    def __init__(
        self,
        shape: tuple[int, int],
        channels: int = 3,
        ref_refresh: int = 10,
        min_matched: int = 8,
        min_matched_floor: int = 5,
        min_ref_stars: int = 10,
        max_rms: float = 2.0,
        fwhm_tolerance: float = 1.8,
        max_stars: int = 60,
        central: float = 0.70,
        saturation: float | None = None,
        sigma_clip: float | None = None,
        quality_weighting: bool = True,
        weight_floor: float = 0.05,
        max_elongation: float = 1.7,
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
        # Similaridade tem 4 graus de liberdade: 2 pares já a determinam. Exigir
        # 8 pares é conservador e trava o stack em campo pobre ou logo após uma
        # recentragem, onde a sobreposição com a referência é pequena. Abaixo
        # deste piso a confiança vem do rms, não da contagem.
        self.min_matched_floor = min_matched_floor
        # O primeiro frame aceito define o referencial de todo o stack. Aceitar
        # um frame pobre (ou um punhado de pixels quentes) trava a referência em
        # lixo e nada mais casa depois. Exigência mais alta que a dos frames
        # seguintes, de propósito.
        self.min_ref_stars = min_ref_stars
        self._relax = 0
        self.max_rms = max_rms
        self.fwhm_tolerance = fwhm_tolerance
        self.max_stars = max_stars
        self.central = central
        self.saturation = saturation

        self.max_elongation = max_elongation
        self.max_background_jump = max_background_jump
        self.settle_shift_px = settle_shift_px
        self.settle_frames = settle_frames
        self.settle_strictness = settle_strictness
        self._bg_history: list[float] = []
        self._settle_left = 0
        self._last_shift: tuple[float, float] | None = None
        self.quality_weighting = quality_weighting
        self.weight_floor = weight_floor
        self._best_score = 0.0
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
        # integração efetiva: com ponderação, um frame ruim conta menos
        self.weighted_exposure = 0.0
        self.history: list[FrameOutcome] = []
        self.rejections: dict[str, int] = {}
        self.last_stars = None
        self._since_ref = 0

    # ------------------------------------------------------------------ estado
    @property
    def started(self) -> bool:
        return self.n_stacked > 0

    def result(self) -> np.ndarray:
        """Stack normalizado em 0..1, já dividido pelo mapa de peso."""
        w = np.maximum(self.weight, 1e-6)[..., None]
        out = self.accum / w
        peak = float(out.max())
        return (out / peak).astype(np.float32) if peak > 0 else out

    def coverage(self) -> np.ndarray:
        """Fração de frames que contribuiu em cada pixel (0..1)."""
        return self.weight / max(self.n_stacked, 1)

    def drift(self) -> tuple[float, float]:
        """Deslocamento acumulado do último frame em relação ao referencial."""
        for o in reversed(self.history):
            if o.accepted and o.alignment is not None:
                return o.alignment.shift
        return (0.0, 0.0)

    def overlap_fraction(self) -> float:
        """Fração do quadro ainda coberta por praticamente todos os frames.

        Cai quando a plataforma equatorial está no fim do curso — serve de
        aviso antes de você perder o alvo.
        """
        if not self.n_stacked:
            return 0.0
        return float(np.count_nonzero(self.weight >= self.n_stacked * 0.9) / self.weight.size)

    RIGOR = {
        "tolerante": dict(fwhm_tolerance=2.5, max_elongation=2.0,
                          max_background_jump=4.0),
        "normal":    dict(fwhm_tolerance=1.8, max_elongation=1.7,
                          max_background_jump=2.5),
        "rigoroso":  dict(fwhm_tolerance=1.4, max_elongation=1.4,
                          max_background_jump=1.8),
    }

    def set_rigor(self, nivel: str) -> None:
        """Ajusta os limiares em grupo. Cinco botões avulsos no painel seriam
        entulho; no campo você quer um seletor de três posições."""
        for k, v in self.RIGOR.get(nivel, self.RIGOR["normal"]).items():
            setattr(self, k, v)

    # ---------------------------------------------------------------- qualidade
    def frame_score(self, stars) -> float:
        """Qualidade do frame, para ponderar a contribuição no stack.

        Todos os frames terem peso igual desperdiça SNR num dobsoniano sem
        autoguiding, onde a qualidade varia muito: vibração depois de um
        empurrão, erro periódico da plataforma, nuvem fina, seeing.

        Para fonte pontual o SNR vai como fluxo/(ruído·FWHM), e o peso ótimo de
        uma média ponderada é sinal/variância — daí fluxo/(ruído²·FWHM²). Os três
        termos já são medidos na detecção, então isto sai de graça: fluxo mediano
        capta transparência, ruído capta fundo de céu, FWHM capta seeing e foco.
        """
        if not len(stars):
            return 0.0
        flux = float(np.median(stars.flux))
        noise = max(float(stars.noise), 1e-9)
        fwhm = max(float(stars.median_fwhm), 0.5)
        if not np.isfinite(flux) or not np.isfinite(fwhm):
            return 0.0
        return flux / (noise ** 2 * fwhm ** 2)

    def frame_weight(self, stars) -> tuple[float, float]:
        """(peso, pontuação). Peso é relativo ao melhor frame já visto.

        Nota honesta: como a referência é o melhor até agora, um frame excelente
        que apareça tarde faz os anteriores parecerem retroativamente
        super-ponderados. É inerente a empilhar ao vivo — a alternativa exigiria
        reprocessar tudo, que é o que o replay serve para fazer depois.
        """
        sc = self.frame_score(stars)
        if not self.quality_weighting or sc <= 0:
            return 1.0, sc
        self._best_score = max(self._best_score, sc)
        if self._best_score <= 0:
            return 1.0, sc
        return float(np.clip(sc / self._best_score, self.weight_floor, 1.0)), sc

    # ------------------------------------------------------------------ stacking
    def add(self, rgb: np.ndarray, lum: np.ndarray, exposure: float = 0.0,
            lum_scale: float = 2.0) -> FrameOutcome:
        """Adiciona um frame. `rgb` já demosaicado float32, `lum` a luminância.

        `lum_scale` converte coordenadas de `lum` para a grade de `rgb`.
        """
        t0 = time.perf_counter()
        stars = detect(lum, scale=lum_scale, max_stars=self.max_stars,
                       central=self.central, saturation=self.saturation)

        # Guardado para o painel de foco e a lupa: redetectar estrelas só para
        # medir foco dobraria o custo de processamento por frame.
        self.last_stars = stars

        w_frame, score = self.frame_weight(stars)

        def done(acc: bool, reason: str, al=None, kind: str = "ok") -> FrameOutcome:
            bright = None
            if len(stars):
                i = int(np.argmax(stars.flux))
                bright = (float(stars.xy[i, 0]), float(stars.xy[i, 1]))
            o = FrameOutcome(acc, reason, len(stars), stars.median_fwhm, al,
                             self.n_stacked, (time.perf_counter() - t0) * 1e3,
                             hfr=stars.median_hfr, brightest=bright,
                             weight=w_frame, score=score,
                             kind="ok" if acc else kind)
            self.history.append(o)
            if not acc:
                self.n_rejected += 1
                self.rejections[o.kind] = self.rejections.get(o.kind, 0) + 1
            return o

        if len(stars) < 3:
            return done(False, f"só {len(stars)} estrelas detectadas", kind="estrelas")

        # Frame arrastado: tubo encostado durante a exposição, ou plataforma
        # escorregando. Todas as estrelas alongadas na mesma direção — nenhum
        # filtro por estrela vê isso, porque individualmente cada uma parece
        # aceitável.
        el = stars.median_elongation
        if np.isfinite(el) and el > self.max_elongation:
            return done(False, f"estrelas arrastadas (elongação {el:.2f})", kind="arrastado")

        # Salto de fundo: farol, lua nascendo, orvalho no espelho, borda de nuvem.
        # FWHM e contagem podem passar, mas o frame só acrescenta ruído.
        bg = float(stars.background)
        if np.isfinite(bg) and len(self._bg_history) >= 5:
            base = float(np.median(self._bg_history))
            if base > 0 and bg > base * self.max_background_jump:
                return done(False, f"fundo {bg/base:.1f}x acima do normal", kind="fundo")
        if np.isfinite(bg):
            self._bg_history.append(bg)
            if len(self._bg_history) > 40:
                del self._bg_history[0]

        # Janela de assentamento: depois de um salto grande o dobsoniano vibra.
        # Em vez de descartar às cegas, exige FWHM mais apertado nos próximos
        # frames — assim um frame bom logo após a recentragem não é jogado fora.
        tol = self.fwhm_tolerance
        if self._settle_left > 0:
            tol *= self.settle_strictness

        fwhm = stars.median_fwhm
        if np.isfinite(fwhm):
            if fwhm > self.best_fwhm * tol:
                extra = " (assentando)" if self._settle_left > 0 else ""
                self._settle_left = max(self._settle_left - 1, 0)
                return done(False, f"FWHM {fwhm:.1f}px vs melhor "
                                   f"{self.best_fwhm:.1f}px{extra}",
                            kind="assentando" if extra else "fwhm")
            self.best_fwhm = min(self.best_fwhm, fwhm)
        self._settle_left = max(self._settle_left - 1, 0)

        # primeiro frame: define o referencial
        if not self.started:
            if len(stars) < self.min_ref_stars:
                return done(False, f"referencial exige >={self.min_ref_stars} "
                                   f"estrelas, há {len(stars)}", kind="referencial")
            self._accumulate(rgb, np.ones(self.shape, dtype=np.float32), w_frame)
            self.ref_stars = stars
            self.n_stacked = 1
            self.total_exposure += exposure
            self.weighted_exposure += exposure * w_frame
            self._since_ref = 0
            return done(True, "referencial iniciado",
                        register.Alignment(np.array([[1., 0., 0.], [0., 1., 0.]]),
                                           len(stars), 0.0, 0.0, 1.0, (0.0, 0.0)))

        # Exigência de pares proporcional ao que há para casar: não faz sentido
        # pedir 8 pares quando só 12 estrelas foram detectadas.
        need = min(self.min_matched, max(self.min_matched_floor, int(0.4 * len(stars))))
        if self._relax > 0:
            need = self.min_matched_floor
        al = register.estimate(stars.xy, self.ref_stars.xy,
                               min_matched=need, max_rms=self.max_rms)
        if not al.ok:
            return done(False, al.reason, al, kind="registro")
        if self._relax > 0:
            self._relax -= 1

        # salto grande em relação ao frame anterior aceito: abre a janela de
        # assentamento
        if self._last_shift is not None:
            d = float(np.hypot(al.shift[0] - self._last_shift[0],
                               al.shift[1] - self._last_shift[1]))
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
        mask = cov > 0.5           # ignora pixels só parcialmente cobertos
        wm = (mask * w).astype(np.float32)

        if self.sigma_clip and self._count is not None:
            # rejeição por sigma corrente (Welford) — mata satélite e avião sem
            # guardar a pilha inteira de frames em memória
            n = self._count
            active = n >= 4
            if np.any(active):
                sigma = np.sqrt(np.maximum(self._m2 / np.maximum(n[..., None] - 1, 1), 0))
                dev = np.abs(rgb - self._mean)
                outlier = (dev > self.sigma_clip * np.maximum(sigma, 1e-6)).any(axis=2)
                wm = np.where(active & outlier, 0.0, wm).astype(np.float32)

            upd = wm > 0
            if np.any(upd):
                n_new = n + upd
                delta = rgb - self._mean
                self._mean += np.where(upd[..., None],
                                       delta / np.maximum(n_new, 1)[..., None], 0.0)
                self._m2 += np.where(upd[..., None], delta * (rgb - self._mean), 0.0)
                self._count = n_new

        self.accum += rgb * wm[..., None]
        self.weight += wm

    def _refresh_reference(self) -> None:
        """Re-extrai as estrelas da referência a partir do stack acumulado.

        Só a *lista de estrelas* é atualizada — a geometria do referencial
        continua a do primeiro frame aceito. Trocar a geometria faria o stack
        escorregar, acumulando erro a cada atualização.
        """
        stack = self.result()
        lum = stack.sum(axis=2) if stack.ndim == 3 else stack
        try:
            fresh = detect(lum, scale=1.0, max_stars=self.max_stars,
                           central=self.central, saturation=None)
        except Exception:
            self._since_ref = 0
            return
        if len(fresh) >= max(self.min_matched, 6):
            self.ref_stars = fresh
        self._since_ref = 0

    def new_segment(self) -> None:
        """Marca uma descontinuidade — reset da plataforma, recentragem manual.

        Não zera nada: o próximo frame é registrado contra o stack existente,
        que é exatamente o que permite retomar depois de resetar a plataforma
        equatorial. Serve para relaxar os limiares de qualidade uma vez, já que
        o primeiro frame após o reset costuma vir pior.
        """
        self.best_fwhm = float("inf")
        self._bg_history.clear()
        self._last_shift = None
        self._settle_left = self.settle_frames
        self._since_ref = self.ref_refresh   # força atualizar a referência
        # O primeiro frame após um reset vem pior e com pouca sobreposição:
        # relaxa a exigência de pares por algumas tentativas.
        self._relax = 5
