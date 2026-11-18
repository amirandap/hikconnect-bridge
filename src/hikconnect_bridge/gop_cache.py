"""Caché del último GOP (desde el último IRAP) para que un cliente nuevo vea imagen enseguida."""
from __future__ import annotations

import threading
import time

from hikconnect_bridge.annexb import (
    IRAP_TYPES,
    PARAM_SET_TYPES,
    is_first_slice,
    is_vcl,
    nal_type,
)

DEFAULT_MAX_GOP_BYTES = 32 * 1024 * 1024


class GopCache:
    """Guarda parámetros (VPS/SPS/PPS) y los NAL desde el último IRAP; thread-safe y con tope.

    - `snapshot()` devuelve lo que hay que enviar a un cliente nuevo para decodificar desde ya.
    - `latest_keyframe()` devuelve la última imagen IRAP completa (parámetros + slices) y su hora.
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_GOP_BYTES) -> None:
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._params: dict[int, bytes] = {}
        self._gop: list[bytes] = []
        self._gop_bytes = 0
        self._key_parts = 0
        self._key_open = False
        self._last_key = b""
        self._last_key_time = 0.0
        self.overflows = 0

    def reset(self) -> None:
        with self._lock:
            self._params.clear()
            self._gop = []
            self._gop_bytes = 0
            self._key_parts = 0
            self._key_open = False
            self._last_key = b""
            self._last_key_time = 0.0

    def add(self, nal: bytes) -> bool:
        """Registra un NAL. Devuelve True si inicia una imagen nueva (primer slice)."""
        if len(nal) < 5:
            return False
        kind = nal_type(nal)
        with self._lock:
            if kind in PARAM_SET_TYPES:
                self._params[kind] = nal
                return False
            starts_picture = is_vcl(nal) and is_first_slice(nal)
            if is_vcl(nal) and kind in IRAP_TYPES:
                self._add_irap(nal, starts_picture)
            else:
                self._freeze_key_if_open(vcl=is_vcl(nal))
                self._append(nal)
            return starts_picture

    def _add_irap(self, nal: bytes, starts_picture: bool) -> None:
        if starts_picture:
            self._freeze_key_if_open(vcl=True)
            head = [self._params[t] for t in PARAM_SET_TYPES if t in self._params]
            self._gop = head
            self._gop_bytes = sum(len(n) for n in head)
            self._key_parts = len(head)
            self._key_open = True
        elif not self._key_open:
            return  # slice de continuación sin su primer slice: se descarta
        self._append(nal)
        if self._key_open:
            self._key_parts = len(self._gop)

    def _freeze_key_if_open(self, *, vcl: bool) -> None:
        if self._key_open and vcl:
            self._last_key = b"".join(self._gop[: self._key_parts])
            self._last_key_time = time.monotonic()
            self._key_open = False

    def _append(self, nal: bytes) -> None:
        if not self._gop:
            return  # aún no hay IRAP: nada que cachear
        if self._gop_bytes + len(nal) > self._max_bytes:
            self._gop = []
            self._gop_bytes = 0
            self._key_parts = 0
            self._key_open = False
            self.overflows += 1
            return
        self._gop.append(nal)
        self._gop_bytes += len(nal)

    def snapshot(self) -> bytes:
        """Parámetros + todo desde el último IRAP. Vacío si todavía no hubo IRAP."""
        with self._lock:
            return b"".join(self._gop)

    def latest_keyframe(self) -> tuple[bytes, float]:
        with self._lock:
            return self._last_key, self._last_key_time

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"gop_bytes": self._gop_bytes, "gop_nals": len(self._gop), "gop_overflows": self.overflows}
