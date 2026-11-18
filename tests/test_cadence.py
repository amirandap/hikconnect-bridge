from hikconnect_bridge.cadence import RTP_CLOCK_HZ, RTP_WRAP, CadenceStats


def feed_frames(stats: CadenceStats, *, fps: float, seconds: float, start_ts: int = 0, speed: float = 1.0):
    step = RTP_CLOCK_HZ / fps
    for i in range(int(fps * seconds) + 1):
        stats.record(int(start_ts + i * step) % RTP_WRAP, new_frame=True, now=i / fps / speed)


def test_real_time_stream_reports_nominal_fps_and_ratio_one():
    stats = CadenceStats(window_s=30)
    feed_frames(stats, fps=20, seconds=5)
    summary = stats.summary()
    assert abs(summary["fps_wall"] - 20) < 1.0
    assert abs(summary["realtime_ratio_rtp_over_wall"] - 1.0) < 0.05


def test_timestamp_wraparound_is_handled():
    stats = CadenceStats(window_s=30)
    feed_frames(stats, fps=20, seconds=5, start_ts=RTP_WRAP - 90_000)
    assert abs(stats.summary()["realtime_ratio_rtp_over_wall"] - 1.0) < 0.05


def test_stream_faster_than_real_time_has_ratio_above_one():
    stats = CadenceStats(window_s=30)
    feed_frames(stats, fps=20, seconds=5, speed=2.0)
    assert 1.9 < stats.summary()["realtime_ratio_rtp_over_wall"] < 2.1


def test_reset_drops_state_so_a_reconnection_gap_does_not_pollute_the_ratio():
    stats = CadenceStats(window_s=30)
    feed_frames(stats, fps=20, seconds=2)
    stats.reset()
    assert stats.summary() == {}
    feed_frames(stats, fps=20, seconds=2, start_ts=123_456_789)
    assert abs(stats.summary()["realtime_ratio_rtp_over_wall"] - 1.0) < 0.05


def test_window_forgets_old_samples():
    stats = CadenceStats(window_s=1.0)
    feed_frames(stats, fps=20, seconds=5)
    assert stats.summary()["window_s"] <= 1.05
