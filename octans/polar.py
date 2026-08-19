"""Alinhamento polar da plataforma, por determinação geométrica do eixo.

O método: pontos que giram em torno de um eixo A descrevem um círculo, e esse
círculo está num plano perpendicular a A. Então basta resolver o campo em três
ou mais momentos, ajustar um plano aos vetores unitários resultantes, e a normal
do plano **é** o eixo real da plataforma. Comparando com o polo celeste sai a
correção em altitude e azimute.

Isso é exato e não depende de fórmulas aproximadas de drift align — que, além de
serem fáceis de errar, pressupõem estrelas em posições específicas (meridiano,
equador celeste, horizonte leste). Aqui serve qualquer alvo, em qualquer parte do
céu, que é o que você tem num dobsoniano apontado onde dá.

No hemisfério sul isto importa mais ainda: Sigma Octantis é magnitude 5,4 e você
não a acha num céu urbano, então alinhar olhando o polo está fora de questão.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .catalog import radec_to_vec, vec_to_radec

try:                                    # evita tentativa de download no campo
    from astropy.utils import iers
    iers.conf.auto_download = False
except Exception:
    pass


@dataclass
class PolarResult:
    axis_ra: float
    axis_dec: float
    total_error_deg: float
    alt_error_deg: float          # positivo: eixo da plataforma alto demais
    az_error_deg: float           # positivo: eixo girado para leste do polo
    n_points: int
    rms_deg: float
    hemisphere: str
    advice: str = ""

    @property
    def good(self) -> bool:
        return self.total_error_deg < 0.25


def fit_rotation_axis(vectors: np.ndarray,
                      expect_south: bool = True) -> tuple[np.ndarray, float]:
    """Eixo de rotação a partir de vetores unitários do mesmo alvo em tempos
    diferentes. Devolve (eixo, rms em graus do ajuste do plano).

    Precisa de pelo menos 3 pontos. Quanto maior o arco percorrido, melhor
    condicionado o ajuste — 15 a 30 minutos de rotação é bem melhor que 2.
    """
    P = np.asarray(vectors, dtype=float)
    P = P / np.linalg.norm(P, axis=1, keepdims=True)
    if len(P) < 3:
        raise ValueError("são necessários ao menos 3 pontos")

    c = P.mean(axis=0)
    _, _, Vt = np.linalg.svd(P - c)
    n = Vt[2] / np.linalg.norm(Vt[2])

    # resíduo: distância dos pontos ao plano ajustado, em graus
    d = (P - c) @ n
    rms = float(np.degrees(np.sqrt(np.mean(d ** 2))))

    # ambiguidade de sinal: escolhe o hemisfério esperado
    if (expect_south and n[2] > 0) or (not expect_south and n[2] < 0):
        n = -n
    return n, rms


def analyse(radecs: list[tuple[float, float]], latitude: float, longitude: float,
            when, elevation_m: float = 0.0) -> PolarResult:
    """Da lista de (RA, Dec) resolvidos sai a correção da plataforma.

    `when` é o instante médio das observações (datetime ou astropy Time). O
    instante importa: o eixo é medido em coordenadas equatoriais, mas a correção
    que você aplica é mecânica, no referencial local — e a conversão entre os
    dois depende do tempo sideral.
    """
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time

    south = latitude < 0
    vecs = radec_to_vec(np.array([r for r, _ in radecs]),
                        np.array([d for _, d in radecs]))
    axis, rms = fit_rotation_axis(vecs, expect_south=south)
    ra, dec = vec_to_radec(axis)

    site = EarthLocation(lat=latitude * u.deg, lon=longitude * u.deg,
                         height=elevation_m * u.m)
    t = when if isinstance(when, Time) else Time(when)
    frame = AltAz(obstime=t, location=site)

    axis_h = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame="icrs").transform_to(frame)
    pole_dec = -90.0 if south else 90.0
    pole_h = SkyCoord(ra=0 * u.deg, dec=pole_dec * u.deg,
                      frame="icrs").transform_to(frame)

    alt_err = float(axis_h.alt.deg - pole_h.alt.deg)
    az_err = float(((axis_h.az.deg - pole_h.az.deg + 180.0) % 360.0) - 180.0)
    total = float(np.degrees(np.arccos(np.clip(
        np.dot(axis, radec_to_vec(np.array([0.0]), np.array([pole_dec]))[0]), -1, 1))))

    res = PolarResult(
        axis_ra=ra, axis_dec=dec, total_error_deg=total,
        alt_error_deg=alt_err, az_error_deg=az_err,
        n_points=len(radecs), rms_deg=rms,
        hemisphere="sul" if south else "norte",
    )
    res.advice = _advice(res)
    return res


def _advice(r: PolarResult) -> str:
    if r.good:
        return f"alinhamento bom: erro total {r.total_error_deg*60:.1f}'"
    parts = []
    if abs(r.alt_error_deg) > 0.03:
        d = "abaixe" if r.alt_error_deg > 0 else "levante"
        parts.append(f"{d} a plataforma {abs(r.alt_error_deg)*60:.1f}'")
    if abs(r.az_error_deg) > 0.03:
        d = "oeste" if r.az_error_deg > 0 else "leste"
        parts.append(f"gire {abs(r.az_error_deg)*60:.1f}' para {d}")
    return (f"erro total {r.total_error_deg*60:.1f}' — " + ", ".join(parts)
            if parts else f"erro total {r.total_error_deg*60:.1f}'")


def rotate_about(v: np.ndarray, axis: np.ndarray, angle_deg: float) -> np.ndarray:
    """Rodrigues — usado nos testes para sintetizar rotações conhecidas."""
    a = np.asarray(axis, float) / np.linalg.norm(axis)
    th = np.deg2rad(angle_deg)
    return (v * np.cos(th) + np.cross(a, v) * np.sin(th)
            + a * (a @ v) * (1 - np.cos(th)))
