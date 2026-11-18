import threading
import time

from hikconnect_bridge.fanout import Subscriber


def test_fifo_order():
    sub = Subscriber()
    for chunk in (b"a", b"b", b"c"):
        assert sub.offer(chunk)
    assert [sub.get(0.1) for _ in range(3)] == [b"a", b"b", b"c"]


def test_get_times_out_with_none_and_no_close_reason():
    sub = Subscriber()
    assert sub.get(0.05) is None
    assert sub.close_reason is None


def test_overflow_closes_the_subscriber_without_blocking():
    sub = Subscriber(max_bytes=10)
    assert sub.offer(b"123456")
    assert sub.offer(b"7890123") is False
    assert sub.close_reason == "slow-client"
    assert sub.get(0.05) is None  # la cola vieja se descarta: el cliente no recibe un flujo con huecos
    assert sub.offer(b"x") is False


def test_close_wakes_a_blocked_reader():
    sub = Subscriber()
    result: list[bytes | None] = []
    reader = threading.Thread(target=lambda: result.append(sub.get(5)))
    reader.start()
    time.sleep(0.1)
    started = time.monotonic()
    sub.close("done")
    reader.join(2)
    assert result == [None]
    assert time.monotonic() - started < 1
    assert sub.close_reason == "done"


def test_first_close_reason_wins():
    sub = Subscriber()
    sub.close("first")
    sub.close("second")
    assert sub.close_reason == "first"
