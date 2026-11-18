"""Utilidades mínimas de Annex B / HEVC compartidas por el servicio."""
from __future__ import annotations

from collections.abc import Iterator

START_CODE = b"\x00\x00\x00\x01"
NAL_VPS, NAL_SPS, NAL_PPS = 32, 33, 34
PARAM_SET_TYPES = (NAL_VPS, NAL_SPS, NAL_PPS)
IRAP_TYPES = range(16, 22)  # BLA, IDR, CRA
MAX_VCL_TYPE = 31
# start code (4) + NAL header (2) + primer byte del slice header (first_slice_segment_in_pic_flag)
MIN_SLICE_NAL_LEN = 7
HEVC_TYPE_SHIFT = 1
HEVC_TYPE_MASK = 0x3F
FIRST_SLICE_FLAG = 0x80


def iter_nals(annexb: bytes) -> Iterator[bytes]:
    """Parte un bloque Annex B (start code de 4 bytes) en NAL, cada uno con su start code."""
    starts: list[int] = []
    pos = annexb.find(START_CODE)
    while pos != -1:
        starts.append(pos)
        pos = annexb.find(START_CODE, pos + len(START_CODE))
    if not starts:
        return
    for begin, end in zip(starts, [*starts[1:], len(annexb)], strict=True):
        yield annexb[begin:end]


def nal_type(nal: bytes) -> int:
    """Tipo de NAL HEVC (el NAL debe traer start code de 4 bytes)."""
    return (nal[4] >> HEVC_TYPE_SHIFT) & HEVC_TYPE_MASK


def is_vcl(nal: bytes) -> bool:
    return len(nal) > 5 and nal_type(nal) <= MAX_VCL_TYPE


def is_first_slice(nal: bytes) -> bool:
    return len(nal) >= MIN_SLICE_NAL_LEN and bool(nal[6] & FIRST_SLICE_FLAG)
