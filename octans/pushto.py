"""Push-to: círculo graduado digital, sem encoder.

Com plate solve você sabe onde está apontado. Dado um alvo, isto diz para onde
empurrar o tubo. Num dobsoniano os movimentos são em altitude e azimute — mesmo
sobre plataforma equatorial, o tubo continua se movendo em alt-az — então é
nessas duas grandezas que a orientação tem de ser dada.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .catalog import angular_sep

try:
    from astropy.utils import iers
    iers.conf.auto_download = False
except Exception:
    pass


@dataclass
class Guidance:
    separation_deg: float
    delta_alt_deg: float          # positivo: suba o tubo
    delta_az_deg: float           # positivo: gire para oeste
    target_alt_deg: float
    target_az_deg: float
    on_target: bool
    text: str
    zenith_warning: bool = False

    @property
    def arrow(self) -> tuple[float, float]:
        """Vetor unitário para desenhar a seta na tela (x = azimute, y = altitude)."""
        v = np.array([self.delta_az_deg, self.delta_alt_deg])
        n = np.linalg.norm(v)
        return (0.0, 0.0) if n < 1e-9 else tuple(v / n)


def guide(current: tuple[float, float], target: tuple[float, float],
          latitude: float, longitude: float, when=None,
          elevation_m: float = 0.0, tolerance_deg: float = 0.25,
          fov_deg: float | None = None) -> Guidance:
    """`current` e `target` em (RA, Dec) graus."""
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    t = Time(when) if when is not None else Time.now()
    site = EarthLocation(lat=latitude * u.deg, lon=longitude * u.deg,
                         height=elevation_m * u.m)
    frame = AltAz(obstime=t, location=site)

    cur = SkyCoord(ra=current[0] * u.deg, dec=current[1] * u.deg).transform_to(frame)
    tgt = SkyCoord(ra=target[0] * u.deg, dec=target[1] * u.deg).transform_to(frame)

    d_alt = float(tgt.alt.deg - cur.alt.deg)
    d_az = float(((tgt.az.deg - cur.az.deg + 180.0) % 360.0) - 180.0)
    sep = angular_sep(current[0], current[1], target[0], target[1])

    tol = tolerance_deg if fov_deg is None else min(tolerance_deg, fov_deg * 0.25)
    on = sep <= tol
    # Perto do zênite o azimute converge: a correção em azimute é amplificada por
    # 1/cos(alt) e fica instável, além de o próprio dobsoniano ficar difícil de
    # mover com precisão nessa região.
    zenith = float(tgt.alt.deg) > 80.0

    if float(tgt.alt.deg) < 0:
        text = f"alvo abaixo do horizonte (altitude {tgt.alt.deg:.1f}°)"
    elif on:
        text = f"no alvo — {sep*60:.1f}' do centro"
    else:
        bits = []
        if abs(d_alt) > tol / 2:
            bits.append(f"{'suba' if d_alt > 0 else 'baixe'} {_fmt(abs(d_alt))}")
        if abs(d_az) > tol / 2:
            bits.append(f"gire {_fmt(abs(d_az))} para {'oeste' if d_az > 0 else 'leste'}")
        text = " · ".join(bits) or f"{sep*60:.1f}' do alvo"
    if zenith and not on:
        text += " (perto do zênite: azimute instável, prefira corrigir a altitude)"
    return Guidance(sep, d_alt, d_az, float(tgt.alt.deg), float(tgt.az.deg),
                    on, text, zenith_warning=zenith)


def _fmt(deg: float) -> str:
    return f"{deg*60:.0f}'" if deg < 1.0 else f"{deg:.2f}°"
