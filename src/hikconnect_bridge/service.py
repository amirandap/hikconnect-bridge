"""Servicio en vivo: una sesión con la nube por cámara, repartida a N clientes con caché de GOP."""
from __future__ import annotations

import contextlib
import logging
import random
import re
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass

from hikconnect_bridge.cadence import CadenceStats
from hikconnect_bridge.fanout import DEFAULT_MAX_QUEUE_BYTES, Subscriber
from hikconnect_bridge.gop_cache import GopCache
from hikconnect_bridge.sources import PacketSource, PermanentSourceError
from hikconnect_bridge.video import Depacketizer, make_depacketizer, rtp_timestamp

log = logging.getLogger(__name__)

POLL_S = 0.2
RETRY_BASE_S = 5.0
RETRY_MAX_S = 30.0
RETRY_JITTER = 0.2
PERMANENT_PAUSE_S = 60.0
FRAME_WAIT_S = 25.0
FRAME_POLL_S = 0.1
FRAME_JOB_WAIT_S = 5.0
FFMPEG_JPEG_TIMEOUT_S = 30
MAX_ERROR_CHARS = 120
DEFAULT_INPUT_FORMAT = "hevc"
RTP_MIN_HEADER = 12
_URL = re.compile(r"https?://\S+")
_LONG_HEX = re.compile(r"[A-Fa-f0-9]{24,}")


class TooManyClientsError(Exception):
    """Se alcanzó el máximo de clientes simultáneos."""


@dataclass(frozen=True)
class Attachment:
    subscriber: Subscriber
    initial: bytes
    input_format: str


def describe_error(exc: BaseException) -> str:
    """Texto corto y sin URLs ni cadenas largas (posibles tokens) para publicar en /health."""
    text = _LONG_HEX.sub("<redactado>", _URL.sub("<url>", str(exc)))
    return f"{type(exc).__name__}: {text}"[:MAX_ERROR_CHARS]


def retry_delay(failures: int, *, rng: random.Random | None = None) -> float:
    base = min(RETRY_BASE_S * 2 ** max(failures - 1, 0), RETRY_MAX_S)
    jitter = (rng or random).uniform(-RETRY_JITTER, RETRY_JITTER)  # noqa: S311 - jitter de reintentos, no criptografía
    return base * (1 + jitter)


