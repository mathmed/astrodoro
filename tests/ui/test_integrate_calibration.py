from __future__ import annotations

from astrodoro.core import masters


def test_the_panel_offers_all_three_kinds(window):
    w = window
    assert set(w.integrate._calib_labels) == set(masters.KINDS)
    for kind, label in w.integrate._calib_labels.items():
        assert label.text() == f"{kind}: none"
    for b in (
        w.integrate.btn_bias,
        w.integrate.btn_capture_bias,
        w.integrate.btn_dark,
        w.integrate.btn_capture_dark,
        w.integrate.btn_flat,
        w.integrate.btn_capture_flat,
    ):
        assert b.isEnabled()


def test_each_kind_writes_only_its_own_line(window):
    w = window
    w.integrate.on_calibration({"kind": "bias", "path": "/x/bias_g250_o20_bin2.fits"})

    assert w.integrate.lbl_bias.text() == "bias: bias_g250_o20_bin2.fits"
    assert w.integrate.lbl_dark.text() == "dark: none"
    assert w.integrate.lbl_flat.text() == "flat: none"


def test_a_refusal_is_shown_where_the_file_name_would_be(window):
    w = window
    w.integrate.on_calibration(
        {"kind": "dark", "path": "", "reason": "dark ignored: (100, 100) != (200, 200)"}
    )

    assert "ignored" in w.integrate.lbl_dark.text()
    assert w.pal.bad in w.integrate.lbl_dark.styleSheet()


def test_a_file_picked_before_start_is_marked_as_unchecked(
    window, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog

    w = window
    picked = tmp_path / "bias_g250_o20_bin2.fits"
    seen = {}

    def fake(parent, title, folder, filt):
        seen["folder"] = folder
        return str(picked), filt

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake))
    w.integrate.pick_bias()

    assert seen["folder"] == str(w.settings.path("bias_dir"))
    assert w._bias_path == str(picked)
    assert "checked at Start" in w.integrate.lbl_bias.text()


def test_recording_a_master_needs_a_session(window):
    w = window
    assert w.worker is None
    for call, word in (
        (w.integrate.capture_bias, "bias"),
        (w.integrate.capture_dark, "dark"),
        (w.integrate.capture_flat, "flat"),
    ):
        w.log.clear()
        call()
        text = w.log.toPlainText()
        assert "start the capture" in text and word in text


def test_a_master_can_be_dropped_without_touching_the_file(
    window, tmp_path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog

    w = window
    picked = tmp_path / "dark_e5_g250_o20_bin2_t-10.fits"
    picked.write_bytes(b"")

    def fake(parent, title, folder, filt):
        return str(picked), filt

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake))
    w.integrate.pick_dark()

    assert w._dark_path == str(picked)
    assert w.integrate.btn_clear_dark.isEnabled()

    w.integrate.btn_clear_dark.click()

    assert w._dark_path is None
    assert w.integrate.lbl_dark.text() == "dark: none"
    assert not w.integrate.btn_clear_dark.isEnabled()
    assert picked.exists(), "removing a master must not delete the file"
    assert "no longer corrected" in w.log.toPlainText()


def test_the_other_kinds_keep_their_master_when_one_is_dropped(window):
    w = window
    for kind in ("bias", "dark", "flat"):
        w.integrate.on_calibration({"kind": kind, "path": f"/x/{kind}.fits"})
        setattr(w, f"_{kind}_path", f"/x/{kind}.fits")

    w.integrate.clear_bias()

    assert w.integrate.lbl_bias.text() == "bias: none"
    assert w.integrate.lbl_dark.text() == "dark: dark.fits"
    assert w.integrate.lbl_flat.text() == "flat: flat.fits"
    assert not w.integrate.btn_clear_bias.isEnabled()
    assert w.integrate.btn_clear_dark.isEnabled()


def test_nothing_to_drop_leaves_no_trace(window):
    w = window
    assert not w.integrate.btn_clear_flat.isEnabled()
    w.log.clear()
    w.integrate.clear_flat()
    assert w.log.toPlainText() == ""
