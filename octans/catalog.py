"""Catálogo de objetos, para responder "o que estou fotografando".

Usa o OpenNGC (NGC + IC completos, ~14 mil objetos, CC-BY-SA 4.0 de Mattia
Verga). Com uma solução astrométrica, isso vira anotação sobre a imagem e alvos
para o push-to.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent.parent / "data" / "NGC.csv"

# tipos que interessam em EAA visual, com rótulo curto
TYPES = {
    "G": "galáxia", "GPair": "par de galáxias", "GTrpl": "trio de galáxias",
    "GGroup": "grupo de galáxias", "GCl": "aglomerado globular",
    "OCl": "aglomerado aberto", "Cl+N": "aglomerado + nebulosa",
    "PN": "nebulosa planetária", "HII": "região HII", "Neb": "nebulosa",
    "RfN": "nebulosa de reflexão", "EmN": "nebulosa de emissão",
    "SNR": "remanescente de supernova", "Dup": "duplicata", "*": "estrela",
    "**": "estrela dupla", "*Ass": "associação estelar", "Nova": "nova",
    "NonEx": "inexistente", "Other": "outro",
}
INTERESTING = {"G", "GPair", "GTrpl", "GGroup", "GCl", "OCl", "Cl+N", "PN",
               "HII", "Neb", "RfN", "EmN", "SNR"}


@dataclass
class Obj:
    name: str
    kind: str
    ra: float                 # graus
    dec: float                # graus
    major_arcmin: float
    minor_arcmin: float
    mag: float
    messier: str
    common: str

    @property
    def label(self) -> str:
        if self.messier:
            base = f"M{int(self.messier)}"
            if self.common:
                return f"{base} ({self.common})"
            return f"{base} / {self.name}"
        return f"{self.name} ({self.common})" if self.common else self.name

    @property
    def kind_label(self) -> str:
        return TYPES.get(self.kind, self.kind)


def _sex_to_deg(s: str, is_ra: bool) -> float:
    s = s.strip()
    if not s:
        return float("nan")
    sign = -1.0 if s.startswith("-") else 1.0
    parts = re.split(r"[:\s]+", s.lstrip("+-"))
    try:
        vals = [float(x) for x in parts[:3]]
    except ValueError:
        return float("nan")
    while len(vals) < 3:
        vals.append(0.0)
    deg = vals[0] + vals[1] / 60.0 + vals[2] / 3600.0
    return sign * deg * (15.0 if is_ra else 1.0)


class Catalog:
    def __init__(self, path: Path | str = DATA):
        self.path = Path(path)
        self.objs: list[Obj] = []
        self._ra = np.empty(0)
        self._dec = np.empty(0)
        self._vec = np.empty((0, 3))

    def load(self, only_interesting: bool = True) -> "Catalog":
        if not self.path.exists():
            raise FileNotFoundError(
                f"{self.path} não encontrado — rode scripts/get_catalog.sh")
        rows: list[Obj] = []
        with self.path.open(encoding="utf-8") as fh:
            header = fh.readline().rstrip("\n").split(";")
            idx = {n: i for i, n in enumerate(header)}
            for line in fh:
                f = line.rstrip("\n").split(";")
                if len(f) < len(header):
                    continue
                kind = f[idx["Type"]].strip()
                if only_interesting and kind not in INTERESTING:
                    continue
                ra = _sex_to_deg(f[idx["RA"]], True)
                dec = _sex_to_deg(f[idx["Dec"]], False)
                if not (np.isfinite(ra) and np.isfinite(dec)):
                    continue

                def num(key, default=float("nan")):
                    v = f[idx[key]].strip()
                    try:
                        return float(v)
                    except ValueError:
                        return default

                rows.append(Obj(
                    name=f[idx["Name"]].strip(), kind=kind, ra=ra, dec=dec,
                    major_arcmin=num("MajAx"), minor_arcmin=num("MinAx"),
                    mag=num("V-Mag") if np.isfinite(num("V-Mag")) else num("B-Mag"),
                    messier=f[idx["M"]].strip(),
                    common=f[idx["Common names"]].strip().split(",")[0],
                ))
        self.objs = rows
        self._ra = np.array([o.ra for o in rows])
        self._dec = np.array([o.dec for o in rows])
        self._vec = radec_to_vec(self._ra, self._dec)
        return self

    # ------------------------------------------------------------------ busca
    def find(self, query: str) -> Obj | None:
        """Aceita 'M42', 'm 42', 'NGC253', 'ngc 253', 'IC434' ou nome popular."""
        q = query.strip().lower().replace(" ", "")
        m = re.fullmatch(r"m(\d{1,3})", q)
        if m:
            want = str(int(m.group(1)))
            for o in self.objs:
                if o.messier and str(int(o.messier)) == want:
                    return o
            return None
        m = re.fullmatch(r"(ngc|ic)0*(\d{1,4})([a-z]?)", q)
        if m:
            pref = "NGC" if m.group(1) == "ngc" else "IC"
            name = f"{pref}{int(m.group(2)):04d}{m.group(3).upper()}"
            for o in self.objs:
                if o.name.upper() == name:
                    return o
            return None
        for o in self.objs:
            if o.common and o.common.lower().replace(" ", "") == q:
                return o
        for o in self.objs:
            if o.common and q in o.common.lower().replace(" ", ""):
                return o
        return None

    def near(self, ra: float, dec: float, radius_deg: float,
             limit: int = 40) -> list[Obj]:
        if not len(self.objs):
            return []
        v = radec_to_vec(np.array([ra]), np.array([dec]))[0]
        cos = self._vec @ v
        m = cos > np.cos(np.deg2rad(radius_deg))
        idx = np.flatnonzero(m)
        idx = idx[np.argsort(-cos[idx])][:limit]
        return [self.objs[i] for i in idx]

    def in_field(self, wcs, shape: tuple[int, int], limit: int = 40,
                 margin: float = 0.02) -> list[tuple[Obj, float, float]]:
        """Objetos dentro do quadro, com posição em pixels."""
        h, w = shape
        try:
            x, y = wcs.all_world2pix(self._ra, self._dec, 0)
        except Exception:
            return []
        mx, my = w * margin, h * margin
        m = (x > -mx) & (x < w + mx) & (y > -my) & (y < h + my)
        idx = np.flatnonzero(m)
        # prioriza o que é visível: brilho, e tamanho como desempate
        key = np.where(np.isfinite([self.objs[i].mag for i in idx]),
                       [self.objs[i].mag if np.isfinite(self.objs[i].mag) else 99.0
                        for i in idx], 99.0)
        idx = idx[np.argsort(key)][:limit]
        return [(self.objs[i], float(x[i]), float(y[i])) for i in idx]


def radec_to_vec(ra_deg: np.ndarray, dec_deg: np.ndarray) -> np.ndarray:
    ra, dec = np.deg2rad(ra_deg), np.deg2rad(dec_deg)
    return np.column_stack([np.cos(dec) * np.cos(ra),
                            np.cos(dec) * np.sin(ra),
                            np.sin(dec)])


def vec_to_radec(v: np.ndarray) -> tuple[float, float]:
    v = np.asarray(v, dtype=float)
    v = v / np.linalg.norm(v)
    dec = np.degrees(np.arcsin(np.clip(v[2], -1, 1)))
    ra = np.degrees(np.arctan2(v[1], v[0])) % 360.0
    return float(ra), float(dec)


def angular_sep(ra1, dec1, ra2, dec2) -> float:
    a = radec_to_vec(np.array([ra1]), np.array([dec1]))[0]
    b = radec_to_vec(np.array([ra2]), np.array([dec2]))[0]
    return float(np.degrees(np.arccos(np.clip(a @ b, -1, 1))))
