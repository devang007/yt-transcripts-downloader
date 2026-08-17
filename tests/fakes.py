"""Test doubles shared across the suite. No network, no clock, no database."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from typing import Any


class FakeClock:
    """A monotonic clock that only moves when a sleeper asks it to.

    ``sleep`` advances the clock instead of blocking, which lets a test drive a
    five-minute circuit-breaker cooldown in microseconds and assert on the exact
    escalation schedule rather than approximating it.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise AssertionError(f"negative sleep: {seconds}")
        with self._lock:
            self.slept.append(seconds)
            self.now += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


class ScriptedRandom:
    """A ``random.Random`` stand-in returning a fixed sequence.

    Only the methods this project actually calls are implemented; anything else
    raising is a feature, because it means a test is depending on randomness it
    did not pin.
    """

    def __init__(self, values: Sequence[float]) -> None:
        self._values = list(values)
        self._index = 0

    def _next(self) -> float:
        if not self._values:
            return 0.0
        value = self._values[self._index % len(self._values)]
        self._index += 1
        return value

    def uniform(self, a: float, b: float) -> float:
        """Interpret the scripted value as a position in [0, 1] across [a, b]."""
        return a + (b - a) * self._next()

    def random(self) -> float:
        return self._next()


def iter_lines(text: str) -> Iterator[dict[str, Any]]:
    import json

    for line in text.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)
