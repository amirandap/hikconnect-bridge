"""Punta a punta del servicio: video HEVC real por RTP -> servicio -> HTTP -> FFmpeg del cliente."""
from __future__ import annotations

import contextlib
import os
import signal
import socket
import threading
import time

from net import decode_frames, fetch, ffmpeg_children, first_byte_seconds, wait_until

from hikconnect_bridge.sources import PermanentSourceError, SourceError


def test_no_demand_means_the_source_is_never_opened(start_bridge, replay):
    source = replay()
    bridge = start_bridge(source)
    time.sleep(1.2)
    assert source.opens == 0
    assert bridge.service.health()["state"] == "idle"


def test_stream_is_decodable_end_to_end(start_bridge, replay):
    bridge = start_bridge(replay())
    frames = decode_frames(f"{bridge.url}/stream.ts", 4)
    assert frames >= 50, f"se esperaban ~80 fotogramas en 4 s, llegaron {frames}"
    health = bridge.service.health()
    assert health["input_format"] == "hevc"
    assert 15 <= health["fps_wall"] <= 25


def test_second_client_gets_a_picture_fast_thanks_to_the_gop_cache(start_bridge, replay):
    bridge = start_bridge(replay())
    first = threading.Thread(target=decode_frames, args=(f"{bridge.url}/stream.ts", 6), daemon=True)
    first.start()
    time.sleep(3.0)  # el primer cliente ya hizo que la sesión esté estable y la caché llena
    assert first_byte_seconds(f"{bridge.url}/stream.ts") < 2.5
    assert decode_frames(f"{bridge.url}/stream.ts", 2) >= 20
    first.join(15)


def test_session_closes_after_idle_and_leaves_no_ffmpeg(start_bridge, replay):
    source = replay()
    bridge = start_bridge(source, idle_seconds=1.0)
    assert decode_frames(f"{bridge.url}/stream.ts", 2) > 0
    assert wait_until(lambda: not source.open_now, 8), "la sesión de la nube debería cerrarse sin clientes"
    assert wait_until(lambda: ffmpeg_children() == [], 5), f"FFmpeg huérfano: {ffmpeg_children()}"
    assert source.opens == source.closes


def test_cold_frame_jpg_wakes_the_source_and_returns_a_jpeg(start_bridge, replay):
    source = replay()
    bridge = start_bridge(source, idle_seconds=1.0)
    status, headers, body = fetch(f"{bridge.url}/frame.jpg")
    assert status == 200
    assert body[:2] == b"\xff\xd8"
    assert headers["Content-Type"] == "image/jpeg"
    assert source.opens >= 1
    status2, _, body2 = fetch(f"{bridge.url}/frame.jpg")  # imagen reciente: no vuelve a decodificar
    assert status2 == 200
    assert body2[:2] == b"\xff\xd8"


def test_frame_jpg_returns_504_when_no_keyframe_arrives(start_bridge, monkeypatch):
    monkeypatch.setattr("hikconnect_bridge.service.FRAME_WAIT_S", 1.0)

    @contextlib.contextmanager
    def silent_source():
        yield iter(())  # la fuente abre pero nunca entrega paquetes

    bridge = start_bridge(silent_source)
    status, _, _ = fetch(f"{bridge.url}/frame.jpg")
    assert status == 504


def test_transient_source_error_is_retried_and_recovers(start_bridge, replay, monkeypatch):
    monkeypatch.setattr("hikconnect_bridge.service.RETRY_BASE_S", 0.2)
    source = replay(fail_first=1, error=SourceError("fallo de red simulado"))
    bridge = start_bridge(source)
    assert decode_frames(f"{bridge.url}/stream.ts", 3) >= 20
    health = bridge.service.health()
    assert health["counters"]["errors"] == 1
    assert source.opens >= 2


