"""Settings persistence and the derived values that depend on it."""
from __future__ import annotations

from astrodoro.settings import Settings


def test_defaults_fill_in_the_output_folders(tmp_path):
    s = Settings.load(tmp_path / "missing.json")
    for name in ("capture_dir", "dark_dir", "flat_dir", "export_dir"):
        assert getattr(s, name), f"{name} left empty"


def test_a_corrupt_file_falls_back_to_defaults(tmp_path):
    """Never raises: it would do so in the dark, in the field."""
    p = tmp_path / "settings.json"
    p.write_text("{ this is not json")
    assert Settings.load(p).gain == Settings().gain


def test_unknown_keys_are_ignored(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"gain": 321, "from_a_future_version": true}')
    s = Settings.load(p)
    assert s.gain == 321
    assert not hasattr(s, "from_a_future_version")


def test_roundtrip(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings.load(p)
    s.capture_dir = str(tmp_path / "sessions")
    s.gain = 199
    s.language = "pt_BR"
    s.save()
    again = Settings.load(p)
    assert again.gain == 199
    assert again.language == "pt_BR"
    assert again.capture_dir == str(tmp_path / "sessions")


def test_pixel_scale_follows_the_optics():
    s = Settings(focal_length_mm=1200.0, pixel_size_um=4.63)
    assert abs(s.pixel_scale(1) - 0.7958) < 1e-3
    assert abs(s.pixel_scale(2) - 2 * s.pixel_scale(1)) < 1e-9
    # The luminance image is half resolution, so twice the scale.
    assert abs(s.pixel_scale(2, halved=True) - 2 * s.pixel_scale(2)) < 1e-9


def test_path_creates_on_demand(tmp_path):
    s = Settings.load(tmp_path / "settings.json")
    s.capture_dir = str(tmp_path / "deep" / "sessions")
    p = s.path("capture_dir", create=True)
    assert p.is_dir()
