"""Survey cutouts of a catalogue object — what the target looks like.

A list of names does not answer the question anyone actually has in front of a
target: *is this worth an hour, and will it fit?* A DSS thumbnail with the
sensor's rectangle drawn on it answers both in one glance.

Three constraints shape this module, and all three come from where the program
runs:

- **there is no internet in the field.** So the disk cache is not an
  optimisation, it is the feature: what was fetched at home is what you have
  under the sky. Every read goes to the cache first and the network is optional;
- **nothing may block the interface.** The fetch is done off the Qt thread by
  the caller (`ui/previews.py`); this module only knows files and HTTP;
- **no new dependency.** `urllib` from the standard library, one request, one
  timeout, no retry loop.

Source: the CDS `hips2fits` service over the DSS2 colour HiPS. Attribution is in
`docs/third-party.md` and on screen next to the image.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .. import __version__

#: CDS image service. Renders any HiPS survey to a JPEG at the requested field.
SERVICE = "https://alasky.u-strasbg.fr/hips-image-services/hips2fits"

#: DSS2 colour: the whole sky, red plus blue plates, deep enough that anything
#: this program can image at all is visible on it.
HIPS = "CDS/P/DSS2/color"

#: Side of the cutout in pixels. 512 is ~30 KB per object as JPEG: a hundred
#: targets cached for a night out is 3 MB.
SIZE = 512

#: Politeness, and how the CDS sees this program in its logs.
USER_AGENT = f"astrodoro/{__version__} (+https://github.com/mathmed/astrodoro)"

DEFAULT_TIMEOUT = 8.0


def cache_dir() -> Path:
    from ..settings import data_dir
    return data_dir() / "previews"


def _slug(name: str) -> str:
    """A file name that survives every catalogue label, without collisions."""
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "object"


def cache_path(name: str, fov_arcmin: float, root: Path | None = None) -> Path:
    """Where this object's cutout lives, keyed by name *and* field.

    The field is part of the key because the same object at two zoom levels is
    two different pictures, and because changing the optics has to invalidate
    the old framing rather than quietly show it.
    """
    root = root or cache_dir()
    return root / f"{_slug(name)}_{fov_arcmin:.0f}arcmin.jpg"


def cached(name: str, fov_arcmin: float, root: Path | None = None) -> Path | None:
    """The cutout already on disk, or None. Never touches the network."""
    p = cache_path(name, fov_arcmin, root)
    # A zero-length file is a fetch that died mid-write; treat it as absent.
    return p if p.is_file() and p.stat().st_size > 0 else None


def url_for(ra: float, dec: float, fov_arcmin: float, size: int = SIZE) -> str:
    query = urllib.parse.urlencode({
        "hips": HIPS, "ra": f"{ra:.6f}", "dec": f"{dec:.6f}",
        "fov": f"{fov_arcmin / 60.0:.6f}",          # the service wants degrees
        "width": size, "height": size,
        "projection": "TAN", "coordsys": "icrs", "format": "jpg",
    })
    return f"{SERVICE}?{query}"


def fetch(name: str, ra: float, dec: float, fov_arcmin: float,
          root: Path | None = None, timeout: float = DEFAULT_TIMEOUT,
          size: int = SIZE) -> Path | None:
    """Download the cutout unless it is already cached. None if it cannot be had.

    Never raises: no network, a service in maintenance or a corporate portal
    returning HTML are all "no picture tonight", which is a state the interface
    has to handle anyway.
    """
    hit = cached(name, fov_arcmin, root)
    if hit is not None:
        return hit
    path = cache_path(name, fov_arcmin, root)
    req = urllib.request.Request(url_for(ra, dec, fov_arcmin, size),
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.headers.get_content_type() != "image/jpeg":
                return None
            data = r.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if len(data) < 1024:            # an error page dressed as an image
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename: a fetch interrupted by closing the
        # program must not leave a truncated JPEG that then caches forever.
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        return None
    return path


def frame_fraction(fov_frame_arcmin: tuple[float, float],
                   fov_cutout_arcmin: float) -> tuple[float, float]:
    """The sensor rectangle as a fraction of the cutout, for drawing it on top."""
    w, h = fov_frame_arcmin
    return w / fov_cutout_arcmin, h / fov_cutout_arcmin


#: Smallest and largest cutout. The floor keeps a planetary nebula from being
#: three pixels; the ceiling stops a request for six degrees of DSS, which
#: arrives as a grey smear with no detail at any zoom.
MIN_FOV_ARCMIN = 10.0
MAX_FOV_ARCMIN = 300.0


def cutout_fov(object_arcmin: float | None,
               fov_frame_arcmin: tuple[float, float]) -> float:
    """How wide a cutout to ask for, in arcminutes.

    Framed on the **object**, not on the sensor. Sizing the cutout to the frame
    was the first attempt and it made every small target useless: M57 is 1.3'
    inside a 51' field, so the picture was a black square with a dot. The
    sensor's rectangle is then drawn on top when it fits, and when it does not
    the answer — "it fits with room to spare" — needs no drawing.

    A little over twice the object, so its surroundings are visible: half of
    judging a target is what else falls in the frame.
    """
    size = object_arcmin if object_arcmin and object_arcmin > 0 else 0.0
    want = max(size * 2.2, MIN_FOV_ARCMIN)
    if size > min(fov_frame_arcmin):
        # Bigger than the frame: the picture has to show where the sensor lands
        # inside it, so make sure the rectangle has room.
        want = max(want, max(fov_frame_arcmin) * 1.3)
    want = min(want, MAX_FOV_ARCMIN)
    # Rounded to 5', so a nudge in binning or in the catalogue's size does not
    # earn its own cache entry.
    return max(MIN_FOV_ARCMIN, round(want / 5.0) * 5.0)
