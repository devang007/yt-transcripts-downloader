"""Rate limiter and circuit breaker, driven by a fake clock.

Timing code tested with real sleeps is slow and flaky, and the interesting cases
here span an hour of cooldown. The clock is injected, so the whole escalation
schedule runs in microseconds and the assertions are exact rather than
approximate.
"""

from __future__ import annotations

import random
import threading

import pytest

from tests.fakes import FakeClock, ScriptedRandom
from yt_tx.limiter import (
    BreakerState,
    BreakerStatus,
    CircuitBreaker,
    CircuitExhausted,
    RateLimitTimeout,
    Stopped,
    TokenBucket,
    full_jitter_backoff,
)

# --------------------------------------------------------------------------- #
# Token bucket
# --------------------------------------------------------------------------- #


def make_bucket(
    rate: float = 1.0,
    capacity: float = 3.0,
    *,
    jitter: float = 0.0,
    clock: FakeClock | None = None,
) -> tuple[TokenBucket, FakeClock]:
    fake = clock or FakeClock()
    bucket = TokenBucket(
        rate, capacity, jitter=jitter, clock=fake.monotonic, sleep=fake.sleep
    )
    return bucket, fake


def test_burst_is_immediate_then_rate_limited() -> None:
    """Capacity tokens go straight through; the next one waits 1/rate."""
    bucket, clock = make_bucket(rate=2.0, capacity=3.0)
    for _ in range(3):
        assert bucket.acquire() == 0.0
    assert clock.total_slept == 0.0

    waited = bucket.acquire()
    assert waited == pytest.approx(0.5)  # 1 token at 2/s
    assert clock.now == pytest.approx(1000.5)


def test_sustained_rate_is_exact_over_many_requests() -> None:
    bucket, clock = make_bucket(rate=4.0, capacity=1.0)
    bucket.acquire()  # consumes the initial token
    for _ in range(20):
        bucket.acquire()
    # 20 requests at 4/s after the bucket emptied.
    assert clock.total_slept == pytest.approx(5.0)


def test_refill_is_capped_at_capacity() -> None:
    """An idle bucket does not accumulate an unbounded burst allowance."""
    bucket, clock = make_bucket(rate=1.0, capacity=2.0)
    clock.advance(3600)
    assert bucket.acquire() == 0.0
    assert bucket.acquire() == 0.0
    assert bucket.acquire() > 0.0  # only `capacity` was banked, not 3600


def test_jitter_varies_delay_but_preserves_mean() -> None:
    """+/-30% jitter: every delay differs, the average does not drift."""
    rng = random.Random(1234)
    clock = FakeClock()
    bucket = TokenBucket(
        1.0, 1.0, jitter=0.3, clock=clock.monotonic, sleep=clock.sleep, rng=rng
    )
    bucket.acquire()
    delays = [bucket.acquire() for _ in range(400)]

    assert len({round(d, 6) for d in delays}) > 100, "delays look like a metronome"
    assert min(delays) >= 0.7 - 1e-9
    assert max(delays) <= 1.3 + 1e-9
    assert sum(delays) / len(delays) == pytest.approx(1.0, abs=0.05)


def test_zero_jitter_is_deterministic() -> None:
    bucket, _ = make_bucket(rate=1.0, capacity=1.0, jitter=0.0)
    bucket.acquire()
    assert [bucket.acquire() for _ in range(5)] == [1.0] * 5


def test_set_rate_takes_effect_immediately() -> None:
    """This is what makes the UI slider honest rather than decorative."""
    bucket, _ = make_bucket(rate=1.0, capacity=1.0)
    bucket.acquire()
    assert bucket.acquire() == pytest.approx(1.0)

    assert bucket.set_rate(rate=10.0) is True
    assert bucket.acquire() == pytest.approx(0.1)

    assert bucket.set_rate(rate=10.0) is False, "no-op change should report False"


def test_set_rate_does_not_lose_accrued_tokens() -> None:
    bucket, clock = make_bucket(rate=1.0, capacity=5.0)
    for _ in range(5):
        bucket.acquire()
    clock.advance(3.0)  # 3 tokens accrued
    bucket.set_rate(rate=100.0)
    for _ in range(3):
        assert bucket.acquire() == 0.0


def test_capacity_reduction_clamps_current_tokens() -> None:
    bucket, _ = make_bucket(rate=1.0, capacity=10.0)
    bucket.set_rate(capacity=2.0)
    assert bucket.acquire() == 0.0
    assert bucket.acquire() == 0.0
    assert bucket.acquire() > 0.0


def test_timeout_refuses_rather_than_waiting() -> None:
    bucket, clock = make_bucket(rate=0.1, capacity=1.0)
    bucket.acquire()
    with pytest.raises(RateLimitTimeout):
        bucket.acquire(timeout=1.0)
    assert clock.total_slept == 0.0, "a refused acquire must not sleep"


