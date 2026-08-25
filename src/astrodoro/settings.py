"""User settings, persisted as JSON outside the repository.

Everything a user might reasonably want to change lives here rather than in the
source: where captures are written, the observing site, the optics, the
language and the display preferences. Both the GUI and the CLI read the same
file, so a folder configured once applies everywhere.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "astrodoro"


def config_dir() -> Path:
    """Where settings are stored. `ASTRODORO_CONFIG_DIR` overrides it."""
    override = os.environ.get("ASTRODORO_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def data_dir() -> Path:
    """Generated data that is neither settings nor captures: catalog, TLS cert."""
    override = os.environ.get("ASTRODORO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME / "data"
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME / "data"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def repo_data_dir() -> Path | None:
    """`data/` at the root of a source checkout, or None when installed.

    Detected by the presence of `pyproject.toml` two levels above the package,
    rather than by `data/` already existing: `astrodoro catalog` needs to know
    where to *create* it. A development tree therefore keeps everything inside
    the checkout, and an installed copy uses the user data directory.
    """
    root = Path(__file__).resolve().parents[2]
    return root / "data" if (root / "pyproject.toml").is_file() else None


def _default_workspace() -> Path:
    return Path.home() / "Astrodoro"


SETTINGS_FILE = "settings.json"


@dataclass
class Settings:
    """Every user-tunable value, with the defaults of the setup it grew from."""

    # --- where files go -----------------------------------------------------
    #: Session root. `Recorder` writes `<capture_dir>/YYYY-MM-DD/HHMM_target/`.
    capture_dir: str = ""
    dark_dir: str = ""
    flat_dir: str = ""
    #: Fallback for "save what is on screen" when no session is recording.
    export_dir: str = ""

    # --- observing site -----------------------------------------------------
    # Latitude feeds straight into the altitude of the celestial pole, so an
    # error here becomes an equal error in the polar alignment correction:
    # 0.1 deg of latitude is 6 arcmin of correction.
    latitude: float = -6.7003
    longitude: float = -36.9436
    elevation_m: float = 350.0

    # --- optics -------------------------------------------------------------
    #: Used for the arcsec/px scale and the sensor rectangle on the sky map.
    focal_length_mm: float = 1200.0
    pixel_size_um: float = 4.63

    # --- interface ----------------------------------------------------------
    language: str = "en"
    theme: str = "dark"          # "dark" | "night"
    night_level: int = 1         # 0..2, only meaningful for the night theme
    large_targets: bool = False  # bigger click targets for gloved fingers

    # --- capture defaults ---------------------------------------------------
    exposure_s: float = 5.0
    gain: int = 250
    offset: int = 20
    binning: int = 2
    handset_port: int = 8443

    _path: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ paths
    def __post_init__(self) -> None:
        workspace = _default_workspace()
        for name, default in (("capture_dir", workspace / "sessions"),
                              ("dark_dir", workspace / "darks"),
                              ("flat_dir", workspace / "flats"),
                              ("export_dir", workspace / "exports")):
            if not getattr(self, name):
                setattr(self, name, str(default))

    def path(self, name: str, create: bool = False) -> Path:
        """One of the directory settings as a `Path`, optionally created."""
        p = Path(str(getattr(self, name))).expanduser()
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    def catalog_path(self) -> Path:
        """Where `NGC.csv` lives. Checkout first, then the user data directory.

        `astrodoro catalog` downloads to exactly this path, so the lookup and
        the download can never disagree.
        """
        env = os.environ.get("ASTRODORO_CATALOG")
        if env:
            return Path(env).expanduser()
        repo = repo_data_dir()
        if repo is not None:
            return repo / "NGC.csv"
        return data_dir() / "NGC.csv"

    def pixel_scale(self, binning: int | None = None,
                    halved: bool = False) -> float:
        """Arcsec per pixel.

        `halved` is for the luminance image, which is half the frame resolution
        (each Bayer 2x2 quad summed) and therefore twice the scale.
        """
        b = self.binning if binning is None else binning
        s = 206.265 * self.pixel_size_um / self.focal_length_mm * b
        return s * 2 if halved else s

    # ------------------------------------------------------------- persistence
    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        """Read settings, falling back to defaults for anything missing.

        Never raises: a corrupt or hand-edited file must not stop the program
        from opening, because it would do so in the dark, in the field.
        """
        p = path or (config_dir() / SETTINGS_FILE)
        known = {f.name for f in fields(cls) if not f.name.startswith("_")}
        data: dict = {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = {k: v for k, v in raw.items() if k in known}
        except (OSError, ValueError):
            data = {}
        out = cls(**data)
        out._path = p
        return out

    def save(self, path: Path | None = None) -> Path:
        p = path or self._path or (config_dir() / SETTINGS_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: v for k, v in asdict(self).items()
                   if not k.startswith("_")}
        p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
        self._path = p
        return p
