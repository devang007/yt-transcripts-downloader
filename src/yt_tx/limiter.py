"""Token bucket, circuit breaker, and backoff. Pure logic, injectable clock.

No network and no database here, which is what makes the awkward timing
behaviour testable with a fake clock instead of a stopwatch and hope.

Division of responsibility: the bucket knows a rate, not where the rate came
from. The worker polls ``runtime_control`` every couple of seconds and calls
:meth:`TokenBucket.set_rate`, so the UI's slider takes effect mid-run without
this module ever touching a connection.

The breaker's contract is the important part. When it opens, workers park and
**no video status changes**. A block means YouTube is refusing us; the videos
are fine and stay queued. Cooling down and retrying with a single canary is the
correct response, and giving up after enough failed reopens - with everything
still queued and a non-zero exit code - is better than grinding through 40,000
videos marking them all failed.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]

DEFAULT_POLL_INTERVAL: Final = 0.25


class RateLimitTimeout(RuntimeError):
    """A token could not be acquired inside the caller's deadline."""


class CircuitExhausted(RuntimeError):
    """The breaker reopened too many times. Stop the run; leave work queued."""

    def __init__(self, reopens: int) -> None:
        self.reopens = reopens
        super().__init__(
            f"circuit breaker failed to close after {reopens} reopen attempts; "
            "stopping with all work still queued"
        )


class Stopped(RuntimeError):
    """A stop was requested while parked in the limiter or breaker."""


# --------------------------------------------------------------------------- #
# Token bucket
# --------------------------------------------------------------------------- #