def test_refused_acquire_does_not_consume_budget() -> None:
    """A timeout must not silently reserve capacity it never used."""
    bucket, _ = make_bucket(rate=1.0, capacity=1.0)
    bucket.acquire()
    with pytest.raises(RateLimitTimeout):
        bucket.acquire(timeout=0.1)
    assert bucket.acquire() == pytest.approx(1.0)


def test_stop_interrupts_a_long_wait() -> None:
    """A stop request must not have to wait out a 100-second delay."""
    bucket, clock = make_bucket(rate=0.01, capacity=1.0)
    bucket.acquire()
    with pytest.raises(Stopped):
        bucket.acquire(should_stop=lambda: True)
    assert clock.total_slept == 0.0


def test_stop_checked_partway_through_a_wait() -> None:
    bucket, clock = make_bucket(rate=1.0, capacity=1.0)
    bucket.acquire()
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(Stopped):
        bucket.acquire(should_stop=should_stop)
    assert clock.total_slept < 1.0, "should have bailed out early"


def test_concurrent_acquire_respects_aggregate_rate() -> None:
    """Real threads, real clock: N requests take at least (N - burst) / rate."""
    import time

    bucket = TokenBucket(50.0, 2.0)
    started = time.monotonic()
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        for _ in range(5):
            bucket.acquire()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    elapsed = time.monotonic() - started

    # 40 requests, burst 2, 50/s -> at least 0.76s of enforced spacing.
    assert elapsed >= 0.70, f"aggregate rate not enforced across threads ({elapsed:.3f}s)"


