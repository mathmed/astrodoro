from __future__ import annotations

import pytest


@pytest.fixture
def make_window(qapp, settings):
    from PySide6.QtCore import QEvent

    built: list = []

    def build(s=None, show: bool = False):
        from astrodoro.ui.main import MainWindow

        w = MainWindow(s if s is not None else settings)
        if show:
            w.show()
        built.append(w)
        return w

    yield build

    # close() does not delete a widget and deleteLater() only posts a
    # DeferredDelete that processEvents does not dispatch, so without this
    # every window a test built stays alive, timers and all: measured over
    # twelve windows, the first took 0.12 s to build and the twelfth 2.61 s.
    for w in built:
        for extra in ("config_window",):
            child = getattr(w, extra, None)
            if child is not None:
                child.close()
        w.close()
        w.deleteLater()
    built.clear()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    qapp.processEvents()


@pytest.fixture
def window(make_window):
    return make_window()