class TokenBucket:
    """Thread-safe token bucket with reservation-based waiting and jitter.

    Callers that cannot proceed immediately reserve future capacity and then
    sleep *outside* the lock. Sleeping while holding it would serialise every
    worker thread behind whichever one happened to arrive first; reserving keeps
    the aggregate rate exact while letting threads overlap their waits.

    Jitter is applied to the computed delay, not to the accounting, so the
    long-run rate is unaffected while the inter-request spacing stops looking
    like a metronome. Constant intervals are a fingerprint.
    """

    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        jitter: float = 0.0,
        clock: MonotonicClock = time.monotonic,
        sleep: Sleeper = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        if not 0.0 <= jitter <= 1.0:
            raise ValueError("jitter must be within [0, 1]")
        self._rate = rate
        self._capacity = capacity
        self._jitter = jitter
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._tokens = capacity  # start full: the burst allowance is real
        self._updated = clock()

    @property
    def rate(self) -> float:
        with self._lock:
            return self._rate

    @property
    def capacity(self) -> float:
        with self._lock:
            return self._capacity

    def set_rate(self, rate: float | None = None, capacity: float | None = None) -> bool:
        """Change the sustained rate and/or burst size mid-run.

        Returns:
            True if anything actually changed, so the caller can log it once
            rather than on every poll.
        """
        with self._lock:
            changed = False
            if rate is not None and rate > 0 and rate != self._rate:
                self._refill_locked(self._clock())
                self._rate = rate
                changed = True
            if capacity is not None and capacity > 0 and capacity != self._capacity:
                self._capacity = capacity
                self._tokens = min(self._tokens, capacity)
                changed = True
            return changed

    def set_jitter(self, jitter: float) -> None:
        with self._lock:
            self._jitter = min(1.0, max(0.0, jitter))

    def _refill_locked(self, now: float) -> None:
        elapsed = now - self._updated
        if elapsed <= 0:
            # Either the clock did not move or a reservation pushed ``_updated``
            # into the future. Both mean there is nothing to add.
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    def _reserve(self, tokens: float, timeout: float | None) -> float:
        """Reserve capacity and return how long the caller must sleep.

        ``_updated`` doubles as the *reservation frontier*: the time at which the
        recorded token count becomes true. Waiters queue by pushing that frontier
        further out, and each new caller measures its wait from the frontier
        rather than from its own arrival. Measuring from arrival was a real bug -
        every thread then waited only ``1/rate`` from the moment it showed up, so
        eight workers quietly ran at eight times the configured rate.

        Jitter is folded in here, under the lock, so the bookkeeping matches what
        the caller actually sleeps. Jittering the returned delay afterwards let
        the frontier drift out of step with the clock and broke the bounds.
        """
        with self._lock:
            now = self._clock()
            self._refill_locked(now)
            # After the refill, ``_updated`` is either ``now`` (clock caught up)
            # or still in the reserved future, where ``_tokens`` is 0 by
            # construction.
            base = max(now, self._updated)

            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0

            deficit = tokens - self._tokens
            wait_from_base = self._jittered(deficit / self._rate)
            ready_at = base + wait_from_base
            delay = max(0.0, ready_at - now)

            if timeout is not None and delay > timeout:
                # Raise before mutating: a refused acquire must not consume
                # budget it never used.
                raise RateLimitTimeout(
                    f"need {delay:.2f}s of rate-limit budget, "
                    f"deadline is {timeout:.2f}s"
                )

            self._tokens = 0.0
            self._updated = ready_at
            return delay

    def _jittered(self, delay: float) -> float:
        if delay <= 0 or self._jitter <= 0:
            return delay
        factor = 1.0 + self._rng.uniform(-self._jitter, self._jitter)
        return max(0.0, delay * factor)

    def acquire(
        self,
        tokens: float = 1.0,
        *,
        timeout: float | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> float:
        """Block until ``tokens`` are available, then return seconds slept.

        Args:
            tokens: Cost of the upcoming request.
            timeout: Give up (raising :class:`RateLimitTimeout`) rather than
                waiting longer than this.
            should_stop: Polled while sleeping so a stop request does not have to
                wait out a long delay.

        Raises:
            RateLimitTimeout: The wait would exceed ``timeout``.
            Stopped: ``should_stop`` returned True mid-sleep.
        """
        wait = self._reserve(tokens, timeout)
        if wait <= 0:
            return 0.0
        self._sleep_interruptibly(wait, should_stop)
        return wait

    def _sleep_interruptibly(
        self, seconds: float, should_stop: Callable[[], bool] | None
    ) -> None:
        if should_stop is None:
            self._sleep(seconds)
            return
        remaining = seconds
        while remaining > 0:
            if should_stop():
                raise Stopped("stop requested while rate limited")
            chunk = min(DEFAULT_POLL_INTERVAL, remaining)
            self._sleep(chunk)
            remaining -= chunk


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class BreakerStatus:
    """Snapshot for the UI header, which must show an open breaker loudly."""

    state: BreakerState
    consecutive_blocks: int
    reopens: int
    seconds_until_retest: float
    total_blocks: int


class CircuitBreaker:
    """Trips after N consecutive blocks; retests with exactly one canary.

    Waiting is done with an injectable ``sleep`` rather than
    ``threading.Condition.wait`` specifically so a fake clock can drive the
    whole escalation schedule in a unit test. The cost is a quarter-second poll,
    which is irrelevant next to a five-minute cooldown.

    This class never sees a video id and cannot change a video's status. That is
    not an accident - it is the property that keeps an IP block from poisoning
    thousands of rows.
    """

    def __init__(
        self,
        *,
        consecutive_blocks_to_open: int = 3,
        cooldown_schedule_seconds: Sequence[float] = (300, 600, 1200, 2400, 3600),
        max_reopens: int = 5,
        clock: MonotonicClock = time.monotonic,
        sleep: Sleeper = time.sleep,
        on_state_change: Callable[[BreakerStatus], None] | None = None,
    ) -> None:
        if consecutive_blocks_to_open < 1:
            raise ValueError("consecutive_blocks_to_open must be >= 1")
        if not cooldown_schedule_seconds:
            raise ValueError("cooldown_schedule_seconds must not be empty")
        if max_reopens < 1:
            raise ValueError("max_reopens must be >= 1")
        self._threshold = consecutive_blocks_to_open
        self._schedule = tuple(float(s) for s in cooldown_schedule_seconds)
        self._max_reopens = max_reopens
        self._clock = clock
        self._sleep = sleep
        self._on_change = on_state_change

        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._consecutive = 0
        self._reopens = 0
        self._total_blocks = 0
        self._open_until = 0.0
        self._canary_claimed = False

    # -- introspection ----------------------------------------------------- #

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state

    def status(self) -> BreakerStatus:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> BreakerStatus:
        remaining = 0.0
        if self._state is BreakerState.OPEN:
            remaining = max(0.0, self._open_until - self._clock())
        return BreakerStatus(
            state=self._state,
            consecutive_blocks=self._consecutive,
            reopens=self._reopens,
            seconds_until_retest=remaining,
            total_blocks=self._total_blocks,
        )

    def cooldown_for(self, reopens: int) -> float:
        """Cooldown for the given reopen count; the last value repeats."""
        return self._schedule[min(reopens, len(self._schedule) - 1)]

    # -- transitions ------------------------------------------------------- #

    def record_success(self) -> None:
        """A request succeeded: reset the streak, and close if this was a canary."""
        notify = False
        with self._lock:
            self._consecutive = 0
            if self._state is BreakerState.HALF_OPEN:
                self._state = BreakerState.CLOSED
                self._reopens = 0
                self._canary_claimed = False
                notify = True
            status = self._status_locked()
        if notify:
            self._emit(status)

    def record_block(self) -> BreakerState:
        """Record a HardBlock. Returns the state the breaker settled into."""
        with self._lock:
            self._consecutive += 1
            self._total_blocks += 1
            previous = self._state

            if self._state is BreakerState.HALF_OPEN:
                # The canary was refused too: escalate.
                self._reopens += 1
                self._canary_claimed = False
                if self._reopens >= self._max_reopens:
                    self._state = BreakerState.EXHAUSTED
                else:
                    self._open_locked()
            elif self._state is BreakerState.CLOSED:
                if self._consecutive >= self._threshold:
                    self._open_locked()

            status = self._status_locked()
            changed = status.state is not previous
        if changed:
            self._emit(status)
        return status.state

    def _open_locked(self) -> None:
        self._state = BreakerState.OPEN
        self._open_until = self._clock() + self.cooldown_for(self._reopens)
        self._canary_claimed = False

    def trip(self) -> BreakerState:
        """Open immediately, regardless of the streak. Used by the quota guard."""
        with self._lock:
            previous = self._state
            if self._state in (BreakerState.CLOSED, BreakerState.HALF_OPEN):
                self._consecutive = self._threshold
                self._open_locked()
            status = self._status_locked()
            changed = status.state is not previous
        if changed:
            self._emit(status)
        return status.state

    def reset(self) -> None:
        """Force closed. Only for an explicit operator resume."""
        with self._lock:
            self._state = BreakerState.CLOSED
            self._consecutive = 0
            self._reopens = 0
            self._open_until = 0.0
            self._canary_claimed = False
            status = self._status_locked()
        self._emit(status)

    # -- the gate ---------------------------------------------------------- #

    def before_request(
        self, *, should_stop: Callable[[], bool] | None = None
    ) -> bool:
        """Block while the breaker is open. Returns True if this call is the canary.

        Exactly one waiting thread is released into the half-open state; the
        rest keep parking. A canary that succeeds closes the breaker for
        everyone, and a canary that is blocked reopens it with the next, longer
        cooldown.

        Raises:
            CircuitExhausted: reopened ``max_reopens`` times; the run must end.
            Stopped: ``should_stop`` returned True while parked.
        """
        while True:
            if should_stop is not None and should_stop():
                raise Stopped("stop requested while circuit breaker was open")

            promoted: BreakerStatus | None = None
            with self._lock:
                if self._state is BreakerState.EXHAUSTED:
                    raise CircuitExhausted(self._reopens)
                if self._state is BreakerState.CLOSED:
                    return False
                if self._state is BreakerState.OPEN:
                    remaining = self._open_until - self._clock()
                    if remaining <= 0:
                        # Cooldown elapsed: promote and take the canary slot.
                        self._state = BreakerState.HALF_OPEN
                        self._canary_claimed = True
                        promoted = self._status_locked()
                        wait = 0.0
                    else:
                        wait = min(DEFAULT_POLL_INTERVAL, remaining)
                else:  # HALF_OPEN
                    if not self._canary_claimed:
                        self._canary_claimed = True
                        return True
                    wait = DEFAULT_POLL_INTERVAL

            # Emit outside the lock: the callback logs, and may take its own.
            if promoted is not None:
                self._emit(promoted)
                return True
            self._sleep(wait)

    def _emit(self, status: BreakerStatus) -> None:
        if self._on_change is not None:
            self._on_change(status)


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #


def full_jitter_backoff(
    attempt: int,
    *,
    base: float = 2.0,
    cap: float = 300.0,
    rng: random.Random | None = None,
    retry_after: float | None = None,
) -> float:
    """``random(0, min(cap, base * 2**attempt))``, honouring ``Retry-After``.

    Full jitter rather than the exponential value itself: after a shared
    failure, retrying at identical times is how a fleet turns one transient
    error into a synchronised thundering herd.

    Args:
        attempt: Attempts already made (0 for the first retry).
        base: Multiplier.
        cap: Upper bound on the exponential term.
        rng: Injectable for tests.
        retry_after: If the server said how long to wait, never wait less.
    """
    source = rng or random.Random()
    ceiling = min(cap, base * (2 ** max(0, attempt)))
    delay = source.uniform(0.0, max(0.0, ceiling))
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay
