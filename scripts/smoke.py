"""Boot smoke: start the real entrypoints and confirm they come up ready.

    python scripts/smoke.py          # what `make smoke` and CI run

Astrodoro has no HTTP API, so there is no /health or /ready to call. The
equivalent signals, in order, each against an isolated config, data and capture
folder that is thrown away at the end:

    liveness    `astrodoro --version` exits 0 and names the package version
    config      `astrodoro settings --set ...` writes a realistic settings file
    pipeline    a recorded session goes through `astrodoro replay` and comes out
                as a stack_final.fits combining the frames
    gui         the `astrodoro-gui` entry point shows its window and runs its
                event loop offscreen without a traceback

No camera, no phone and no network: the replay source stands in for the camera
and the catalogue comes from tests/data, as in the test suite.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

from astrodoro import __version__
from astrodoro.core.recorder import Recorder
from astrodoro.core.source import FrameMeta
from astrodoro.drivers.base import Bayer

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "tests" / "data" / "ngc-sample.csv"

_CLI_TIMEOUT_S = 60
_REPLAY_TIMEOUT_S = 300
_GUI_TIMEOUT_S = 120
_GUI_READY_DEADLINE_MS = 60_000
_GUI_POLL_MS = 250
_GUI_SETTLE_MS = 1_500
_GUI_NOT_READY_EXIT = 3
_TRACEBACK = "Traceback (most recent call last)"

_FRAMES = 8
_WIDTH, _HEIGHT = 1200, 800
_STARS = 220
_SKY_ADU = 800.0
_PSF_SIGMA_PX = 2.2
_DRIFT_PX = (1.6, -1.1)
_FULL_SCALE = 65532
_MIN_STACKED = _FRAMES - 2
_SEED = 7

_SITE = {"latitude": "-6.7003", "longitude": "-36.9436", "elevation_m": "350"}

_GUI_PROBE = f"""
import sys
from importlib.metadata import entry_points

from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)
clock = QElapsedTimer()
clock.start()


def poll():
    shown = [w for w in app.topLevelWidgets() if w.isVisible()]
    if shown and clock.elapsed() >= {_GUI_SETTLE_MS}:
        print(f"ready: {{type(shown[0]).__name__}} shown", flush=True)
        app.exit(0)
    elif clock.elapsed() > {_GUI_READY_DEADLINE_MS}:
        print("not ready: no window shown", flush=True)
        app.exit({_GUI_NOT_READY_EXIT})
    else:
        QTimer.singleShot({_GUI_POLL_MS}, poll)


