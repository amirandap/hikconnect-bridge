"""Fija el contrato con el depaquetizador de hikcloudstream: si el SDK lo cambia, esto falla primero."""
from hikconnect_bridge.annexb import START_CODE, iter_nals, nal_type
from hikconnect_bridge.video import make_depacketizer, rtp_timestamp


def test_depacketizer_yields_annexb_nals_from_real_hevc_rtp(rtp_capture):
    depacketizer = make_depacketizer()
    nals = [n for p in rtp_capture for n in depacketizer.feed_complete(p)]
    assert nals, "el depaquetizador no produjo NAL"
    assert all(n.startswith(START_CODE) for n in nals)
    kinds = {nal_type(n) for n in nals}
    assert {32, 33, 34} <= kinds, "faltan VPS/SPS/PPS"
    assert kinds & {19, 20}, "falta un IDR"
    assert getattr(depacketizer.codec, "value", None) == "hevc"


def test_every_output_chunk_splits_into_whole_nals(rtp_capture):
    depacketizer = make_depacketizer()
    for packet in rtp_capture:
        chunk = b"".join(depacketizer.feed_complete(packet))
        assert b"".join(iter_nals(chunk)) == chunk


def test_rtp_timestamp_reads_the_header_field():
    packet = bytes([0x80, 96, 0, 1]) + (0xDEADBEEF).to_bytes(4, "big") + bytes(4) + b"x"
    assert rtp_timestamp(packet) == 0xDEADBEEF
