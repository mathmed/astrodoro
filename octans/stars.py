"""Detecção de estrelas e métricas de qualidade de frame."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import sep


@dataclass
class StarField:
    xy: np.ndarray          # (N, 2) float64, coordenadas em resolução plena
    flux: np.ndarray        # (N,)
    fwhm: np.ndarray        # (N,) em px de resolução plena
    background: float
    noise: float
    hfr: np.ndarray = field(default_factory=lambda: np.empty(0))
    peak: np.ndarray = field(default_factory=lambda: np.empty(0))
    elong: np.ndarray = field(default_factory=lambda: np.empty(0))

    def __len__(self) -> int:
        return len(self.xy)

    @property
    def median_fwhm(self) -> float:
        return float(np.median(self.fwhm)) if len(self) else float("nan")

    @property
    def median_elongation(self) -> float:
        """Elongação mediana (eixo maior / menor).

        Por estrela, elongação alta é trilha de satélite ou raio cósmico e já é
        filtrada na detecção. Mediana alta é outra coisa: significa que TODAS as
        estrelas estão alongadas, ou seja o tubo se moveu durante a exposição.
        Nenhum filtro por estrela pega isso.
        """
        return float(np.median(self.elong)) if len(self.elong) else float("nan")

    @property
    def median_hfr(self) -> float:
        """HFR mediano. Mais estável que FWHM quando o foco está ruim, porque
        não depende de ajustar um perfil a uma estrela que virou uma rosquinha."""
        return float(np.median(self.hfr)) if len(self.hfr) else float("nan")


def detect(
    lum: np.ndarray,
    scale: float = 2.0,
    thresh_sigma: float = 5.0,
    min_area: int = 4,
    max_stars: int = 60,
    central: float = 0.70,
    saturation: float | None = None,
    min_central: int = 12,
) -> StarField:
    """Extrai estrelas de uma imagem de luminância.

    `lum` normalmente vem de `cfa_to_luminance` (meia resolução); `scale`
    converte as coordenadas de volta para resolução plena.

    `central` restringe a detecção à fração central do quadro. Num dobsoniano
    rápido a coma nas bordas enviesa o centróide e envenena o ajuste — o stack
    usa o quadro inteiro, mas o registro só confia no centro.

    Estrelas saturadas são descartadas: o centróide delas é achatado e puxa a
    transformada.
    """
    data = np.ascontiguousarray(lum, dtype=np.float32)
    bkg = sep.Background(data)
    sub = data - bkg.back()
    rms = float(bkg.globalrms)

    try:
        objs = sep.extract(sub, thresh_sigma, err=rms, minarea=min_area)
    except Exception:
        # sep estoura se o buffer de deblending encher num campo muito denso
        objs = sep.extract(sub, thresh_sigma * 2, err=rms, minarea=min_area)

    if len(objs) == 0:
        return StarField(np.empty((0, 2)), np.empty(0), np.empty(0), float(bkg.globalback), rms)

    x, y = objs["x"], objs["y"]
    keep = np.ones(len(objs), dtype=bool)

    if 0 < central < 1:
        h, w = data.shape
        mx, my = w * (1 - central) / 2, h * (1 - central) / 2
        inner = (x > mx) & (x < w - mx) & (y > my) & (y < h - my)
        # Só vale abrir mão das bordas se o centro ainda entregar estrelas
        # suficientes. Num campo pobre, ou logo depois de uma recentragem
        # manual em que o alvo caiu fora do centro, exigir o centro deixaria o
        # registro sem material e o stack travaria rejeitando tudo.
        if np.count_nonzero(keep & inner) >= min_central:
            keep &= inner

    if saturation is not None:
        keep &= objs["peak"] < saturation * 0.95

    # elipticidade absurda = trilha, raio cósmico ou coluna quente
    a, b = objs["a"], objs["b"]
    with np.errstate(divide="ignore", invalid="ignore"):
        elong = np.where(b > 0, a / b, np.inf)
    keep &= elong < 3.0

    objs, x, y = objs[keep], x[keep], y[keep]
    if len(objs) == 0:
        return StarField(np.empty((0, 2)), np.empty(0), np.empty(0), float(bkg.globalback), rms)

    order = np.argsort(objs["flux"])[::-1][:max_stars]
    objs = objs[order]

    fwhm = 2.3548 * np.sqrt(np.abs(objs["a"] * objs["b"])) * scale
    xy = np.column_stack([objs["x"] * scale, objs["y"] * scale]).astype(np.float64)

    # HFR: raio que contém metade do fluxo. Para uma gaussiana vale
    # sigma*sqrt(2 ln 2) = 1.1774*sigma, mas ele continua bem definido quando a
    # estrela desfocada deixa de ser gaussiana — por isso é a metrica de foco.
    try:
        # Raio de integração por estrela, não mediano: uma estrela muito
        # desfocada precisa de janela maior, e truncar a janela subestima o HFR
        # exatamente quando você mais precisa dele (desfoque grande).
        rmax = np.clip(np.asarray(objs["a"], dtype=np.float64) * 8.0, 4.0, 60.0)
        # Normalizado pelo fluxo isofotal. Isso subestima o HFR no desfoque
        # extremo (~19% em sigma=7), porque o fluxo isofotal ignora as asas do
        # perfil. Tentar corrigir com fluxo de abertura e' pior: a abertura
        # grande captura estrelas vizinhas e fundo, e o erro inverte para +250%.
        # Na faixa util de foco (HFR 1.4 a 3 px) o erro fica em +-2%, e a metrica
        # e' monotonica em todo o intervalo -- que e' o que focar exige.
        hfr, hflag = sep.flux_radius(sub, objs["x"], objs["y"], rmax, 0.5,
                                     normflux=objs["flux"], subpix=5)
        hfr = np.asarray(hfr, dtype=np.float64) * scale
    except Exception:
        hfr = fwhm / 2.3548 * 1.1774
    a2, b2 = np.asarray(objs["a"], float), np.asarray(objs["b"], float)
    with np.errstate(divide="ignore", invalid="ignore"):
        el = np.where(b2 > 0, a2 / b2, np.nan)
    return StarField(xy, objs["flux"].astype(np.float64), fwhm,
                     float(bkg.globalback), rms,
                     hfr=hfr, peak=objs["peak"].astype(np.float64), elong=el)
