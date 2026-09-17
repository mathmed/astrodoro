from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "ASTRODORO_CONFIG_DIR", tempfile.mkdtemp(prefix="astrodoro-test-config-")
)
os.environ.setdefault(
    "ASTRODORO_DATA_DIR", tempfile.mkdtemp(prefix="astrodoro-test-data-")
)
os.environ.setdefault(
    "ASTRODORO_CATALOG", str(Path(__file__).parent / "data" / "ngc-sample.csv")
)

import pytest


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def settings(tmp_path):
    from astrodoro.settings import Settings

    s = Settings.load(tmp_path / "settings.json")
    s.capture_dir = str(tmp_path / "sessions")
    s.bias_dir = str(tmp_path / "bias")
    s.dark_dir = str(tmp_path / "darks")
    s.flat_dir = str(tmp_path / "flats")
    s.export_dir = str(tmp_path / "exports")
    s.latitude, s.longitude, s.elevation_m = -6.7003, -36.9436, 350.0
    s.site_set = True
    return s
