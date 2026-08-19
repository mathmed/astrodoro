"""Plate solving — envelope sobre solvers externos.

Suporta astrometry.net (`solve-field`) e ASTAP (`astap`), detectando o que
estiver instalado. Nenhum dos dois vem junto: ambos precisam de arquivos de
índice ou banco de estrelas, que são grandes. Sem solver, tudo aqui degrada
silenciosamente e o resto do programa segue funcionando.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits


@dataclass
class Solution:
    ra: float                 # centro, graus
    dec: float
    scale_arcsec: float       # arcsec por pixel
    orientation_deg: float    # ângulo de posição do eixo Y da imagem
    flipped: bool             # paridade invertida (espelhado)
    fov_deg: tuple[float, float]
    solver: str
    elapsed: float
    wcs: object = None

    def __str__(self) -> str:
        return (f"RA {self.ra:.4f}° Dec {self.dec:+.4f}° · {self.scale_arcsec:.2f}\"/px · "
                f"campo {self.fov_deg[0]*60:.1f}'x{self.fov_deg[1]*60:.1f}' · "
                f"PA {self.orientation_deg:.1f}°{' espelhado' if self.flipped else ''} · "
                f"{self.solver} em {self.elapsed:.1f}s")


LOCAL_CFG = Path(__file__).resolve().parent.parent / "data" / "astrometry.cfg"

# Últimas linhas da saída do solver. Falha silenciosa custou tempo de depuração:
# agora quem chama pode mostrar o motivo.
LAST_LOG: str = ""


def solvers_available() -> dict[str, str | None]:
    return {"astrometry.net": shutil.which("solve-field"),
            "astap": shutil.which("astap") or shutil.which("astap_cli")}


def install_hint() -> str:
    return (
        "nenhum solver instalado. Duas opções:\n"
        "  astrometry.net:  brew install astrometry-net\n"
        "                   + índices para o seu campo (scripts/get_indexes.sh)\n"
        "  ASTAP:           baixe de hnsky.org (não está no Homebrew)\n"
        "                   + banco de estrelas H18 ou o menor V17"
    )


def solve(image: np.ndarray | str | Path, *,
          scale_arcsec: float | None = None,
          scale_tolerance: float = 0.25,
          hint: tuple[float, float] | None = None,
          radius_deg: float = 15.0,
          timeout: float = 90.0,
          downsample: int = 1,
          prefer: str | None = None) -> Solution | None:
    """Resolve um frame. `image` pode ser array 2D (luminância) ou caminho FITS.

    `scale_arcsec` acelera muito a busca — passe a escala conhecida. `hint` é um
    (RA, Dec) aproximado, que com `radius_deg` restringe a busca; num dobsoniano
    sem goto normalmente você não tem hint, e aí a busca é cega.
    """
    avail = solvers_available()
    order = [prefer] if prefer else []
    order += ["astap", "astrometry.net"]
    tmp = Path(tempfile.mkdtemp(prefix="octans-solve-"))
    try:
        path = _ensure_fits(image, tmp)
        for name in order:
            exe = avail.get(name)
            if not exe:
                continue
            t0 = time.perf_counter()
            fn = _solve_astap if name == "astap" else _solve_anet
            sol = fn(exe, path, scale_arcsec, scale_tolerance, hint,
                     radius_deg, timeout, downsample)
            if sol:
                sol.solver = name
                sol.elapsed = time.perf_counter() - t0
                return sol
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ensure_fits(image, tmp: Path) -> Path:
    if isinstance(image, (str, Path)):
        return Path(image)
    a = np.asarray(image)
    if a.ndim == 3:
        a = a.mean(axis=2)
    # solvers gostam de 16 bits; normaliza preservando a estrutura
    a = a.astype(np.float64)
    lo, hi = np.percentile(a, [0.5, 99.9])
    a = np.clip((a - lo) / max(hi - lo, 1e-9), 0, 1) * 65535.0
    p = tmp / "frame.fits"
    fits.PrimaryHDU(a.astype(np.uint16)).writeto(p, overwrite=True)
    return p


def _solve_anet(exe, path: Path, scale, tol, hint, radius, timeout, downsample):
    out = path.parent / "anet"
    out.mkdir(exist_ok=True)
    cmd = [exe, "--overwrite", "--no-plots", "--no-verify", "--crpix-center",
           "-D", str(out), "-N", "none", "-U", "none", "-S", "none",
           "-M", "none", "-R", "none", "-B", "none", "--axy", "none",
           "--downsample", str(downsample), "-l", str(int(timeout))]
    if LOCAL_CFG.exists():
        # cfg do projeto: aponta para data/indexes sem tocar no do Homebrew
        cmd += ["--backend-config", str(LOCAL_CFG)]
    if scale:
        cmd += ["-u", "arcsecperpix", "-L", f"{scale*(1-tol):.4f}",
                "-H", f"{scale*(1+tol):.4f}"]
    if hint:
        cmd += ["--ra", f"{hint[0]:.5f}", "--dec", f"{hint[1]:.5f}",
                "--radius", f"{radius:.2f}"]
    cmd.append(str(path))
    global LAST_LOG
    try:
        # cwd no diretório temporário: o solve-field escreve alguns arquivos
        # relativos ao diretório corrente independentemente do -D, e sem isto
        # eles ficam largados na raiz do projeto
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout + 30, cwd=str(path.parent))
        LAST_LOG = "\n".join((r.stdout or "").strip().splitlines()[-12:])
    except subprocess.TimeoutExpired:
        LAST_LOG = f"solve-field excedeu {timeout + 30:.0f}s"
        return None
    except OSError as e:
        LAST_LOG = f"solve-field não executou: {e}"
        return None
    return _from_wcs_file(out / (path.stem + ".wcs"))


def _solve_astap(exe, path: Path, scale, tol, hint, radius, timeout, downsample):
    cmd = [exe, "-f", str(path), "-r", f"{radius:.2f}", "-wcs"]
    if scale:
        with fits.open(path) as h:
            w = h[0].data.shape[1]
        cmd += ["-fov", f"{scale * w / 3600.0:.4f}"]
    if hint:
        cmd += ["-ra", f"{hint[0]/15.0:.6f}", "-spd", f"{hint[1]+90.0:.6f}"]
    try:
        subprocess.run(cmd, capture_output=True, timeout=timeout + 20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return _from_wcs_file(path.with_suffix(".wcs"))


def _from_wcs_file(p: Path) -> Solution | None:
    """Lê o .wcs produzido pelo solver.

    Tanto o astrometry.net quanto o ASTAP escrevem um FITS só de cabeçalho.
    Uma versão anterior tentava adivinhar se o arquivo era texto e classificava
    errado, caindo no leitor de texto e falhando em silêncio — daí a tentativa
    com getheader primeiro e o texto apenas como alternativa.
    """
    if not p.exists() or p.stat().st_size == 0:
        return None
    hdr = None
    for reader in (lambda: fits.getheader(p),
                   lambda: fits.Header.fromtextfile(str(p))):
        try:
            hdr = reader()
            break
        except Exception:
            continue
    if hdr is None:
        return None
    try:
        from astropy.wcs import WCS
        w = WCS(hdr)
        h = int(hdr.get("IMAGEH") or hdr.get("NAXIS2") or 0)
        wid = int(hdr.get("IMAGEW") or hdr.get("NAXIS1") or 0)
        return from_wcs(w, (h, wid))
    except Exception:
        return None


def from_wcs(w, shape: tuple[int, int]) -> Solution:
    """Extrai centro, escala, orientação e paridade de um WCS já pronto.

    Também usado nos testes, com um WCS sintético — o que permite validar
    anotação e push-to sem nenhum solver instalado.
    """
    h, wid = shape
    cx, cy = (wid / 2.0 if wid else 0.0), (h / 2.0 if h else 0.0)
    ra, dec = [float(v) for v in w.all_pix2world([[cx, cy]], 0)[0]]
    cd = w.pixel_scale_matrix
    scale = float(np.sqrt(abs(np.linalg.det(cd))) * 3600.0)
    orientation = float(np.degrees(np.arctan2(cd[0, 1], cd[1, 1])) % 360.0)
    flipped = bool(np.linalg.det(cd) > 0)
    fov = (scale * wid / 3600.0, scale * h / 3600.0) if wid else (0.0, 0.0)
    return Solution(ra, dec, scale, orientation, flipped, fov, "wcs", 0.0, w)


def synthetic_wcs(ra: float, dec: float, scale_arcsec: float,
                  shape: tuple[int, int], rotation_deg: float = 0.0):
    """WCS gaussiano-tangente para testes e para simular uma solução."""
    from astropy.wcs import WCS
    h, w = shape
    d = scale_arcsec / 3600.0
    th = np.deg2rad(rotation_deg)
    wc = WCS(naxis=2)
    wc.wcs.crpix = [w / 2.0, h / 2.0]
    wc.wcs.crval = [ra, dec]
    wc.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wc.wcs.cd = np.array([[-d * np.cos(th), d * np.sin(th)],
                          [d * np.sin(th), d * np.cos(th)]])
    return wc
