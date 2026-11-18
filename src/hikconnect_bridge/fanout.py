"""Cola de salida acotada por bytes para un cliente; al desbordarse el cliente se cierra con motivo."""
from __future__ import annotations

import collections
import threading

DEFAULT_MAX_QUEUE_BYTES = 8 * 1024 * 1024


class Subscriber:
    """Búfer FIFO por cliente. `offer` nunca bloquea; `get` bloquea hasta dato, cierre o timeout."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_QUEUE_BYTES) -> None:
        self._max_bytes = max_bytes
        self._cond = threading.Condition()
        self._items: collections.deque[bytes] = collections.deque()
        self._bytes = 0
        self._reason: str | None = None

    @property
    def close_reason(self) -> str | None:
        with self._cond:
            return self._reason

    def offer(self, chunk: bytes) -> bool:
        """Encola un trozo. Devuelve False (y cierra) si el cliente va demasiado atrasado."""
        with self._cond:
            if self._reason is not None:
                return False
            if self._bytes + len(chunk) > self._max_bytes:
                self._reason = "slow-client"
                self._items.clear()
                self._bytes = 0
                self._cond.notify_all()
                return False
            self._items.append(chunk)
            self._bytes += len(chunk)
            self._cond.notify()
            return True

    def close(self, reason: str) -> None:
        with self._cond:
            if self._reason is None:
                self._reason = reason
            self._cond.notify_all()

    def get(self, timeout: float) -> bytes | None:
        """Siguiente trozo, o None si el cliente se cerró o venció el timeout (ver close_reason)."""
        with self._cond:
            if not self._items and self._reason is None:
                self._cond.wait(timeout)
            if self._items:
                chunk = self._items.popleft()
                self._bytes -= len(chunk)
                return chunk
            return None
