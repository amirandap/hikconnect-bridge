"""Fixtures compartidas: HEVC real empaquetado en RTP por FFmpeg y una fuente que lo reproduce en tiempo real."""
from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from hikconnect_bridge.http_api import BridgeServer
from hikconnect_bridge.service import LiveService

FPS = 20
GOP_SECONDS = 1
CAPTURE_SECONDS = 6
RTP_CLOCK = 90_000
VIDEO_PT = 96
PRIVATE_PT = 112
PRIVATE_EXT = bytes.fromhex("0001000c") + bytes(range(48))  # extensión RTP: profile 1, 12 palabras


def _have_libx265() -> bool:
    if shutil.which("ffmpeg") is None:
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False)
    return "libx265" in out.stdout


@pytest.fixture(scope="session")
def rtp_capture() -> list[bytes]:
    """6 s de HEVC sintético (testsrc2) empaquetado por FFmpeg en RTP (RFC 7798) y capturado por UDP."""
    if not _have_libx265():
        pytest.skip("hace falta ffmpeg con libx265 para generar el video de prueba")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    port = sock.getsockname()[1]
    sender = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
         "-i", f"testsrc2=size=640x360:rate={FPS}:duration={CAPTURE_SECONDS}",
         "-c:v", "libx265", "-preset", "ultrafast",
         "-x265-params", f"keyint={FPS * GOP_SECONDS}:min-keyint={FPS * GOP_SECONDS}:scenecut=0:repeat-headers=1:log-level=none",
         "-f", "rtp", "-sdp_file", "/dev/null", f"rtp://127.0.0.1:{port}?pkt_size=1200"],
    )
    packets: list[bytes] = []
    deadline = time.monotonic() + CAPTURE_SECONDS + 15
    while time.monotonic() < deadline:
        try:
            packets.append(sock.recv(65535))
        except TimeoutError:
            if sender.poll() is not None:
                break
    sender.wait(timeout=10)
    sock.close()
    assert packets, "FFmpeg no produjo paquetes RTP"
    return packets


class ReplaySource:
    """Fuente de paquetes: reproduce la captura en bucle y en tiempo real, con seq/timestamp continuos."""

    def __init__(self, packets: list[bytes], *, speed: float = 1.0, private: bool = True,
                 fail_first: int = 0, error: Exception | None = None) -> None:
        self._packets = packets
        self._speed = speed
        self._private = private
        self._fail_first = fail_first
        self._error = error
        self.opens = 0
        self.closes = 0
        self.open_now = False

    def __call__(self) -> contextlib.AbstractContextManager[Iterator[bytes]]:
        return self._open()

    @contextlib.contextmanager
    def _open(self) -> Iterator[Iterator[bytes]]:
        self.opens += 1
        if self._error is not None and self.opens <= self._fail_first:
            raise self._error
        self.open_now = True
        try:
            yield self._stream()
        finally:
            self.open_now = False
            self.closes += 1

    def _stream(self) -> Iterator[bytes]:
        first_ts = int.from_bytes(self._packets[0][4:8], "big")
        last_ts = int.from_bytes(self._packets[-1][4:8], "big")
        span = ((last_ts - first_ts) % (1 << 32)) + RTP_CLOCK // FPS
        start = time.monotonic()
        seq, private_seq, loop = 0, 0xC36E, 0
        while True:
            for original in self._packets:
                rel = ((int.from_bytes(original[4:8], "big") - first_ts) % (1 << 32)) + loop * span
                delay = start + rel / RTP_CLOCK / self._speed - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                ts = (first_ts + rel) % (1 << 32)
                video = bytearray(original)
                video[2:4] = (seq & 0xFFFF).to_bytes(2, "big")
                video[4:8] = ts.to_bytes(4, "big")
                seq += 1
                yield bytes(video)
                if self._private:
                    header = bytes([0x90, 0x80 | PRIVATE_PT]) + (private_seq & 0xFFFF).to_bytes(2, "big")
                    yield header + ts.to_bytes(4, "big") + video[8:12] + PRIVATE_EXT + b"\x7e\x01" + bytes(20)
                    private_seq += 1
            loop += 1


@pytest.fixture
def replay(rtp_capture: list[bytes]):
    def make(**kwargs) -> ReplaySource:  # noqa: ANN003
        return ReplaySource(rtp_capture, **kwargs)
    return make


@dataclass
class Bridge:
    service: LiveService
    server: BridgeServer
    url: str


@pytest.fixture
def start_bridge():
    """Arranca servicio + servidor HTTP en un puerto libre y lo limpia siempre al terminar."""
    running: list[tuple[LiveService, BridgeServer]] = []

    def start(source, *, token: str | None = None, **service_kwargs) -> Bridge:  # noqa: ANN001, ANN003
        service = LiveService(source, **service_kwargs)
        service.start()
        server = BridgeServer(("127.0.0.1", 0), service, token=token)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        running.append((service, server))
        return Bridge(service, server, f"http://127.0.0.1:{server.server_address[1]}")

    yield start
    for service, server in running:
        server.shutdown()
        server.server_close()
        service.stop()


@pytest.fixture(autouse=True)
def no_leaked_ffmpeg():
    """Tras cada prueba no debe quedar ningún FFmpeg hijo: así se señala la prueba exacta que lo deja."""
    from net import ffmpeg_children, wait_until

    yield
    leaked = ffmpeg_children()
    assert wait_until(lambda: not ffmpeg_children(), 8), f"FFmpeg huérfano tras la prueba: {leaked}"
