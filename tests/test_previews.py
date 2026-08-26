"""Survey cutouts: the cache, the framing decision, and failing quietly.

No test here touches the network. That is not only for speed — the module's
whole contract is that the network is optional, so what has to be exercised is
the cache and every way a fetch can fail.
"""
from __future__ import annotations

import io
import urllib.error

import pytest

from astrodoro.core import previews

FRAME = (51.0, 29.0)          # the SV405CC at 1200 mm, bin2


class _Response(io.BytesIO):
    """The two things `fetch` reads out of urlopen."""

    def __init__(self, data: bytes, content_type: str = "image/jpeg"):
        super().__init__(data)
        self.headers = _Headers(content_type)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Headers:
    def __init__(self, content_type: str):
        self._t = content_type

    def get_content_type(self) -> str:
        return self._t


def _jpeg(size: int = 4096) -> bytes:
    return b"\xff\xd8\xff\xe0" + b"x" * size


# ------------------------------------------------------------------- framing
def test_a_small_object_gets_a_small_cutout():
    """Sizing the cutout to the frame was the first attempt: M57 is 1.3' in a
    51' field, and the picture came out a black square with a dot in it."""
    assert previews.cutout_fov(1.3, FRAME) == previews.MIN_FOV_ARCMIN
    assert previews.cutout_fov(10.0, FRAME) < max(FRAME)


def test_an_object_larger_than_the_frame_leaves_room_for_the_rectangle():
    fov = previews.cutout_fov(45.0, FRAME)
    assert fov >= max(FRAME) * 1.3
    w, h = previews.frame_fraction(FRAME, fov)
    assert w < 1.0 and h < 1.0, "the frame has to fit inside the picture"


def test_the_cutout_never_asks_for_six_degrees_of_survey():
    """Past a few degrees the DSS arrives as a grey smear with no detail."""
    assert previews.cutout_fov(600.0, FRAME) == previews.MAX_FOV_ARCMIN


def test_an_object_with_no_measured_size_still_gets_a_picture():
    assert previews.cutout_fov(None, FRAME) >= previews.MIN_FOV_ARCMIN
    assert previews.cutout_fov(0.0, FRAME) >= previews.MIN_FOV_ARCMIN


def test_the_cutout_is_rounded_so_a_nudge_does_not_earn_a_cache_entry():
    a = previews.cutout_fov(20.0, FRAME)
    b = previews.cutout_fov(20.4, FRAME)          # catalogue jitter
    assert a == b
    assert a % 5 == 0


# --------------------------------------------------------------------- cache
def test_the_cache_key_carries_the_field_as_well_as_the_name(tmp_path):
    """The same object at two zooms is two pictures, and changing the optics has
    to invalidate the old framing rather than quietly show it."""
    a = previews.cache_path("NGC6523", 100.0, tmp_path)
    b = previews.cache_path("NGC6523", 55.0, tmp_path)
    assert a != b
    assert "NGC6523" in a.name


def test_awkward_names_still_produce_one_file_each(tmp_path):
    a = previews.cache_path("IC 434", 30.0, tmp_path)
    b = previews.cache_path("IC/434", 30.0, tmp_path)
    assert a.parent == tmp_path and a.suffix == ".jpg"
    assert "/" not in a.name and " " not in a.name
    assert a == b or a.name != b.name        # same slug is fine, collisions are not


def test_a_truncated_file_does_not_cache_forever(tmp_path):
    p = previews.cache_path("NGC1", 30.0, tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    assert previews.cached("NGC1", 30.0, tmp_path) is None


def test_a_cached_file_is_returned_without_the_network(tmp_path, monkeypatch):
    p = previews.cache_path("NGC1", 30.0, tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_jpeg())

    def boom(*a, **k):                       # a fetch here would be a bug
        raise AssertionError("went to the network with the file on disk")

    monkeypatch.setattr(previews.urllib.request, "urlopen", boom)
    assert previews.fetch("NGC1", 10.0, -20.0, 30.0, root=tmp_path) == p


# --------------------------------------------------------------------- fetch
def test_a_successful_fetch_lands_in_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(previews.urllib.request, "urlopen",
                        lambda *a, **k: _Response(_jpeg()))
    out = previews.fetch("NGC6523", 270.9, -24.4, 100.0, root=tmp_path)
    assert out is not None and out.exists()
    assert previews.cached("NGC6523", 100.0, tmp_path) == out
    # And nothing partial is left behind.
    assert not list(tmp_path.glob("*.part"))


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("no route to host"),
    OSError("connection reset"),
    TimeoutError("timed out"),
])
def test_being_offline_is_not_an_error(tmp_path, monkeypatch, failure):
    """In the field there is no internet, and that is a normal state — the
    interface has to show "no picture", never a traceback."""
    def raise_it(*a, **k):
        raise failure

    monkeypatch.setattr(previews.urllib.request, "urlopen", raise_it)
    assert previews.fetch("NGC1", 10.0, -20.0, 30.0, root=tmp_path) is None
    assert not list(tmp_path.glob("*"))


def test_an_error_page_dressed_as_an_image_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(previews.urllib.request, "urlopen",
                        lambda *a, **k: _Response(b"<html>down for maintenance",
                                                  "text/html"))
    assert previews.fetch("NGC1", 10.0, -20.0, 30.0, root=tmp_path) is None

    monkeypatch.setattr(previews.urllib.request, "urlopen",
                        lambda *a, **k: _Response(b"tiny", "image/jpeg"))
    assert previews.fetch("NGC1", 10.0, -20.0, 30.0, root=tmp_path) is None
    assert previews.cached("NGC1", 30.0, tmp_path) is None


def test_the_url_asks_the_service_in_the_units_it_wants():
    url = previews.url_for(270.904, -24.3867, 60.0)
    assert previews.SERVICE in url
    assert "ra=270.904000" in url and "dec=-24.386700" in url
    assert "fov=1.000000" in url, "the service wants degrees, not arcminutes"
    assert "format=jpg" in url
