from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "astrodoro"


def config_dir() -> Path:
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
    root = Path(__file__).resolve().parents[2]
    return root / "data" if (root / "pyproject.toml").is_file() else None


def _default_workspace() -> Path:
    return Path.home() / "Astrodoro"


SETTINGS_FILE = "settings.json"


@dataclass
class Settings:
    capture_dir: str = ""
    bias_dir: str = ""
    dark_dir: str = ""
    flat_dir: str = ""
    export_dir: str = ""

    latitude: float = 0.0
    longitude: float = 0.0
    elevation_m: float = 0.0
    site_set: bool = False

    align_minutes: float = 6.0

    focal_length_mm: float = 1200.0
    pixel_size_um: float = 4.63
    sensor_width: int = 4144
    sensor_height: int = 2822

    language: str = "en"
    theme: str = "dark"
    night_level: int = 1
    large_targets: bool = False

    target_min_alt: float = 25.0
    target_max_mag: float = 12.0
    target_family: str = "all"
    target_fits_only: bool = False
    previews_enabled: bool = True

    exposure_s: float = 5.0
    gain: int = 250
    offset: int = 20
    binning: int = 2
    handset_port: int = 8443

    lucky_exposure_s: float = 0.008
    lucky_gain: int = 100
    lucky_binning: int = 1
    lucky_body: str = "moon"
    lucky_burst_seconds: float = 30.0
    lucky_burst_frames: int = 0
    lucky_burst_compress: bool = False
    lucky_display_fps: float = 12.0
    lucky_follow: bool = True
    lucky_gamma: float = 0.75
    lucky_wb_red: float = 1.0
    lucky_wb_blue: float = 1.0
    lucky_saturation: float = 1.0

    _path: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        workspace = _default_workspace()
        for name, default in (
            ("capture_dir", workspace / "sessions"),
            ("bias_dir", workspace / "bias"),
            ("dark_dir", workspace / "darks"),
            ("flat_dir", workspace / "flats"),
            ("export_dir", workspace / "exports"),
        ):
            if not getattr(self, name):
                setattr(self, name, str(default))

    def has_site(self) -> bool:
        return bool(self.site_set)

    def path(self, name: str, create: bool = False) -> Path:
        p = Path(str(getattr(self, name))).expanduser()
        if create:
            p.mkdir(parents=True, exist_ok=True)
        return p

    def catalog_path(self) -> Path:
        env = os.environ.get("ASTRODORO_CATALOG")
        if env:
            return Path(env).expanduser()
        repo = repo_data_dir()
        if repo is not None:
            return repo / "NGC.csv"
        return data_dir() / "NGC.csv"

    def pixel_scale(self, binning: int | None = None, halved: bool = False) -> float:
        b = self.binning if binning is None else binning
        s = 206.265 * self.pixel_size_um / self.focal_length_mm * b
        return s * 2 if halved else s

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
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
        payload = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        p.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        self._path = p
        return p
