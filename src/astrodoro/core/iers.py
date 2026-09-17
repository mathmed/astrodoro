from __future__ import annotations


def use_bundled_table() -> None:
    try:
        from astropy.utils import iers
    except Exception:
        return
    iers.conf.auto_download = False
    iers.conf.auto_max_age = None