def test_invalid_construction_is_rejected() -> None:
    for kwargs in ({"rate": 0.0}, {"rate": -1.0}):
        with pytest.raises(ValueError):
            TokenBucket(capacity=1.0, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TokenBucket(1.0, 0.0)
    with pytest.raises(ValueError):
        TokenBucket(1.0, 1.0, jitter=1.5)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


def make_breaker(
    *,
    threshold: int = 3,
    schedule: tuple[float, ...] = (300, 600, 1200, 2400, 3600),
    max_reopens: int = 5,
) -> tuple[CircuitBreaker, FakeClock, list[BreakerStatus]]:
    clock = FakeClock()
    seen: list[BreakerStatus] = []
    breaker = CircuitBreaker(
        consecutive_blocks_to_open=threshold,
        cooldown_schedule_seconds=schedule,
        max_reopens=max_reopens,
        clock=clock.monotonic,
        sleep=clock.sleep,
        on_state_change=seen.append,
    )
    return breaker, clock, seen


def test_opens_only_after_consecutive_blocks() -> None:
    breaker, _, _ = make_breaker(threshold=3)
    assert breaker.record_block() is BreakerState.CLOSED
    assert breaker.record_block() is BreakerState.CLOSED
    assert breaker.record_block() is BreakerState.OPEN


def test_success_resets_the_streak() -> None:
    """Two blocks then a success must not leave the breaker one away from open."""
    breaker, _, _ = make_breaker(threshold=3)
    breaker.record_block()
    breaker.record_block()
    breaker.record_success()
    assert breaker.record_block() is BreakerState.CLOSED
    assert breaker.record_block() is BreakerState.CLOSED
    assert breaker.record_block() is BreakerState.OPEN


def test_open_breaker_parks_callers_for_the_cooldown() -> None:
    breaker, clock, _ = make_breaker(threshold=1, schedule=(300,))
    breaker.record_block()
    assert breaker.state is BreakerState.OPEN

    is_canary = breaker.before_request()
    assert is_canary is True
    assert clock.total_slept == pytest.approx(300.0)
    assert breaker.state is BreakerState.HALF_OPEN


def test_successful_canary_closes_the_breaker() -> None:
    breaker, _, _ = make_breaker(threshold=1, schedule=(300,))
    breaker.record_block()
    assert breaker.before_request() is True
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.before_request() is False  # no more parking


def test_cooldown_escalates_then_repeats_the_last_value() -> None:
    """5 -> 10 -> 20 -> 40 -> 60 minutes, then 60 forever."""
    schedule = (300.0, 600.0, 1200.0, 2400.0, 3600.0)
    breaker, clock, _ = make_breaker(threshold=1, schedule=schedule, max_reopens=8)

    breaker.record_block()
    observed: list[float] = []
    for _ in range(7):
        before = clock.total_slept
        breaker.before_request()          # waits out the current cooldown
        observed.append(clock.total_slept - before)
        breaker.record_block()            # canary refused -> escalate

    assert observed == pytest.approx([300, 600, 1200, 2400, 3600, 3600, 3600])


def test_exhaustion_raises_and_leaves_work_queued() -> None:
    breaker, _, _ = make_breaker(threshold=1, schedule=(10,), max_reopens=3)
    breaker.record_block()
    for _ in range(2):
        breaker.before_request()
        breaker.record_block()

    breaker.before_request()
    breaker.record_block()  # third failed reopen -> exhausted
    assert breaker.state is BreakerState.EXHAUSTED

    with pytest.raises(CircuitExhausted) as info:
        breaker.before_request()
    assert info.value.reopens == 3


def test_only_one_canary_is_released() -> None:
    """The retest is a single request, not a stampede of every parked worker."""
    breaker, clock, _ = make_breaker(threshold=1, schedule=(0,))
    breaker.record_block()

    canaries = [breaker.before_request() for _ in range(1)]
    assert canaries == [True]

    # A second caller finds the canary slot taken and parks instead.
    parked: list[bool] = []
    stop = {"flag": False}

    def second() -> None:
        try:
            parked.append(breaker.before_request(should_stop=lambda: stop["flag"]))
        except Stopped:
            parked.append(False)

    thread = threading.Thread(target=second)
    thread.start()
    clock.advance(1.0)
    stop["flag"] = True
    thread.join(timeout=5)
    assert parked == [False], "a second thread must not also become the canary"


def test_breaker_cannot_touch_video_status() -> None:
    """Structural guarantee, not a behavioural one.

    The breaker has no video id, no database handle, and no status vocabulary in
    its API. An IP block therefore *cannot* mark videos failed, which is the
    failure mode that would otherwise poison thousands of rows per bad
    afternoon.
    """
    breaker, _, _ = make_breaker()
    public = {n for n in dir(breaker) if not n.startswith("_")}
    assert public == {
        "before_request", "cooldown_for", "record_block", "record_success",
        "reset", "state", "status", "trip",
    }
    for name in public:
        member = getattr(CircuitBreaker, name, None)
        if callable(member) and member.__doc__:
            assert "videos" not in member.__doc__.lower() or "status" not in name


def test_status_reports_time_until_retest() -> None:
    breaker, clock, _ = make_breaker(threshold=1, schedule=(300,))
    breaker.record_block()
    assert breaker.status().seconds_until_retest == pytest.approx(300.0)
    clock.advance(120)
    assert breaker.status().seconds_until_retest == pytest.approx(180.0)
    assert breaker.status().state is BreakerState.OPEN


def test_state_change_callback_fires_for_the_ui() -> None:
    breaker, _, seen = make_breaker(threshold=2, schedule=(60,))
    breaker.record_block()
    assert seen == [], "no state change yet, so no notification"
    breaker.record_block()
    assert [s.state for s in seen] == [BreakerState.OPEN]
    breaker.before_request()
    breaker.record_success()
    assert [s.state for s in seen] == [
        BreakerState.OPEN, BreakerState.HALF_OPEN, BreakerState.CLOSED
    ]


def test_trip_opens_immediately() -> None:
    breaker, _, _ = make_breaker(threshold=99)
    assert breaker.trip() is BreakerState.OPEN


def test_reset_clears_escalation() -> None:
    breaker, _, _ = make_breaker(threshold=1, schedule=(10, 20), max_reopens=2)
    breaker.record_block()
    breaker.before_request()
    breaker.record_block()
    breaker.reset()
    assert breaker.state is BreakerState.CLOSED
    assert breaker.status().reopens == 0
    assert breaker.cooldown_for(0) == 10


def test_stop_escapes_an_open_breaker() -> None:
    breaker, clock, _ = make_breaker(threshold=1, schedule=(3600,))
    breaker.record_block()
    with pytest.raises(Stopped):
        breaker.before_request(should_stop=lambda: True)
    assert clock.total_slept == 0.0


def test_invalid_breaker_construction() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(consecutive_blocks_to_open=0)
    with pytest.raises(ValueError):
        CircuitBreaker(cooldown_schedule_seconds=())
    with pytest.raises(ValueError):
        CircuitBreaker(max_reopens=0)


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #


def test_full_jitter_stays_within_the_envelope() -> None:
    rng = random.Random(7)
    for attempt in range(6):
        ceiling = min(300.0, 2.0 * (2**attempt))
        for _ in range(50):
            delay = full_jitter_backoff(attempt, base=2.0, cap=300.0, rng=rng)
            assert 0.0 <= delay <= ceiling


def test_full_jitter_is_capped() -> None:
    rng = ScriptedRandom([1.0])
    assert full_jitter_backoff(30, base=2.0, cap=300.0, rng=rng) == pytest.approx(300.0)


def test_full_jitter_spreads_retries() -> None:
    """Identical retry times are how one transient error becomes a herd."""
    rng = random.Random(11)
    delays = {full_jitter_backoff(4, rng=rng) for _ in range(50)}
    assert len(delays) > 40


def test_retry_after_is_a_floor() -> None:
    rng = ScriptedRandom([0.0])
    assert full_jitter_backoff(0, rng=rng, retry_after=90.0) == pytest.approx(90.0)
    # ...but never shortens a longer computed delay.
    rng = ScriptedRandom([1.0])
    assert full_jitter_backoff(8, base=2.0, cap=300.0, rng=rng, retry_after=5.0) == (
        pytest.approx(300.0)
    )
