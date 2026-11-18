"""Frontera con hikcloudstream: único módulo que conoce el depaquetizador RTP del SDK."""
from __future__ import annotations

from typing import Protocol

from hikcloudstream.stream.rtp import RtpVideoDepacketizer


class Depacketizer(Protocol):
    """Convierte un paquete RTP en cero o más NAL Annex B (cada uno con start code de 4 bytes)."""

    def feed_complete(self, rtp_packet: bytes) -> list[bytes]: ...


def make_depacketizer(video_payload_type: int | None = None) -> Depacketizer:
    """Depaquetizador H.264/HEVC con detección de códec y bloqueo del tipo de carga RTP de video."""
    return RtpVideoDepacketizer(video_payload_type=video_payload_type)


def rtp_timestamp(packet: bytes) -> int:
    return int.from_bytes(packet[4:8], "big")
