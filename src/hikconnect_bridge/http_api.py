"""API HTTP local: /stream.ts, /frame.jpg y /health, con protección frente a páginas web y límites."""
from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from hikconnect_bridge.remux import TsRemuxer
from hikconnect_bridge.service import Attachment, LiveService, TooManyClientsError

REQUEST_TIMEOUT_S = 30
SUBSCRIBER_POLL_S = 1.0
FEEDER_JOIN_S = 2.0
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
HTTP_FORBIDDEN, HTTP_UNAVAILABLE, HTTP_GATEWAY_TIMEOUT, HTTP_NOT_FOUND = 403, 503, 504, 404


def _has_port(host: str) -> bool:
    """True si `host` ya trae puerto (`nombre:9000` o `[::1]:9000`); `[::1]` sin puerto no cuenta."""
    return ":" in host.rsplit("]", 1)[-1]


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: LiveService, *, token: str | None = None,
                 extra_hosts: tuple[str, ...] = ()) -> None:
        super().__init__(address, _Handler)
        self.service = service
        self.token = token
        port = self.server_address[1]
        hosts = {*LOOPBACK_HOSTS, address[0], *extra_hosts}
        # un host con puerto explícito se respeta tal cual; los demás se aceptan con el puerto del servidor
        self.allowed_hosts = {h.lower() if _has_port(h) else f"{h}:{port}".lower() for h in hosts}


class _Handler(BaseHTTPRequestHandler):
    timeout = REQUEST_TIMEOUT_S
    server: BridgeServer

    def log_message(self, *_args: object) -> None:  # sin log por petición (evita filtrar tokens en la URL)
        return

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        """Rechaza DNS rebinding (Host ajeno), peticiones entre sitios del navegador y falta de token."""
        if self.headers.get("Host", "").lower() not in self.server.allowed_hosts:
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        token = self.server.token
        if token is None:
            return True
        bearer = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        given = bearer or (query.get("token") or [""])[0]
        return hmac.compare_digest(given.encode(), token.encode())

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if not self._authorized(parse_qs(parts.query)):
            self._send(HTTP_FORBIDDEN, "text/plain", b"forbidden\n")
            return
        service = self.server.service
        if parts.path == "/health":
            self._send(200, "application/json", json.dumps(service.health(), indent=2).encode())
        elif parts.path == "/frame.jpg":
            jpeg = service.keyframe_jpeg()
            if jpeg is None:
                self._send(HTTP_GATEWAY_TIMEOUT, "text/plain", b"no keyframe available yet\n")
            else:
                self._send(200, "image/jpeg", jpeg)
        elif parts.path == "/stream.ts":
            self._stream(service)
        else:
            self._send(HTTP_NOT_FOUND, "text/plain", b"/stream.ts  /frame.jpg  /health\n")

    def _stream(self, service: LiveService) -> None:
        try:
            attachment = service.attach()
        except TooManyClientsError:
            self._send(HTTP_UNAVAILABLE, "text/plain", b"too many clients\n")
            return
        remuxer = TsRemuxer(service.ffmpeg, attachment.input_format)
        feeder: threading.Thread | None = None
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            remuxer.start()
            feeder = threading.Thread(target=_feed, args=(attachment, remuxer), daemon=True)
            feeder.start()
            for ts in remuxer.read_ts():
                self.wfile.write(ts)
        except OSError:
            pass  # el cliente cerró la conexión
        finally:
            service.detach(attachment.subscriber)
            remuxer.stop()
            if feeder is not None:
                feeder.join(FEEDER_JOIN_S)


def _feed(attachment: Attachment, remuxer: TsRemuxer) -> None:
    """Escribe el GOP cacheado y luego el flujo en vivo en el FFmpeg del cliente."""
    try:
        if attachment.initial:
            remuxer.write(attachment.initial)
        while attachment.subscriber.close_reason is None:
            chunk = attachment.subscriber.get(SUBSCRIBER_POLL_S)
            if chunk:
                remuxer.write(chunk)
    except (BrokenPipeError, ValueError, OSError):
        pass
    finally:
        remuxer.close_input()