def test_permanent_error_pauses_retries_and_hides_secrets(start_bridge, replay, monkeypatch):
    monkeypatch.setattr("hikconnect_bridge.service.PERMANENT_PAUSE_S", 1.5)
    secret = "https://api.hik-connect.com/login?token=" + "a1b2c3d4" * 6
    source = replay(fail_first=99, error=PermanentSourceError(secret))
    bridge = start_bridge(source)
    with contextlib.suppress(Exception):
        fetch(f"{bridge.url}/frame.jpg", timeout=3)
    assert wait_until(lambda: bridge.service.health()["state"] == "paused-after-permanent-error", 3)
    opens_when_paused = source.opens
    time.sleep(0.8)
    assert source.opens == opens_when_paused, "no debe reintentar mientras está en pausa"
    error = bridge.service.health()["last_error"]
    assert "token=" not in error and "a1b2c3d4" not in error and "https://" not in error


def test_slow_client_is_dropped_without_hurting_the_others(start_bridge, replay):
    bridge = start_bridge(replay(), client_queue_bytes=32 * 1024)
    slow = socket.socket()
    slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
    slow.connect(("127.0.0.1", bridge.server.server_address[1]))
    slow.sendall(b"GET /stream.ts HTTP/1.1\r\nHost: " + f"127.0.0.1:{bridge.server.server_address[1]}".encode() + b"\r\n\r\n")
    try:
        assert decode_frames(f"{bridge.url}/stream.ts", 4) >= 50  # el cliente sano sigue viendo todo
        assert wait_until(lambda: bridge.service.health()["counters"].get("clients_dropped_slow", 0) >= 1, 25), (
            "el cliente que no lee debería descartarse por lento")
    finally:
        slow.close()


def test_killing_one_clients_ffmpeg_ends_only_that_client(start_bridge, replay):
    bridge = start_bridge(replay())
    port = bridge.server.server_address[1]
    victim = socket.create_connection(("127.0.0.1", port))
    victim.sendall(f"GET /stream.ts HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    victim.settimeout(15)
    assert victim.recv(188), "la víctima debería recibir video antes de que muera su FFmpeg"
    victim_ffmpeg = ffmpeg_children()
    assert len(victim_ffmpeg) == 1, f"debía haber exactamente un FFmpeg de servidor: {victim_ffmpeg}"

    results: list[int] = []
    healthy = threading.Thread(target=lambda: results.append(decode_frames(f"{bridge.url}/stream.ts", 5)), daemon=True)
    healthy.start()
    time.sleep(1.5)
    os.kill(victim_ffmpeg[0], signal.SIGKILL)

    deadline = time.monotonic() + 10
    victim.settimeout(2)
    ended = False
    while time.monotonic() < deadline and not ended:
        try:
            ended = victim.recv(65536) == b""
        except TimeoutError:
            continue
        except OSError:
            ended = True
    victim.close()
    healthy.join(20)
    assert ended, "el cliente cuyo FFmpeg murió debería recibir fin de transmisión"
    assert results and results[0] >= 40, f"el otro cliente debe seguir recibiendo video: {results}"


def test_too_many_clients_gets_503(start_bridge, replay):
    bridge = start_bridge(replay(), max_clients=1)
    holder = threading.Thread(target=decode_frames, args=(f"{bridge.url}/stream.ts", 5), daemon=True)
    holder.start()
    assert wait_until(lambda: bridge.service.health()["clients"] == 1, 10)
    status, _, _ = fetch(f"{bridge.url}/stream.ts", timeout=5)
    assert status == 503
    holder.join(15)


def test_stop_joins_the_worker_and_leaves_nothing_behind(start_bridge, replay):
    source = replay()
    bridge = start_bridge(source)
    decode_frames(f"{bridge.url}/stream.ts", 2)
    bridge.service.stop()
    assert not bridge.service._worker.is_alive()  # noqa: SLF001
    assert wait_until(lambda: ffmpeg_children() == [], 5)
    assert not source.open_now
