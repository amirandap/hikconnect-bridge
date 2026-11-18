"""Métricas de cadencia del flujo: fps reales, relación reloj RTP / reloj real, mayor hueco entre paquetes."""
from __future__ import annotations

import collections
import threading
import time

RTP_CLOCK_HZ = 90_000
RTP_WRAP = 1 << 32
RTP_HALF = 1 << 31
DEFAULT_WINDOW_S = 10.0


class CadenceStats:
    """Ventana deslizante; `reset()` al iniciar cada sesión para no arrastrar saltos de timestamp."""

    def __init__(self, window_s: float = DEFAULT_WINDOW_S) -> None:
        self._window_s = window_s
        self._lock = threading.Lock()
        self._samples: collections.deque[tuple[float, int, bool]] = collections.deque()
        self._ticks = 0
        self._last_ts: int | None = None

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._ticks = 0
            self._last_ts = None

    def record(self, rtp_timestamp: int, *, new_frame: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self._last_ts is not None:
                delta = (rtp_timestamp - self._last_ts) % RTP_WRAP
                self._ticks += delta - RTP_WRAP if delta > RTP_HALF else delta
            self._last_ts = rtp_timestamp
            self._samples.append((now, self._ticks, new_frame))
            while self._samples and self._samples[0][0] < now - self._window_s:
                self._samples.popleft()

    def summary(self) -> dict[str, float | int | None]:
        with self._lock:
            samples = list(self._samples)
        if len(samples) < 2:
            return {}
        wall = samples[-1][0] - samples[0][0]
        rtp = (samples[-1][1] - samples[0][1]) / RTP_CLOCK_HZ
        frames = sum(1 for _, _, new in samples if new)
        gaps = [b[0] - a[0] for a, b in zip(samples, samples[1:], strict=False)]
        return {
            "window_s": round(wall, 2),
            "frames_in_window": frames,
            "fps_wall": round(frames / wall, 2) if wall else None,
            "fps_rtp_clock": round(frames / rtp, 2) if rtp > 0 else None,
            "realtime_ratio_rtp_over_wall": round(rtp / wall, 2) if wall else None,
            "max_packet_gap_ms": round(max(gaps) * 1000),
        }