class LiveService:
    def __init__(  # noqa: PLR0913 - parámetros de afinación, todos con valor por defecto
        self, source: PacketSource, *, ffmpeg: str = "ffmpeg", idle_seconds: float = 30.0,
        frame_max_age: float = 10.0, max_clients: int = 8, max_frame_jobs: int = 2,
        client_queue_bytes: int = DEFAULT_MAX_QUEUE_BYTES,
        depacketizer_factory=make_depacketizer,  # noqa: ANN001
    ) -> None:
        self._source = source
        self.ffmpeg = ffmpeg
        self._idle = idle_seconds
        self._frame_max_age = frame_max_age
        self._max_clients = max_clients
        self._client_queue_bytes = client_queue_bytes
        self._make_depacketizer = depacketizer_factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._subs: set[Subscriber] = set()
        self._wake_until = 0.0
        self._blocked_until = 0.0
        self._source_open = False
        self._last_error = ""
        self._input_format = DEFAULT_INPUT_FORMAT
        self._started = time.monotonic()
        self._counters: Counter[str] = Counter()
        self._gop = GopCache()
        self._cadence = CadenceStats()
        self._jpeg: tuple[float, bytes] | None = None
        self._jpeg_jobs = threading.BoundedSemaphore(max_frame_jobs)
        self._worker = threading.Thread(target=self._run, name="source-worker", daemon=True)

    # ---- ciclo de vida -----------------------------------------------------------------------
    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.close("service-stopped")
        if self._worker.is_alive():
            self._worker.join(timeout=5)

    def _demand(self) -> bool:
        with self._lock:
            return bool(self._subs) or time.monotonic() < self._wake_until

    def _touch(self, seconds: float) -> None:
        with self._lock:
            self._wake_until = max(self._wake_until, time.monotonic() + seconds)

    # ---- clientes ----------------------------------------------------------------------------
    def attach(self) -> Attachment:
        """Registra un cliente y le entrega el GOP cacheado de forma atómica con el alta."""
        sub = Subscriber(self._client_queue_bytes)
        with self._lock:
            if len(self._subs) >= self._max_clients:
                raise TooManyClientsError
            initial = self._gop.snapshot()
            self._subs.add(sub)
            fmt = self._input_format
        return Attachment(sub, initial, fmt)

    def detach(self, sub: Subscriber) -> None:
        sub.close("detached")
        with self._lock:
            self._subs.discard(sub)
        self._touch(self._idle)

    # ---- hilo de la fuente -------------------------------------------------------------------
    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            if not self._demand() or time.monotonic() < self._blocked_until:
                self._stop.wait(POLL_S)
                continue
            delivered = self._session()
            failures = 0 if delivered else failures + 1
            if not delivered and not self._stop.is_set():
                self._stop.wait(retry_delay(failures))

    def _session(self) -> bool:
        """Una apertura de la fuente. Devuelve True si entregó al menos un fotograma."""
        self._gop.reset()
        self._cadence.reset()
        frames_before = self._counters["frames"]
        try:
            with self._source() as packets:
                self._source_open = True
                self._counters["sessions"] += 1
                self._consume(packets)
        except PermanentSourceError as exc:
            self._last_error = describe_error(exc)
            self._counters["permanent_errors"] += 1
            self._blocked_until = time.monotonic() + PERMANENT_PAUSE_S
            log.error("fuente en pausa %.0fs: %s", PERMANENT_PAUSE_S, self._last_error)
        except Exception as exc:  # noqa: BLE001 - la sesión se reintenta; el motivo queda en /health
            self._last_error = describe_error(exc)
            self._counters["errors"] += 1
            log.warning("sesión terminada con error: %s", self._last_error)
        finally:
            self._source_open = False
        return self._counters["frames"] > frames_before

    def _consume(self, packets) -> None:  # noqa: ANN001
        depacketizer: Depacketizer = self._make_depacketizer()
        for packet in packets:
            if self._stop.is_set() or not self._demand():
                return
            if len(packet) < RTP_MIN_HEADER:
                self._counters["short_packets"] += 1
                continue
            nals = depacketizer.feed_complete(packet)
            self._counters["rtp_packets"] += 1
            if nals:
                self._publish(nals, rtp_timestamp(packet), getattr(depacketizer, "codec", None))

    def _publish(self, nals: list[bytes], timestamp: int, codec) -> None:  # noqa: ANN001
        chunk = b"".join(nals)
        new_frame = False
        with self._lock:
            if codec is not None:
                self._input_format = str(getattr(codec, "value", codec))
            for nal in nals:
                new_frame |= self._gop.add(nal)
            for sub in list(self._subs):
                if not sub.offer(chunk):
                    self._subs.discard(sub)
                    self._counters["clients_dropped_slow"] += 1
        self._counters["frames"] += new_frame
        self._cadence.record(timestamp, new_frame=new_frame)

    # ---- fotograma clave -> JPEG -------------------------------------------------------------
    def keyframe_jpeg(self) -> bytes | None:
        """JPEG del último IRAP reciente; despierta la nube si no lo hay y devuelve None si vence."""
        key = self._fresh_keyframe()
        if key is None:
            return None
        key_time, picture = key
        cached = self._jpeg
        if cached is not None and cached[0] == key_time:
            return cached[1]
        if not self._jpeg_jobs.acquire(timeout=FRAME_JOB_WAIT_S):
            return None
        try:
            jpeg = self._decode_jpeg(picture)
        finally:
            self._jpeg_jobs.release()
        if jpeg is not None:
            self._jpeg = (key_time, jpeg)
        return jpeg

    def _fresh_keyframe(self) -> tuple[float, bytes] | None:
        deadline = time.monotonic() + FRAME_WAIT_S
        woke = False
        while not self._stop.is_set():
            picture, key_time = self._gop.latest_keyframe()
            if picture and time.monotonic() - key_time <= self._frame_max_age:
                return key_time, picture
            if not woke:
                self._touch(FRAME_WAIT_S)
                woke = True
            if time.monotonic() >= deadline:
                return None
            self._stop.wait(FRAME_POLL_S)
        return None

    def _decode_jpeg(self, picture: bytes) -> bytes | None:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", self._input_format, "-i", "pipe:0",
               "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "pipe:1"]
        with contextlib.suppress(subprocess.TimeoutExpired):
            res = subprocess.run(cmd, input=picture, capture_output=True, check=False,
                                 timeout=FFMPEG_JPEG_TIMEOUT_S)
            if res.returncode == 0 and res.stdout[:2] == b"\xff\xd8":
                return res.stdout
        self._counters["jpeg_failures"] += 1
        return None

    # ---- estado ------------------------------------------------------------------------------
    def health(self) -> dict[str, object]:
        with self._lock:
            clients = len(self._subs)
        blocked = time.monotonic() < self._blocked_until
        state = "paused-after-permanent-error" if blocked else "streaming" if self._source_open else "idle"
        return {
            "state": state, "source_open": self._source_open, "clients": clients,
            "uptime_s": round(time.monotonic() - self._started), "last_error": self._last_error,
            "counters": dict(self._counters), "input_format": self._input_format,
            **self._gop.stats(), **self._cadence.summary(),
        }
