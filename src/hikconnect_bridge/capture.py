"""Herramientas de línea de comandos que usan la misma tubería que el servicio: capture y probe."""
from __future__ import annotations

import collections
import subprocess
import time
from pathlib import Path

from hikconnect_bridge.annexb import iter_nals, nal_type
from hikconnect_bridge.sources import PacketSource
from hikconnect_bridge.video import make_depacketizer

RTP_PT_MASK = 0x7F
RTP_MIN_HEADER = 12
FFMPEG_FRAME_TIMEOUT_S = 60
NAL_NAMES = {0: "TRAIL_N", 1: "TRAIL_R", 19: "IDR_W_RADL", 20: "IDR_N_LP", 21: "CRA",
             32: "VPS", 33: "SPS", 34: "PPS"}


def collect(source: PacketSource, seconds: float) -> tuple[bytes, str, collections.Counter[int]]:
    """Recoge `seconds` de video: devuelve Annex B, formato de entrada de FFmpeg y paquetes por PT."""
    depacketizer = make_depacketizer()
    out = bytearray()
    by_pt: collections.Counter[int] = collections.Counter()
    deadline = time.monotonic() + seconds
    with source() as packets:
        for packet in packets:
            if time.monotonic() > deadline:
                break
            if len(packet) >= RTP_MIN_HEADER:
                by_pt[packet[1] & RTP_PT_MASK] += 1
            for nal in depacketizer.feed_complete(packet):
                out += nal
    codec = getattr(depacketizer, "codec", None)
    return bytes(out), str(getattr(codec, "value", codec or "hevc")), by_pt


def nal_histogram(annexb: bytes) -> dict[str, int]:
    hist: collections.Counter[str] = collections.Counter()
    for nal in iter_nals(annexb):
        if len(nal) > 4:
            kind = nal_type(nal)
            hist[NAL_NAMES.get(kind, f"tipo{kind}")] += 1
    return dict(hist)


def extract_first_frame(annexb_path: Path, jpg_path: Path, input_format: str, ffmpeg: str = "ffmpeg") -> bool:
    res = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", input_format, "-i", str(annexb_path),
         "-frames:v", "1", "-update", "1", str(jpg_path)],
        capture_output=True, text=True, check=False, timeout=FFMPEG_FRAME_TIMEOUT_S,
    )
    return res.returncode == 0 and jpg_path.is_file() and jpg_path.stat().st_size > 0
