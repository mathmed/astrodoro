from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import (
    QAbstractSocket,
    QHostAddress,
    QNetworkInterface,
    QSsl,
    QSslCertificate,
    QSslConfiguration,
    QSslKey,
    QSslServer,
)
from PySide6.QtWebSockets import QWebSocketServer

from ..i18n import gettext as _
from ..settings import data_dir

PAGE = Path(__file__).resolve().parents[1] / "ui" / "web" / "handset.html"


def cert_paths() -> tuple[Path, Path]:
    d = data_dir()
    return d / "handset-cert.pem", d / "handset-key.pem"


def local_ips() -> list[str]:
    out = []
    for iface in QNetworkInterface.allInterfaces():
        f = iface.flags()
        up = QNetworkInterface.InterfaceFlag.IsUp
        loopback = QNetworkInterface.InterfaceFlag.IsLoopBack
        if not (f & up) or (f & loopback):
            continue
        for e in iface.addressEntries():
            ip = e.ip()
            if ip.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                out.append(ip.toString())
    out.sort(key=lambda s: (not s.startswith(("192.168.", "10.")), s))
    return out


def ensure_cert(ips: list[str]) -> tuple[Path, Path]:
    cert, key = cert_paths()
    san = ",".join([f"IP:{ip}" for ip in ips] + ["DNS:localhost", "IP:127.0.0.1"])
    if cert.exists() and key.exists():
        try:
            txt = subprocess.run(
                ["openssl", "x509", "-in", str(cert), "-noout", "-text"],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
            if all(f"IP Address:{ip}" in txt for ip in ips):
                return cert, key
        except Exception:
            pass
    cert.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "3650",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=Astrodoro",
            "-addext",
            f"subjectAltName={san}",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return cert, key


class Handset(QObject):
    sample = Signal(float, float, float, object, float)
    clients = Signal(int)
    status = Signal(str)

    def __init__(self, port: int = 8443, parent=None):
        super().__init__(parent)
        self.port = port
        self._srv: QSslServer | None = None
        self._ws: QWebSocketServer | None = None
        self._socks: list = []
        self._pending: dict = {}
        self._ips: list[str] = []

    def start(self) -> str:
        if self._srv is not None:
            return self.url
        self._ips = local_ips()
        cert_p, key_p = ensure_cert(self._ips)
        cfg = QSslConfiguration()
        cfg.setLocalCertificate(QSslCertificate(cert_p.read_bytes()))
        cfg.setPrivateKey(QSslKey(key_p.read_bytes(), QSsl.KeyAlgorithm.Rsa))

        self._ws = QWebSocketServer(
            "astrodoro", QWebSocketServer.SslMode.NonSecureMode, self
        )
        self._ws.newConnection.connect(self._on_ws)

        self._srv = QSslServer(self)
        self._srv.setSslConfiguration(cfg)
        self._srv.pendingConnectionAvailable.connect(self._on_tcp)
        for port in range(self.port, self.port + 5):
            if self._srv.listen(QHostAddress.SpecialAddress.Any, port):
                self.port = port
                break
        else:
            err = self._srv.errorString()
            self._srv = None
            raise OSError(
                _("no free port between {low} and {high}: {error}").format(
                    low=self.port, high=self.port + 4, error=err
                )
            )
        self.status.emit(_("phone: waiting at {url}").format(url=self.url))
        return self.url

    def stop(self) -> None:
        for s in list(self._socks):
            try:
                s.disconnected.disconnect()
                s.close()
            except RuntimeError:
                pass
        self._socks.clear()
        self._pending.clear()
        if self._srv is not None:
            self._srv.close()
            self._srv = None
        if self._ws is not None:
            self._ws.close()
            self._ws = None

    @property
    def running(self) -> bool:
        return self._srv is not None

    @property
    def url(self) -> str:
        ip = self._ips[0] if self._ips else "127.0.0.1"
        return f"https://{ip}:{self.port}/"

    def _on_tcp(self) -> None:
        while self._srv is not None and self._srv.hasPendingConnections():
            sock = self._srv.nextPendingConnection()
            if sock is None:
                break
            self._pending[sock] = (
                sock.readyRead.connect(lambda s=sock: self._on_head(s)),
                sock.disconnected.connect(sock.deleteLater),
            )

    def _on_head(self, sock) -> None:
        head = bytes(sock.peek(8192))
        if b"\r\n\r\n" not in head:
            return
        c_read, c_disc = self._pending.pop(sock, (None, None))
        for c in (c_read, c_disc):
            if c is not None:
                try:
                    sock.disconnect(c)
                except (RuntimeError, TypeError):
                    pass
        if b"upgrade: websocket" in head.lower() and self._ws is not None:
            self._ws.handleConnection(sock)
            return
        sock.disconnected.connect(sock.deleteLater)
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        sock.read(len(head))
        self._serve(sock, request_line)

    def _serve(self, sock, request_line: str) -> None:
        parts = request_line.split(" ")
        path = parts[1] if len(parts) > 1 else "/"
        if path in ("/", "/index.html", "/handset.html"):
            body, ctype, code = self._html(), "text/html; charset=utf-8", "200 OK"
        else:
            body, ctype, code = b"", "text/plain", "404 Not Found"
        sock.write(
            f"HTTP/1.1 {code}\r\nContent-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
            f"Connection: close\r\n\r\n".encode()
            + body
        )
        sock.flush()
        sock.disconnectFromHost()

    def _html(self) -> bytes:
        try:
            return PAGE.read_bytes()
        except OSError as e:
            return f"<h1>handset page not found</h1><p>{e}</p>".encode()

    def _on_ws(self) -> None:
        while self._ws is not None:
            c = self._ws.nextPendingConnection()
            if c is None:
                break
            self._socks.append(c)
            c.textMessageReceived.connect(lambda m, s=c: self._on_msg(m, s))
            c.disconnected.connect(lambda s=c: self._drop(s))
            self.clients.emit(len(self._socks))
            self.status.emit(_("phone connected"))

    def _drop(self, sock) -> None:
        try:
            if sock in self._socks:
                self._socks.remove(sock)
            self.clients.emit(len(self._socks))
            self.status.emit(_("phone disconnected"))
        except RuntimeError:
            pass

    def _on_msg(self, msg: str, sock) -> None:
        try:
            d = json.loads(msg)
        except ValueError:
            return
        kind = d.get("type")
        if kind == "o":
            self.sample.emit(
                float(d.get("a") or 0.0),
                float(d.get("b") or 0.0),
                float(d.get("g") or 0.0),
                d.get("c"),
                time.monotonic(),
            )
        elif kind == "hello":
            self.status.emit(
                _("phone: {agent}").format(agent=str(d.get("ua", "?"))[:60])
            )

    def send(self, payload: dict) -> None:
        if not self._socks:
            return
        txt = json.dumps(payload, ensure_ascii=False)
        for s in self._socks:
            s.sendTextMessage(txt)
