"""A fake phone: wanders the sky and feeds the GUI's sensor.

It exists so you can work on the FRAME screen without strapping a phone to the
tube and without going outside — the same role `ReplaySource` plays for the
camera. It imitates the real device, crooked mount included: that twist is what
makes the alignment most accurate near the star used.

    astrodoro-gui                  # in one terminal, with the sensor on
    python scripts/fake_handset.py # in another

Needs the package installed (`uv pip install -e .`), like the tests do.

Once connected, choose stars from the keyboard:

    l          list the visible stars
    <number>   point at that star
    a/z s/x    push the tube: altitude up/down, azimuth east/west
    q          quit
"""
from __future__ import annotations

import json
import sys
import threading

import numpy as np
from PySide6.QtCore import QCoreApplication, QTimer, QUrl
from PySide6.QtNetwork import QSslConfiguration, QSslSocket
from PySide6.QtWebSockets import QWebSocket
from scipy.optimize import least_squares

from astrodoro.pointing import brightstars as bs
from astrodoro.pointing import orientation as o
from astrodoro.pointing.handset import local_ips
from astrodoro.settings import Settings

#: Mount 2.8 degrees off the tube axis — a phone held on with a rubber band
#: ends up like this, and it is the defect the alignment has to absorb.
MOUNT = o.normalize(np.array([0.045, 1.0, 0.018]))


def sensor(alt: float, az: float) -> tuple[float, float, float]:
    """The alpha/beta a device on a crooked mount would report for (alt, az)."""
    target = o.altaz_to_enu(alt, az)

    def residual(x):
        return o.normalize(
            o.rotation_matrix(x[0], x[1], 0.0) @ MOUNT) - target

    x = least_squares(residual, [(360.0 - az) % 360.0, alt], method="lm").x
    return float(x[0]), float(x[1]), 0.0


def main() -> None:
    settings = Settings.load()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else settings.handset_port
    ips = local_ips()
    ip = ips[0] if ips else "127.0.0.1"
    app = QCoreApplication([])

    visible = bs.visible(settings.latitude, settings.longitude, min_alt=25.0)
    if not visible:
        print("no bright star above 25 degrees right now")
        return
    where = [visible[0][1], visible[0][2]]
    print(f"pointing at {visible[0][0].full}")

    sock = QWebSocket()
    cfg = QSslConfiguration.defaultConfiguration()
    cfg.setPeerVerifyMode(QSslSocket.PeerVerifyMode.VerifyNone)
    sock.setSslConfiguration(cfg)
    sock.sslErrors.connect(lambda e: sock.ignoreSslErrors())
    sock.errorOccurred.connect(
        lambda e: print("error:", sock.errorString(),
                        "— is the GUI's sensor switched on?"))
    sock.textMessageReceived.connect(lambda m: print("  <- laptop:", m))

    def send():
        a, b, g = sensor(*where)
        sock.sendTextMessage(
            json.dumps({"type": "o", "a": a, "b": b, "g": g, "c": None}))

    timer = QTimer()
    timer.timeout.connect(send)
    sock.connected.connect(lambda: (print("connected"), timer.start(50)))
    sock.open(QUrl(f"wss://{ip}:{port}/"))

    def keyboard():
        for line in sys.stdin:
            c = line.strip().lower()
            if c == "q":
                break
            if c == "l":
                for i, (st, alt, az) in enumerate(visible[:15]):
                    print(f"  {i:2d}  {st.full:26s} alt {alt:5.1f}° "
                          f"az {az:6.1f}°")
            elif c.isdigit() and int(c) < len(visible):
                st, alt, az = visible[int(c)]
                where[:] = [alt, az]
                print(f"  -> {st.full}")
            elif c in ("a", "z", "s", "x"):
                d = {"a": (1, 0), "z": (-1, 0), "s": (0, 1), "x": (0, -1)}[c]
                where[0] += d[0]
                where[1] += d[1]
                print(f"  alt {where[0]:.1f}°  az {where[1]:.1f}°")
        app.quit()

    if sys.stdin.isatty():
        threading.Thread(target=keyboard, daemon=True).start()
        print("  l  list   <n>  point   a/z  up-down   s/x  azimuth   q  quit")
    else:
        # With no terminal (running from a script), keep pointing at the first
        # star instead of exiting immediately on stdin EOF.
        print("  no terminal: pointing at a fixed star")
    app.exec()


if __name__ == "__main__":
    main()
