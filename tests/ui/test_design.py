from __future__ import annotations

from astrodoro.ui.design import palette, stylesheet

FIELDS = (
    "QLineEdit",
    "QSpinBox",
    "QDoubleSpinBox",
    "QDateTimeEdit",
    "QComboBox",
    "QPlainTextEdit",
)


def test_every_editable_field_gets_the_same_box():
    qss = stylesheet(palette("dark"))
    selector = qss[: qss.index("{", qss.index("QLineEdit"))]
    for w in FIELDS:
        assert w in selector, f"{w} would be drawn without a border"
