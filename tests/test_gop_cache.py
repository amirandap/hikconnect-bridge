from helpers import nal

from hikconnect_bridge.annexb import START_CODE, iter_nals, nal_type
from hikconnect_bridge.gop_cache import GopCache

VPS, SPS, PPS, IDR, TRAIL = 32, 33, 34, 19, 1


def feed(cache: GopCache, *kinds: int) -> list[bool]:
    return [cache.add(nal(k)) for k in kinds]


def kinds(blob: bytes) -> list[int]:
    return [nal_type(n) for n in iter_nals(blob)]


def test_empty_until_first_irap():
    cache = GopCache()
    feed(cache, VPS, SPS, PPS, TRAIL, TRAIL)
    assert cache.snapshot() == b""
    assert cache.latest_keyframe()[0] == b""


def test_snapshot_starts_with_parameters_then_irap_then_following_frames():
    cache = GopCache()
    feed(cache, VPS, SPS, PPS, IDR, TRAIL, TRAIL)
    assert kinds(cache.snapshot()) == [VPS, SPS, PPS, IDR, TRAIL, TRAIL]


def test_new_irap_replaces_the_previous_gop():
    cache = GopCache()
    feed(cache, VPS, SPS, PPS, IDR, TRAIL, TRAIL, VPS, SPS, PPS, IDR, TRAIL)
    assert kinds(cache.snapshot()) == [VPS, SPS, PPS, IDR, TRAIL]


def test_new_picture_flag_is_true_only_for_first_slices():
    cache = GopCache()
    flags = feed(cache, VPS, SPS, PPS, IDR)
    assert flags == [False, False, False, True]
    assert cache.add(nal(IDR, first_slice=False)) is False
    assert cache.add(nal(TRAIL)) is True


def test_multi_slice_irap_is_kept_whole_in_the_key_picture():
    cache = GopCache()
    feed(cache, VPS, SPS, PPS, IDR)
    cache.add(nal(IDR, first_slice=False))
    cache.add(nal(TRAIL))  # congela la imagen clave
    key, stamp = cache.latest_keyframe()
    assert kinds(key) == [VPS, SPS, PPS, IDR, IDR]
    assert stamp > 0


def test_key_picture_is_not_published_before_it_is_complete():
    cache = GopCache()
    feed(cache, VPS, SPS, PPS, IDR)
    assert cache.latest_keyframe()[0] == b""


def test_orphan_continuation_slice_is_ignored():
    cache = GopCache()
    feed(cache, VPS, SPS, PPS)
    cache.add(nal(IDR, first_slice=False))
    assert cache.snapshot() == b""


def test_overflow_drops_the_gop_and_waits_for_the_next_irap():
    cache = GopCache(max_bytes=60)
    feed(cache, VPS, SPS, PPS, IDR)
    for _ in range(10):
        cache.add(nal(TRAIL, size=20))
    assert cache.snapshot() == b""
    assert cache.overflows == 1
    feed(cache, VPS, SPS, PPS, IDR)
    assert kinds(cache.snapshot()) == [VPS, SPS, PPS, IDR]


def test_reset_clears_everything():
    cache = GopCache()
    feed(cache, VPS, SPS, PPS, IDR, TRAIL)
    cache.reset()
    assert cache.snapshot() == b""
    assert cache.latest_keyframe() == (b"", 0.0)


def test_snapshot_is_valid_annexb():
    cache = GopCache()
    feed(cache, VPS, SPS, PPS, IDR, TRAIL)
    assert cache.snapshot().startswith(START_CODE)