QTimer.singleShot({_GUI_POLL_MS}, poll)
(gui,) = entry_points(group="gui_scripts", name="astrodoro-gui")
sys.exit(gui.load()())
"""


class SmokeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "ASTRODORO_CONFIG_DIR": str(self.root / "config"),
            "ASTRODORO_DATA_DIR": str(self.root / "data"),
            "ASTRODORO_CATALOG": str(CATALOG),
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONUNBUFFERED": "1",
        }


def cli_executable() -> str:
    found = shutil.which("astrodoro", path=str(Path(sys.executable).parent))
    if found is None:
        raise SmokeError(
            "the `astrodoro` entry point is not installed next to "
            f"{sys.executable}; run `make setup` or `pip install -e .`"
        )
    return found


def run(
    step: str, argv: list[str], ws: Workspace, timeout: float
) -> subprocess.CompletedProcess[str]:
    t0 = time.perf_counter()
    try:
        done = subprocess.run(
            argv,
            env=ws.env(),
            cwd=ws.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise SmokeError(
            f"{step}: no answer after {timeout:.0f} s\n{_logs(e.stdout, e.stderr)}"
        ) from e
    if done.returncode != 0:
        raise SmokeError(
            f"{step}: exit code {done.returncode}\n{_logs(done.stdout, done.stderr)}"
        )
    if _TRACEBACK in done.stderr:
        raise SmokeError(
            f"{step}: exited 0 but raised along the way\n"
            f"{_logs(done.stdout, done.stderr)}"
        )
    print(f"  ok  {step} ({time.perf_counter() - t0:.1f} s)")
    return done


def _logs(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    def text(v: str | bytes | None) -> str:
        if isinstance(v, bytes):
            return v.decode(errors="replace")
        return v or ""

    return f"--- stdout\n{text(stdout)}\n--- stderr\n{text(stderr)}"


def check_liveness(cli: str, ws: Workspace) -> None:
    done = run("liveness: astrodoro --version", [cli, "--version"], ws, _CLI_TIMEOUT_S)
    if __version__ not in done.stdout:
        raise SmokeError(
            f"liveness: expected version {__version__}, got {done.stdout!r}"
        )


def write_settings(cli: str, ws: Workspace) -> None:
    values = {
        **_SITE,
        "site_set": "true",
        "capture_dir": str(ws.sessions),
        "export_dir": str(ws.exports),
        "bias_dir": str(ws.root / "bias"),
        "dark_dir": str(ws.root / "darks"),
        "flat_dir": str(ws.root / "flats"),
        "previews_enabled": "false",
    }
    argv = [cli, "settings"]
    for key, value in values.items():
        argv += ["--set", key, value]
    run("config: astrodoro settings --set ...", argv, ws, _CLI_TIMEOUT_S)
    if not (ws.root / "config" / "settings.json").is_file():
        raise SmokeError("config: settings.json was not written")


def render_frame(k: int, rng: np.random.Generator, stars: np.ndarray) -> np.ndarray:
    xy = stars[:, :2] + np.asarray(_DRIFT_PX) * k
    yy, xx = np.mgrid[0:_HEIGHT, 0:_WIDTH].astype(np.float32)
    img = np.full((_HEIGHT, _WIDTH), _SKY_ADU, dtype=np.float32)
    for (x, y), flux in zip(xy, stars[:, 2], strict=True):
        x0, x1 = max(int(x) - 8, 0), min(int(x) + 9, _WIDTH)
        y0, y1 = max(int(y) - 8, 0), min(int(y) + 9, _HEIGHT)
        dx, dy = xx[y0:y1, x0:x1] - x, yy[y0:y1, x0:x1] - y
        img[y0:y1, x0:x1] += flux * np.exp(-(dx**2 + dy**2) / (2 * _PSF_SIGMA_PX**2))
    noisy = rng.poisson(np.clip(img, 0, None)).astype(np.float64)
    return np.clip(noisy, 0, _FULL_SCALE).astype(np.uint16)


def record_session(ws: Workspace) -> Path:
    rng = np.random.default_rng(_SEED)
    margin = 40
    stars = np.column_stack(
        [
            rng.uniform(margin, _WIDTH - margin, _STARS),
            rng.uniform(margin, _HEIGHT - margin, _STARS),
            10 ** rng.uniform(3.0, 4.4, _STARS),
        ]
    )
    rec = Recorder(root=ws.sessions, target="smoke")
    info = {"name": "smoke", "pixel_um": 4.63}
    folder = rec.begin(info, {"exposure": 5.0, "gain": 250})
    t0 = time.time()
    for k in range(_FRAMES):
        meta = FrameMeta(
            index=k + 1,
            timestamp=t0 + 5.0 * k,
            exposure=5.0,
            gain=250,
            offset=20,
            bin=2,
            full_scale=_FULL_SCALE,
            bayer=Bayer.GR,
        )
        rec.write_sub(render_frame(k, rng, stars), meta)
    rec.end({"n_stacked": 0})
    return folder


def check_pipeline(cli: str, ws: Workspace) -> None:
    folder = record_session(ws)
    run(
        "pipeline: astrodoro replay <recorded session>",
        [cli, "replay", str(folder), "--out", str(ws.exports)],
        ws,
        _REPLAY_TIMEOUT_S,
    )
    result = ws.exports / "stack_final.fits"
    if not result.is_file():
        raise SmokeError(f"pipeline: {result} was not written")
    header = fits.getheader(result)
    stacked = int(header.get("NCOMBINE", 0))
    if stacked < _MIN_STACKED:
        raise SmokeError(
            f"pipeline: only {stacked} of {_FRAMES} frames stacked "
            f"(at least {_MIN_STACKED} expected)"
        )
    print(f"  info  pipeline: {stacked} of {_FRAMES} frames stacked")


def check_gui(ws: Workspace) -> None:
    done = run(
        "gui: astrodoro-gui entry point, offscreen",
        [sys.executable, "-c", _GUI_PROBE],
        ws,
        _GUI_TIMEOUT_S,
    )
    if "ready:" not in done.stdout:
        raise SmokeError(f"gui: no readiness line\n{_logs(done.stdout, done.stderr)}")


def main() -> int:
    argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0]).parse_args()
    print(f"smoke: astrodoro {__version__} on {sys.executable}")
    try:
        cli = cli_executable()
        with tempfile.TemporaryDirectory(prefix="astrodoro-smoke-") as tmp:
            ws = Workspace(Path(tmp))
            check_liveness(cli, ws)
            write_settings(cli, ws)
            check_pipeline(cli, ws)
            check_gui(ws)
    except SmokeError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1
    print("smoke: ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
