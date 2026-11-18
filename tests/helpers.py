"""Constructores de NAL sintéticos para pruebas unitarias."""
from hikconnect_bridge.annexb import START_CODE


def nal(kind: int, *, first_slice: bool = True, size: int = 8) -> bytes:
    header = bytes([(kind << 1) & 0x7E, 0x01])
    slice_header = bytes([0x80 if first_slice else 0x00])
    return START_CODE + header + slice_header + bytes([kind]) * (size - 1)
