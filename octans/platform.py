"""Gerência da plataforma equatorial.

Nenhum software de EAA trata disto, porque quase ninguém faz EAA com dobsoniano
em plataforma. São dois problemas específicos:

1. A plataforma tem curso finito (45 a 75 min) e precisa ser resetada.
2. Alinhamento polar feito no olho deixa rotação de campo residual.

O item 2 é o que realmente limita a sessão, e a pergunta útil não é "quanto
tempo de plataforma sobra" mas "quanto tempo de integração sobra antes das
estrelas do canto virarem risco". Isso é calculável a partir da rotação que já
medimos frame a frame no registro.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np


@dataclass
class Fit:
    rate: float            # unidade por minuto
    r2: float              # qualidade do ajuste (0..1)
    n: int
    span_s: float

    @property
    def solid(self) -> bool:
        """Ajuste confiável: amostras suficientes, tempo suficiente, r2 decente."""
        return self.n >= 8 and self.span_s >= 90 and self.r2 >= 0.5


class PlatformMonitor:
    def __init__(self, limit_minutes: float = 60.0, window_minutes: float = 12.0):
        self.limit_s = limit_minutes * 60.0
        self.window_s = window_minutes * 60.0
        self.run_start: float | None = None
        self.resets: list[float] = []
        self._t: list[float] = []
        self._rot: list[float] = []
        self._dx: list[float] = []
        self._dy: list[float] = []
        self._fwhm: list[float] = []

    # ------------------------------------------------------------------ curso
    def start_run(self) -> None:
        self.run_start = time.time()

    def note_reset(self) -> None:
        """Plataforma resetada: o curso reinicia, mas o histórico de rotação
        continua valendo — o erro de alinhamento polar é o mesmo."""
        self.resets.append(time.time())
        self.run_start = time.time()

    @property
    def elapsed_s(self) -> float:
        return 0.0 if self.run_start is None else time.time() - self.run_start

    @property
    def remaining_s(self) -> float:
        return max(self.limit_s - self.elapsed_s, 0.0)

    # ------------------------------------------------------------------ dados
    def add(self, rotation_deg: float, dx: float, dy: float,
            fwhm: float, t: float | None = None) -> None:
        t = time.time() if t is None else t
        self._t.append(t)
        self._rot.append(rotation_deg)
        self._dx.append(dx)
        self._dy.append(dy)
        self._fwhm.append(fwhm)
        self._trim()

    def _trim(self) -> None:
        if not self._t:
            return
        cut = self._t[-1] - self.window_s
        k = 0
        while k < len(self._t) - 1 and self._t[k] < cut:
            k += 1
        if k:
            for lst in (self._t, self._rot, self._dx, self._dy, self._fwhm):
                del lst[:k]

    def clear(self) -> None:
        for lst in (self._t, self._rot, self._dx, self._dy, self._fwhm):
            lst.clear()

    # ------------------------------------------------------------------ ajustes
    def _fit(self, values: list[float]) -> Fit:
        if len(self._t) < 4:
            return Fit(float("nan"), 0.0, len(self._t), 0.0)
        t = np.asarray(self._t, dtype=float)
        v = np.asarray(values, dtype=float)
        m = np.isfinite(v)
        if m.sum() < 4:
            return Fit(float("nan"), 0.0, int(m.sum()), 0.0)
        t, v = t[m], v[m]
        t = (t - t[0]) / 60.0                      # minutos
        span = float(t[-1] * 60.0)
        if t[-1] <= 1e-6:
            return Fit(float("nan"), 0.0, len(t), span)
        a, b = np.polyfit(t, v, 1)
        pred = a * t + b
        ss_res = float(((v - pred) ** 2).sum())
        ss_tot = float(((v - v.mean()) ** 2).sum())
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        return Fit(float(a), float(np.clip(r2, 0.0, 1.0)), len(t), span)

    def rotation_fit(self) -> Fit:
        """Rotação residual em graus por minuto.

        Este é o número que mede o alinhamento polar. Uma plataforma perfeitamente
        alinhada dá zero; quanto mais longe do polo, mais rápido o campo gira.
        """
        return self._fit(self._rot)

    def drift_fit(self) -> tuple[Fit, Fit]:
        return self._fit(self._dx), self._fit(self._dy)

    # ------------------------------------------------- orçamento de rotação
    def corner_radius(self, shape: tuple[int, int]) -> float:
        h, w = shape
        return 0.5 * float(np.hypot(w, h))

    def corner_smear_rate(self, shape: tuple[int, int]) -> float:
        """Arrasto rotacional no canto do quadro, em px por minuto.

        Rotação de campo não borra uniformemente: no centro de rotação não borra
        nada, e o deslocamento cresce linearmente com o raio. O canto é o pior
        caso, e é onde as estrelas viram risco primeiro.
        """
        f = self.rotation_fit()
        if not np.isfinite(f.rate):
            return float("nan")
        return abs(np.deg2rad(f.rate)) * self.corner_radius(shape)

    def useful_seconds(self, shape: tuple[int, int],
                       fwhm_px: float | None = None) -> float:
        """Segundos de integração até o arrasto no canto igualar o FWHM.

        É o limite prático da exposição total: passando disso, as estrelas das
        bordas ficam visivelmente alongadas no stack, mesmo que a plataforma
        ainda tenha curso.
        """
        rate = self.corner_smear_rate(shape)
        if not np.isfinite(rate) or rate <= 1e-9:
            return float("inf")
        if fwhm_px is None:
            vals = [v for v in self._fwhm if np.isfinite(v)]
            fwhm_px = float(np.median(vals)) if vals else 3.0
        return float(fwhm_px / rate * 60.0)

    # ------------------------------------------------------------------ resumo
    def report(self, shape: tuple[int, int]) -> dict:
        rot = self.rotation_fit()
        fdx, fdy = self.drift_fit()
        smear = self.corner_smear_rate(shape)
        useful = self.useful_seconds(shape)
        drift_rate = float(np.hypot(fdx.rate, fdy.rate)) if np.isfinite(fdx.rate) else float("nan")
        drift_angle = (float(np.degrees(np.arctan2(fdy.rate, fdx.rate)))
                       if np.isfinite(fdx.rate) else float("nan"))
        return {
            "elapsed_s": self.elapsed_s,
            "remaining_s": self.remaining_s,
            "resets": len(self.resets),
            "rotation_deg_min": rot.rate,
            "rotation_r2": rot.r2,
            "rotation_solid": rot.solid,
            "drift_px_min": drift_rate,
            "drift_angle_deg": drift_angle,
            "corner_smear_px_min": smear,
            "useful_s": useful,
            "n_samples": rot.n,
            "span_s": rot.span_s,
        }

    def advice(self, shape: tuple[int, int]) -> str:
        r = self.report(shape)
        if not r["rotation_solid"]:
            need = max(0, 90 - int(r["span_s"]))
            return (f"medindo rotação… {r['n_samples']} amostras"
                    + (f", faltam ~{need}s" if need else ""))
        rot = abs(r["rotation_deg_min"])
        u = r["useful_s"]
        parts = [f"rotação residual {rot:.3f}°/min"]
        if np.isfinite(u):
            parts.append(f"integração útil ~{u/60:.0f} min antes de arrastar o canto")
        if r["remaining_s"] < 600:
            parts.append(f"plataforma com {r['remaining_s']/60:.0f} min de curso")
        return " · ".join(parts)
