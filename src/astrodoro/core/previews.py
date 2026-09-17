from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import __version__

SERVICE = "https://alasky.u-strasbg.fr/hips-image-services/hips2fits"

HIPS = "CDS/P/DSS2/color"

SIZE = 512

USER_AGENT = f"astrodoro/{__version__} (+https://github.com/mathmed/astrodoro)"

DEFAULT_TIMEOUT = 8.0


def cache_dir() -> Path:
    from ..settings import data_dir

    return data_dir() / "previews"


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "object"


def cache_path(name: str, fov_arcmin: float, root: Path | None = None) -> Path:
    root = root or cache_dir()
    return root / f"{_slug(name)}_{fov_arcmin:.0f}arcmin.jpg"


def cached(name: str, fov_arcmin: float, root: Path | None = None) -> Path | None:
    p = cache_path(name, fov_arcmin, root)
    return p if p.is_file() and p.stat().st_size > 0 else None


def url_for(ra: float, dec: float, fov_arcmin: float, size: int = SIZE) -> str:
    query = urllib.parse.urlencode(
        {
            "hips": HIPS,
            "ra": f"{ra:.6f}",
            "dec": f"{dec:.6f}",
            "fov": f"{fov_arcmin / 60.0:.6f}",
            "width": size,
            "height": size,
            "projection": "TAN",
            "coordsys": "icrs",
            "format": "jpg",
        }
    )
    return f"{SERVICE}?{query}"


def fetch(
    name: str,
    ra: float,
    dec: float,
    fov_arcmin: float,
    root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    size: int = SIZE,
) -> Path | None:
    hit = cached(name, fov_arcmin, root)
    if hit is not None:
        return hit
    path = cache_path(name, fov_arcmin, root)
    req = urllib.request.Request(
        url_for(ra, dec, fov_arcmin, size), headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.headers.get_content_type() != "image/jpeg":
                return None
            data = r.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if len(data) < 1024:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        return None
    return path


def frame_fraction(
    fov_frame_arcmin: tuple[float, float], fov_cutout_arcmin: float
) -> tuple[float, float]:
    w, h = fov_frame_arcmin
    return w / fov_cutout_arcmin, h / fov_cutout_arcmin


MIN_FOV_ARCMIN = 10.0
MAX_FOV_ARCMIN = 300.0


def cutout_fov(
    object_arcmin: float | None, fov_frame_arcmin: tuple[float, float]
) -> float:
    size = object_arcmin if object_arcmin and object_arcmin > 0 else 0.0
    want = max(size * 2.2, MIN_FOV_ARCMIN)
    if size > min(fov_frame_arcmin):
        want = max(want, max(fov_frame_arcmin) * 1.3)
    want = min(want, MAX_FOV_ARCMIN)
    return max(MIN_FOV_ARCMIN, round(want / 5.0) * 5.0)
