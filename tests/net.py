"""Ayudas de red y procesos para las pruebas de punta a punta."""
from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request

FIRST_BYTE_TIMEOUT_S = 30


def fetch(url: str, *, headers: dict[str, str] | None = None, timeout: float = 40.0) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - URL local de prueba
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def decode_frames(url: str, seconds: int, *, headers: str = "") -> int:
    """Decodifica `seconds` del stream con FFmpeg y devuelve cuántos fotogramas salieron."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-i", url, "-t", str(seconds), "-f", "null", "-", "-progress", "pipe:1"]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=seconds + 40)
    counts = [int(line.split("=")[1]) for line in res.stdout.splitlines() if line.startswith("frame=")]
    return max(counts) if counts else 0


def first_byte_seconds(url: str) -> float:
    started = time.monotonic()
    with urllib.request.urlopen(url, timeout=FIRST_BYTE_TIMEOUT_S) as resp:  # noqa: S310
        resp.read(188)
    return time.monotonic() - started


def ffmpeg_children() -> list[int]:
    res = subprocess.run(["pgrep", "-P", str(os.getpid()), "-f", "ffmpeg"], capture_output=True, text=True, check=False)
    return [int(pid) for pid in res.stdout.split()]


def wait_until(predicate, timeout: float, step: float = 0.1) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
