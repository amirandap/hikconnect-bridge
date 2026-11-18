import os
import signal
import subprocess
import threading
import time

import pytest

from hikconnect_bridge.remux import TS_PACKET, TsRemuxer


@pytest.fixture(scope="module")
def hevc_bytes() -> bytes:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=20:duration=6",
         "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "keyint=20:repeat-headers=1:log-level=none",
         "-f", "hevc", "pipe:1"], capture_output=True, check=False)
    if out.returncode != 0 or not out.stdout:
        pytest.skip("ffmpeg con libx265 no disponible")
    return out.stdout


def children_named(name: str) -> list[str]:
    res = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", name], capture_output=True, text=True, check=False)
    return res.stdout.split()


def test_remux_outputs_aligned_decodable_ts(hevc_bytes, tmp_path):
    remuxer = TsRemuxer("ffmpeg", "hevc")
    remuxer.start()

    def feed() -> None:  # en un hilo aparte, como el servicio: escribir y leer en el mismo hilo bloquea las tuberías
        remuxer.write(hevc_bytes)
        remuxer.close_input()

    writer = threading.Thread(target=feed)
    writer.start()
    chunks = list(remuxer.read_ts())
    writer.join(10)
    remuxer.stop()
    assert chunks and all(len(c) % TS_PACKET == 0 for c in chunks)
    ts = tmp_path / "out.ts"
    ts.write_bytes(b"".join(chunks))
    probe = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name,nb_read_frames", "-of", "default=nw=1", str(ts)],
                           capture_output=True, text=True, check=False)
    assert "codec_name=hevc" in probe.stdout
    frames = int(next(line for line in probe.stdout.splitlines() if line.startswith("nb_read_frames")).split("=")[1])
    assert frames == 120  # una ráfaga completa no debe perder fotogramas (regresión de `-fflags nobuffer`)


def test_stop_is_idempotent_and_leaves_no_process(hevc_bytes):
    remuxer = TsRemuxer("ffmpeg", "hevc")
    remuxer.start()
    remuxer.write(hevc_bytes[:2000])
    remuxer.stop()
    remuxer.stop()
    assert children_named("ffmpeg") == []


def test_write_after_ffmpeg_death_raises_instead_of_hanging():
    remuxer = TsRemuxer("ffmpeg", "hevc")
    remuxer.start()
    os.kill(remuxer.pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while remuxer.returncode is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert remuxer.returncode is not None
    with pytest.raises((BrokenPipeError, ValueError, OSError)):
        remuxer.write(b"\x00" * 65536)
    remuxer.stop()
    assert children_named("ffmpeg") == []


def test_stop_returns_promptly_even_if_a_writer_is_blocked_on_a_full_pipe(hevc_bytes):
    """Regresión: un cliente lento deja un hilo bloqueado en stdin; stop() no debe esperarlo para matar a FFmpeg."""
    remuxer = TsRemuxer("ffmpeg", "hevc")
    remuxer.start()
    writer_error: list[BaseException] = []

    def flood() -> None:
        try:
            while True:
                remuxer.write(hevc_bytes)  # nadie lee la salida: FFmpeg se bloquea y esta escritura también
        except (BrokenPipeError, ValueError, OSError) as exc:
            writer_error.append(exc)

    writer = threading.Thread(target=flood, daemon=True)
    writer.start()
    time.sleep(1.5)  # deja que las tuberías se llenen y el escritor se bloquee
    assert writer.is_alive()
    started = time.monotonic()
    stopper = threading.Thread(target=remuxer.stop, daemon=True)
    stopper.start()
    stopper.join(6)
    assert not stopper.is_alive(), "stop() quedó bloqueado esperando al escritor"
    assert time.monotonic() - started < 5
    writer.join(5)
    assert not writer.is_alive() and writer_error, "el escritor debe terminar con error de tubería"
    assert children_named("ffmpeg") == []


def test_use_after_stop_raises_broken_pipe_not_assertion_error():
    remuxer = TsRemuxer("ffmpeg", "hevc")
    remuxer.start()
    remuxer.stop()
    with pytest.raises(BrokenPipeError):
        remuxer.write(b"x")
    assert list(remuxer.read_ts()) == []
