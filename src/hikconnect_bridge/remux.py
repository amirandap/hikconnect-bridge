"""Un FFmpeg por cliente: Annex B (HEVC/H.264) -> MPEG-TS, con lectura alineada a paquetes de 188 bytes."""
from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator

TS_PACKET = 188
TS_READ_PACKETS = 7
STOP_WAIT_S = 3.0


class TsRemuxer:
    """Envuelve un proceso `ffmpeg -c copy` que convierte Annex B en MPEG-TS.

    Un proceso por cliente evita compartir el ciclo de vida de FFmpeg entre espectadores y permite
    empezar cada TS en un IRAP con sus parámetros. Con `-c copy` el coste de CPU es mínimo.
    """

    def __init__(self, ffmpeg: str, input_format: str = "hevc") -> None:
        self._cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-use_wallclock_as_timestamps", "1",
            # NO usar `-fflags nobuffer`: con ráfagas de entrada FFmpeg pierde fotogramas (medido: 244 de 298
            # en un substream real). `-probesize`/`-analyzeduration` pequeños sí son inocuos y dan arranque rápido.
            "-probesize", "65536", "-analyzeduration", "0",
            "-f", input_format, "-i", "pipe:0", "-c:v", "copy",
            "-f", "mpegts", "-muxdelay", "0", "-flush_packets", "1", "pipe:1",
        ]
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

    @property
    def pid(self) -> int | None:
        return None if self._proc is None else self._proc.pid

    @property
    def returncode(self) -> int | None:
        return None if self._proc is None else self._proc.poll()

    def write(self, annexb: bytes) -> None:
        """Escribe todo el bloque; lanza BrokenPipeError si FFmpeg ya no existe o el remuxer se detuvo."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise BrokenPipeError("remuxer detenido")
        proc.stdin.write(annexb)
        proc.stdin.flush()

    def close_input(self) -> None:
        """Cierra stdin: FFmpeg vacía lo pendiente y termina (el lector verá EOF)."""
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(BrokenPipeError, ValueError, OSError):
                proc.stdin.close()

    def read_ts(self) -> Iterator[bytes]:
        """Trozos de TS siempre múltiplos de 188 bytes (el resto parcial se retiene hasta completarse)."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        pending = b""
        while data := proc.stdout.read1(TS_PACKET * TS_READ_PACKETS):  # type: ignore[attr-defined]
            pending += data
            usable = len(pending) - len(pending) % TS_PACKET
            if usable:
                yield pending[:usable]
                pending = pending[usable:]

    def stop(self) -> None:
        """Termina FFmpeg sin dejar procesos ni pipes abiertos. Idempotente.

        Primero se mata el proceso: así se desbloquea cualquier hilo atascado escribiendo en una tubería
        llena. Cerrar los flujos antes esperaría el candado de esa escritura y no terminaría nunca.
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(STOP_WAIT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
