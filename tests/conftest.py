"""Shared test setup.

Every GUI test needs `QT_QPA_PLATFORM=offscreen` set *before* Qt is imported, and
none of them should read or write the developer's real settings file. Both are
handled here so no individual test has to remember.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("ASTRODORO_CONFIG_DIR",
                      tempfile.mkdtemp(prefix="astrodoro-test-config-"))
os.environ.setdefault("ASTRODORO_DATA_DIR",
                      tempfile.mkdtemp(prefix="astrodoro-test-data-"))

import pytest


@pytest.fixture(scope="session")
def qapp():
    """One QApplication for the whole session — Qt allows only one."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def settings(tmp_path):
    """Default settings backed by a throwaway file."""
    from astrodoro.settings import Settings
    s = Settings.load(tmp_path / "settings.json")
    s.capture_dir = str(tmp_path / "sessions")
    s.dark_dir = str(tmp_path / "darks")
    s.flat_dir = str(tmp_path / "flats")
    s.export_dir = str(tmp_path / "exports")
    return s
