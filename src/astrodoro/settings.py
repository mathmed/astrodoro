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
    bias_dir: str = ""
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

    # --- equatorial platform ------------------------------------------------
    #: Sign relating the rotation registration measures on the sensor to the
    #: rotation of the sky. A property of the optical train — an odd number of
    #: reflections mirrors the image and flips it — and therefore constant for
    #: a setup. It is settled by measuring, not by counting mirrors: the align
    #: procedure re-measures after a correction and says to invert this when
    #: the residual rotation grew instead of falling.
    platform_parity: int = 1
    #: Minutes each alignment station measures for. The rotation to be measured
    #: is thousandths of a degree per minute, and what buys precision is the
    #: baseline: doubling this halves the uncertainty, doubling the frame rate
    #: only helps by the square root.
    align_minutes: float = 5.0

    # --- optics -------------------------------------------------------------
    #: Used for the arcsec/px scale and the sensor rectangle on the sky map.
    focal_length_mm: float = 1200.0
    pixel_size_um: float = 4.63
    #: Sensor in pixels, at bin1. It is what the field of view is computed from
    #: *before* a capture opens — which is exactly when the field matters, since
    #: that is when you decide whether the target fits. Defaults to the
    #: SV405CC's IMX294 and is corrected from the camera on every open, so any
    #: other camera is right from its second session onwards.
    sensor_width: int = 4144
    sensor_height: int = 2822

    # --- interface ----------------------------------------------------------
    language: str = "en"
    theme: str = "dark"          # "dark" | "night"
    night_level: int = 1         # 0..2, only meaningful for the night theme
    large_targets: bool = False  # bigger click targets for gloved fingers

    # --- target suggestions -------------------------------------------------
    # What the TARGETS list is willing to suggest. The minimum altitude is the
    # one that depends on the site rather than on taste: a garden with a wall to
    # the east has a different floor from an open field.
    target_min_alt: float = 25.0
    target_max_mag: float = 12.0
    target_family: str = "all"
    #: Hide anything larger than the frame. Off by default — a slice of a large
    #: nebula is still worth a night.
    target_fits_only: bool = False
    #: Fetch DSS thumbnails of the suggestions. On by default, but every read
    #: goes to the disk cache first: in the field there is no internet, and what
    #: was fetched at home is what you have.
    previews_enabled: bool = True

    # --- capture defaults ---------------------------------------------------
    exposure_s: float = 5.0
    gain: int = 250
    offset: int = 20
    binning: int = 2
    handset_port: int = 8443

    # --- Moon and planets ---------------------------------------------------
    # Their own set of capture defaults, because none of the deep-sky ones
    # survive contact with a target eight magnitudes brighter than anything
    # else in the sky: milliseconds instead of seconds, low gain because there
    # is signal to spare, and bin1 because the whole point is resolution.
    #
    # These three are the *Moon's*. Every other body is scaled off them by
    # `lucky.exposure_for`, from the ratio of surface brightnesses — one number
    # to keep true instead of eight, and the only part of an exposure that
    # transfers between two targets under the same optics.
    lucky_exposure_s: float = 0.008
    lucky_gain: int = 100
    lucky_binning: int = 1
    #: Which body the panel opens on. The Moon, because it is what a night with
    #: this mode usually starts with and the only one that needs no seeking.
    lucky_body: str = "moon"
    #: Length of one burst. Thirty seconds is the usual compromise: long enough
    #: for a few hundred frames to pick from, short enough that the field's
    #: rotation on an equatorial platform does not matter.
    lucky_burst_seconds: float = 30.0
    #: Hard cap on frames per burst, whichever limit comes first. 0 = no cap.
    lucky_burst_frames: int = 0
    #: RICE on the burst. Off by default, unlike a deep-sky session: measured at
    #: bin1 it costs 119 ms a frame against 32 ms, which caps the burst at 8 fps
    #: instead of 31. Lucky imaging is bought in frames, and the disc is a third
    #: of the frame, so the 40% the compression saves is not worth 4x fewer.
    lucky_burst_compress: bool = False
    #: How often the view is redrawn, at most. The measurement costs 35 ms a
    #: frame and the display 120 ms; without a ceiling the queue between the
    #: threads grows by one 140 MB frame for each one the GUI cannot absorb.
    lucky_display_fps: float = 12.0
    #: Whether the view follows the body. Display only — nothing about the
    #: recorded frames changes, and the stack still aligns them afterwards. On
    #: by default: at the magnification a planet needs, wind and seeing move it
    #: across a quarter of the screen, which makes focusing by eye impossible
    #: and reads as a mount problem.
    lucky_follow: bool = True
    #: Display exponent. Not an autostretch: a bright body has no faint signal
    #: to lift, and a stretch built for a nebula turns the maria into flat grey.
    lucky_gamma: float = 0.75
    #: White balance of the view, green fixed at 1.0 — it has twice the
    #: photosites and is the least noisy of the three. Persisted because it is a
    #: property of the sensor and the sky, not of tonight: `_force_linear()`
    #: disables the camera's own white balance on every open, so the raw channel
    #: response is the same every night.
    lucky_wb_red: float = 1.0
    lucky_wb_blue: float = 1.0
    #: Chroma gain of the view. 1.0 is what the sensor saw; the "mineral Moon"
    #: is this at 2-3, which is real colour — titanium in the blue maria, iron
    #: oxide in the orange ones — amplified, not invented. A planet wants it too:
    #: Jupiter's belts are a few percent apart in hue.
    lucky_saturation: float = 1.0

    _path: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ paths
    def __post_init__(self) -> None:
        workspace = _default_workspace()
        for name, default in (("capture_dir", workspace / "sessions"),
                              ("bias_dir", workspace / "bias"),
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
