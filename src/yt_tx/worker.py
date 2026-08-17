"""The worker process: thread pool, live control, and orderly shutdown.

This never runs inside the web process. The API spawns it as a subprocess and
then talks to it only through MySQL (``runtime_control``) and the run's JSONL log.
A hot reload, a dropped browser tab, or a restarted API must not be able to kill
a four-hour harvest.

Three background concerns run on a single supervisor thread:

* **control polling** every 2s - pause, stop, concurrency and requests/second are
  re-read from ``runtime_control`` so the UI's sliders actually do something to a
  running job;
* **the lease reaper** every 60s - rows whose worker vanished go back on the
  queue;
* **heartbeats** - so the API can tell a live run from a dead one.

Shutdown is the part worth reading. Stop claiming, let in-flight work finish and
commit, release any leases still held, close the ``runs`` row with an accurate
``exit_reason``, and exit with a code that says what happened. Ctrl-C at any
moment leaves the database consistent and every unfinished video queued.
"""

from __future__ import annotations

import os
import random
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from .classify import HardBlock, QuotaExhausted
from .fetch import FetchConfig, TranscriptFetcher, fetch_video, make_fetcher
from .hydrate import SkipRules, hydrate
from .limiter import (
    BreakerState,
    BreakerStatus,
    CircuitBreaker,
    CircuitExhausted,
    Stopped,
    TokenBucket,
)
from .logs import bind, context, get_logger
from .repo import Repo
from .settings import Bootstrap, Knobs
from .states import ExitReason, Status
from .youtube_api import QuotaGuard, YouTubeAPI

log = get_logger(__name__)

CONTROL_POLL_SECONDS: Final = 2.0
REAP_INTERVAL_SECONDS: Final = 60.0
CLAIM_IDLE_SLEEP: Final = 2.0
WORKER_CEILING: Final = 32
"""Hard cap on pool threads, so a runaway ``concurrency`` cannot fork-bomb."""

EXIT_CODES: Final[dict[ExitReason, int]] = {
    ExitReason.COMPLETED: 0,
    # Expected daily behaviour for a cron: work remains, tomorrow's quota gets it.
    ExitReason.QUOTA_EXHAUSTED: 0,
    ExitReason.STOPPED: 0,
    ExitReason.INTERRUPTED: 130,
    ExitReason.CIRCUIT_OPEN: 4,
    ExitReason.CRASHED: 1,
}


def worker_id() -> str:
    """Stable-per-process identity for the ``claimed_by`` lease column."""
    return f"{socket.gethostname()[:40]}:{os.getpid()}"


# --------------------------------------------------------------------------- #
# Dynamic concurrency
# --------------------------------------------------------------------------- #


class DynamicGate:
    """A semaphore whose limit can change while threads are waiting on it.

    ``ThreadPoolExecutor`` cannot be resized, so the pool is created at the
    ceiling and this gate enforces the *live* limit. Lowering it does not
    interrupt work already running - it just stops new work starting until the
    active count falls below the new limit.
    """

    def __init__(self, limit: int) -> None:
        self._limit = max(1, limit)
        self._active = 0
        self._cond = threading.Condition()

    @property
    def limit(self) -> int:
        with self._cond:
            return self._limit

    @property
    def active(self) -> int:
        with self._cond:
            return self._active

    def set_limit(self, limit: int) -> bool:
        limit = max(1, min(WORKER_CEILING, limit))
        with self._cond:
            if limit == self._limit:
                return False
            self._limit = limit
            self._cond.notify_all()
            return True

    @contextmanager
    def slot(self, should_stop: Callable[[], bool]) -> Iterator[None]:
        with self._cond:
            while self._active >= self._limit:
                if should_stop():
                    raise Stopped("stop requested while waiting for a worker slot")
                self._cond.wait(timeout=0.25)
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    def drain(self, timeout: float) -> bool:
        """Wait for active work to finish. Returns True if it did."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._active > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=min(0.25, remaining))
            return True


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #


_STATUS_COUNTER_FIELDS: Final[dict[str, str]] = {
    Status.TRANSCRIPT_OK.value: "transcript_ok",
    Status.NO_TRANSCRIPT.value: "no_transcript",
    Status.LANG_MISSING.value: "lang_missing",
    Status.UNAVAILABLE.value: "unavailable",
    Status.AGE_RESTRICTED.value: "age_restricted",
    Status.SKIPPED.value: "skipped",
    Status.RETRY.value: "retried",
    Status.FAILED.value: "failed",
}


@dataclass
class Counters:
    """Thread-safe tallies, surfaced in ``runs.counts_json`` and the UI."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    discovered: int = 0
    hydrated: int = 0
    transcript_ok: int = 0
    no_transcript: int = 0
    lang_missing: int = 0
    unavailable: int = 0
    age_restricted: int = 0
    skipped: int = 0
    retried: int = 0
    failed: int = 0
    blocked: int = 0

    def note_status(self, status: Status | None) -> None:
        if status is None:
            return
        attribute = _STATUS_COUNTER_FIELDS.get(status.value)
        if attribute is None:
            return
        with self._lock:
            setattr(self, attribute, getattr(self, attribute) + 1)

    def bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "discovered": self.discovered,
                "hydrated": self.hydrated,
                "transcript_ok": self.transcript_ok,
                "no_transcript": self.no_transcript,
                "lang_missing": self.lang_missing,
                "unavailable": self.unavailable,
                "age_restricted": self.age_restricted,
                "skipped": self.skipped,
                "retried": self.retried,
                "failed": self.failed,
                "blocked": self.blocked,
            }


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


class Pipeline:
    """Owns one run: its limiter, breaker, control loop and thread pool."""

    def __init__(
        self,
        repo: Repo,
        bootstrap: Bootstrap,
        knobs: Knobs,
        *,
        run_id: int | None = None,
    ) -> None:
        self.repo = repo
        self.bootstrap = bootstrap
        self.knobs = knobs
        self.run_id = run_id
        self.worker = worker_id()
        self.counters = Counters()

        self._stop = threading.Event()
        # Separate from _stop on purpose. _stop means "the operator or a signal
        # asked us to wind down", and exit_reason() reports on it. This one only
        # tells the supervisor thread to exit when the work is over. Sharing one
        # event made every clean run record exit_reason='stopped', because
        # supervised() set it on the way out and exit_reason() then read it back.
        self._supervisor_stop = threading.Event()
        self._pause = threading.Event()
        self._interrupted = False
        self._exit_reason: ExitReason | None = None

        self.bucket = TokenBucket(
            knobs.requests_per_second,
            float(knobs.burst),
            jitter=knobs.jitter,
            rng=random.Random(),
        )
        self.breaker = CircuitBreaker(
            consecutive_blocks_to_open=knobs.consecutive_blocks_to_open,
            cooldown_schedule_seconds=knobs.cooldown_schedule_seconds,
            max_reopens=knobs.max_reopens,
            on_state_change=self._on_breaker_change,
        )
        self.gate = DynamicGate(knobs.concurrency)
        self._supervisor: threading.Thread | None = None

        bind(run_id=run_id, worker=self.worker)

    # -- lifecycle --------------------------------------------------------- #

    def should_stop(self) -> bool:
        return self._stop.is_set()

    def request_stop(self, *, interrupted: bool = False) -> None:
        if interrupted:
            self._interrupted = True
        self._stop.set()

    def install_signal_handlers(self) -> None:
        """SIGINT and SIGTERM both mean *drain*, not *die*."""

        def handler(signum: int, _frame: Any) -> None:
            name = signal.Signals(signum).name
            if self.should_stop():
                log.warning("second signal; exiting immediately", signal=name)
                raise KeyboardInterrupt
            log.warning("signal received; draining in-flight work", signal=name)
            self.request_stop(interrupted=True)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:
                # Not on the main thread (e.g. under a test runner); the caller
                # is responsible for stopping us in that case.
                log.debug("could not install signal handler", signal=sig)

    @contextmanager
    def supervised(self) -> Iterator[None]:
        """Run the control/reaper/heartbeat thread for the duration of a block."""
        self._supervisor = threading.Thread(
            target=self._supervise, name="yt-tx-supervisor", daemon=True
        )
        self._supervisor.start()
        try:
            yield
        finally:
            # Stop supervising without claiming the run was stopped.
            self._supervisor_stop.set()
            if self._supervisor is not None:
                self._supervisor.join(timeout=5)

    def _supervise(self) -> None:
        last_reap = 0.0
        while not self._supervisor_stop.wait(CONTROL_POLL_SECONDS):
            try:
                self._poll_control()
                now = time.monotonic()
                if now - last_reap >= REAP_INTERVAL_SECONDS:
                    last_reap = now
                    reaped = self.repo.reap_expired_leases()
                    if reaped:
                        log.warning("reclaimed abandoned leases", count=reaped)
                if self.run_id is not None:
                    self.repo.heartbeat(self.run_id, self.status_snapshot())
            except Exception as exc:  # noqa: BLE001 - supervision must not die
                log.error(
                    "supervisor iteration failed",
                    error=str(exc)[:300], error_type=type(exc).__name__,
                )

    def _poll_control(self) -> None:
        control = self.repo.get_control()
        if control.should_stop and not self.should_stop():
            log.warning("stop requested via runtime_control")
            self.request_stop()
        if control.is_paused:
            if not self._pause.is_set():
                log.warning("paused via runtime_control")
                self._pause.set()
        elif self._pause.is_set():
            log.info("resumed via runtime_control")
            self._pause.clear()

        if self.bucket.set_rate(rate=float(control.requests_per_second)):
            log.info("rate limit updated", requests_per_second=control.requests_per_second)
        if self.gate.set_limit(int(control.concurrency)):
            log.info("concurrency updated", concurrency=control.concurrency)

    def status_snapshot(self) -> dict[str, Any]:
        """Counters plus breaker state, for ``runs.counts_json``.

        The breaker lives in the worker process, so the heartbeat is how the UI
        learns about it. Scraping it out of log text would work until the day a
        log line got reworded.
        """
        breaker = self.breaker.status()
        return {
            **self.counters.snapshot(),
            "breaker": {
                "state": breaker.state.value,
                "reopens": breaker.reopens,
                "total_blocks": breaker.total_blocks,
                "seconds_until_retest": round(breaker.seconds_until_retest),
            },
            "concurrency": self.gate.limit,
            "active": self.gate.active,
            "requests_per_second": round(self.bucket.rate, 3),
        }

    def _on_breaker_change(self, status: BreakerStatus) -> None:
        log.warning(
            "circuit breaker",
            state=status.state.value,
            reopens=status.reopens,
            cooldown_seconds=round(status.seconds_until_retest),
            total_blocks=status.total_blocks,
        )

    def wait_while_paused(self) -> None:
        while self._pause.is_set() and not self.should_stop():
            time.sleep(0.25)

    def before_request(self) -> None:
        """Gate every outbound request through the breaker and the bucket."""
        self.breaker.before_request(should_stop=self.should_stop)
        self.bucket.acquire(should_stop=self.should_stop)

    # -- stages ------------------------------------------------------------ #

    def make_api(self) -> YouTubeAPI | None:
        """Data API client, or ``None`` when no key is configured."""
        if not self.knobs.youtube_api_key:
            log.warning(
                "no Data API key configured; falling back to yt-dlp for "
                "enumeration and metadata (slower, but no quota)"
            )
            return None
        guard = QuotaGuard(
            charge=self.repo.add_quota,
            budget=self.knobs.daily_quota_units,
            stop_at_pct=self.knobs.quota_stop_at_pct,
        )
        return YouTubeAPI(
            self.knobs.youtube_api_key,
            quota=guard,
            before_request=self.before_request,
            proxy=self.knobs.proxy,
        )

    def _handle_stage_block(self, exc: HardBlock, stage: str) -> None:
        """A block outside the fetch pool. Stop the stage; conclude nothing.

        Discovery and hydration are single-threaded, so there is no pool to park.
        The useful response is to record the block, leave every row exactly as it
        was, and end the run with a reason that says work remains.
        """
        self.counters.bump("blocked")
        state = self.breaker.record_block()
        log.error(
            "blocked during %s; stopping with all work still queued" % stage,
            detail=str(exc)[:300], breaker=state.value, http_status=exc.http_status,
        )
        self._exit_reason = ExitReason.CIRCUIT_OPEN
        self.request_stop()

    def discover(
        self,
        *,
        channel_id: str | None = None,
        incremental: bool = False,
        limit: int | None = None,
    ) -> None:
        from .discover import discover_all

        api = self.make_api()
        try:
            results = discover_all(
                self.repo,
                api=api,
                channel_id=channel_id,
                incremental=incremental,
                limit=limit,
                include_shorts=self.knobs.include_shorts,
                include_streams=self.knobs.include_streams,
                cookies_file=self.knobs.cookies_file,
                proxy=self.knobs.proxy,
                should_stop=self.should_stop,
            )
        except HardBlock as exc:
            self._handle_stage_block(exc, "discovery")
            return
        finally:
            if api is not None:
                api.close()

        for result in results:
            self.counters.bump("discovered", result.new)
            for warning in result.warnings:
                log.warning("discovery warning", channel_id=result.channel_id,
                            detail=warning)
        log.info(
            "discovery complete",
            channels=len(results),
            new_videos=self.counters.snapshot()["discovered"],
            skipped=sum(1 for r in results if r.was_skipped),
        )

    def hydrate(
        self, *, channel_id: str | None = None, limit: int | None = None
    ) -> None:
        api = self.make_api()
        rules = SkipRules(
            max_duration_seconds=self.knobs.max_duration_seconds,
            include_shorts=self.knobs.include_shorts,
            include_streams=self.knobs.include_streams,
        )
        try:
            result = hydrate(
                self.repo,
                api=api,
                rules=rules,
                channel_id=channel_id,
                limit=limit,
                cookies_file=self.knobs.cookies_file,
                proxy=self.knobs.proxy,
                should_stop=self.should_stop,
            )
        except HardBlock as exc:
            self._handle_stage_block(exc, "hydration")
            return
        finally:
            if api is not None:
                api.close()

        self.counters.bump("hydrated", result.hydrated)
        self.counters.bump("skipped", result.skipped)
        self.counters.bump("unavailable", result.unavailable)
        log.info(
            "hydration complete",
            requested=result.requested, hydrated=result.hydrated,
            skipped=result.skipped, unavailable=result.unavailable,
            deferred=result.deferred, api_calls=result.calls, reasons=result.reasons,
        )

    def fetch(
        self,
        *,
        channel_id: str | None = None,
        limit: int | None = None,
        dry_run: bool = False,
        skip_hydrate: bool = True,
    ) -> None:
        """Claim and fetch transcripts until the queue drains or we are stopped.

        ``skip_hydrate`` (the default) also claims ``discovered`` videos, so
        transcripts start landing immediately instead of after the whole
        metadata pass. Nothing in the caption download uses metadata: the cost
        is duration and view counts, and the shorts/streams/duration skip rules.
        """
        # Two housekeeping passes first: reclaim abandoned work, and requeue
        # videos whose recheck has matured.
        reaped = self.repo.reap_expired_leases()
        if reaped:
            log.warning("reclaimed abandoned leases at startup", count=reaped)
        promoted = self.repo.promote_due_rechecks(channel_id=channel_id)
        if promoted:
            log.info("requeued videos for recheck", count=promoted)
        matured = self.repo.unskip_matured_upcoming()
        if matured:
            log.info("re-evaluating premieres whose date has passed", count=matured)

        if dry_run:
            self._dry_run(
                channel_id=channel_id, limit=limit, skip_hydrate=skip_hydrate
            )
            return

        config = FetchConfig(
            transcript_dir=self.bootstrap.transcript_dir,
            languages=self.knobs.languages,
            prefer_manual=self.knobs.prefer_manual,
            store_all_variants=self.knobs.store_all_variants,
            accept_translated=self.knobs.accept_translated,
            max_attempts=self.knobs.max_attempts,
            backoff_base_seconds=self.knobs.backoff_base_seconds,
            backoff_cap_seconds=self.knobs.backoff_cap_seconds,
            cookies_configured=bool(self.knobs.cookies_file),
        )
        fetcher = make_fetcher(
            self.knobs.fetcher,
            cookies_file=self.knobs.cookies_file,
            proxy=self.knobs.proxy,
        )
        log.info(
            "fetch stage starting",
            backend=fetcher.name, concurrency=self.gate.limit,
            requests_per_second=self.bucket.rate, languages=list(config.languages),
            skip_hydrate=skip_hydrate,
        )

        processed = 0
        futures: list[Future[None]] = []
        pool = ThreadPoolExecutor(
            max_workers=WORKER_CEILING, thread_name_prefix="yt-tx-fetch"
        )
        try:
            while not self.should_stop():
                self.wait_while_paused()
                if self.should_stop():
                    break

                remaining = None if limit is None else limit - processed
                if remaining is not None and remaining <= 0:
                    log.info("fetch limit reached", limit=limit)
                    break

                batch_size = max(self.gate.limit * 3, 10)
                if remaining is not None:
                    batch_size = min(batch_size, remaining)

                claimed = self.repo.claim_batch(
                    self.worker,
                    limit=batch_size,
                    lease_seconds=self.knobs.lease_seconds,
                    channel_id=channel_id,
                    include_unhydrated=skip_hydrate,
                )
                if not claimed:
                    if self.gate.active == 0:
                        log.info("queue is empty; fetch stage done")
                        break
                    time.sleep(CLAIM_IDLE_SLEEP)
                    continue

                for video in claimed:
                    if self.should_stop():
                        # Hand back whatever we claimed but never started.
                        self.repo.release_claims(self.worker, video_ids=[video.video_id])
                        continue
                    try:
                        futures.append(
                            self._submit(pool, fetcher, video, config)
                        )
                    except Stopped:
                        self.repo.release_claims(
                            self.worker, video_ids=[video.video_id]
                        )
                        break
                    processed += 1

                futures = [f for f in futures if not f.done()]
        except CircuitExhausted as exc:
            log.error("circuit breaker exhausted", detail=str(exc))
            self._exit_reason = ExitReason.CIRCUIT_OPEN
            self.request_stop()
        except QuotaExhausted as exc:
            log.warning("quota exhausted", detail=str(exc))
            self._exit_reason = ExitReason.QUOTA_EXHAUSTED
            self.request_stop()
        except KeyboardInterrupt:
            log.warning("interrupted; draining")
            self.request_stop(interrupted=True)
        finally:
            log.info("draining in-flight fetches", active=self.gate.active)
            self.gate.drain(timeout=180)
            pool.shutdown(wait=True, cancel_futures=True)
            fetcher.close()
            released = self.repo.release_claims(self.worker)
            if released:
                log.info("released leases still held", count=released)
            log.info("fetch stage finished", counts=self.counters.snapshot())

    def _submit(
        self,
        pool: ThreadPoolExecutor,
        fetcher: TranscriptFetcher,
        video: Any,
        config: FetchConfig,
    ) -> Future[None]:
        gate_cm = self.gate.slot(self.should_stop)
        gate_cm.__enter__()

        def task() -> None:
            try:
                with context(video_id=video.video_id):
                    self._fetch_one(fetcher, video, config)
            finally:
                gate_cm.__exit__(None, None, None)

        try:
            return pool.submit(task)
        except BaseException:
            gate_cm.__exit__(None, None, None)
            raise

    def _fetch_one(
        self, fetcher: TranscriptFetcher, video: Any, config: FetchConfig
    ) -> None:
        try:
            outcome = fetch_video(
                self.repo,
                fetcher,
                video,
                config,
                run_id=self.run_id,
                worker=self.worker,
                before_request=self.before_request,
            )
        except HardBlock as exc:
            # A block is a fetcher condition. record_block already requeued the
            # video without touching its status; all that is left is to tell the
            # breaker and let it decide whether to pause everything.
            self.counters.bump("blocked")
            state = self.breaker.record_block()
            log.warning(
                "hard block", detail=str(exc)[:300], breaker=state.value,
                http_status=exc.http_status,
            )
            if state is BreakerState.EXHAUSTED:
                self._exit_reason = ExitReason.CIRCUIT_OPEN
                self.request_stop()
            return
        except Stopped:
            self.repo.release_claims(self.worker, video_ids=[video.video_id])
            return
        except QuotaExhausted as exc:
            log.warning("quota exhausted mid-fetch", detail=str(exc))
            self._exit_reason = ExitReason.QUOTA_EXHAUSTED
            self.request_stop()
            return
        except Exception as exc:  # noqa: BLE001 - a worker thread must not die silently
            log.error(
                "unhandled worker error",
                error=str(exc)[:500], error_type=type(exc).__name__, exc_info=True,
            )
            self.repo.release_claims(self.worker, video_ids=[video.video_id])
            return

        if outcome.blocked:  # pragma: no cover - defensive
            self.counters.bump("blocked")
        else:
            self.breaker.record_success()
            self.counters.note_status(outcome.status)

    def _dry_run(
        self, *, channel_id: str | None, limit: int | None, skip_hydrate: bool = False
    ) -> None:
        """Report what would happen without making a single request."""
        claimed = self.repo.claim_batch(
            self.worker,
            limit=limit or 25,
            lease_seconds=5,
            channel_id=channel_id,
            include_unhydrated=skip_hydrate,
        )
        log.info(
            "dry run: would fetch",
            count=len(claimed),
            backend=self.knobs.fetcher,
            languages=list(self.knobs.languages),
            videos=[v.video_id for v in claimed[:25]],
        )
        self.repo.release_claims(self.worker)

    # -- orchestration ----------------------------------------------------- #

    def run_all(
        self,
        *,
        channel_id: str | None = None,
        incremental: bool = False,
        limit: int | None = None,
        skip_hydrate: bool = True,
    ) -> None:
        """discover, then fetch. The metadata stage is opt-in via --hydrate."""
        self.discover(channel_id=channel_id, incremental=incremental, limit=limit)
        if self.should_stop():
            return
        if skip_hydrate:
            log.info("skipping the metadata stage; fetching discovered videos directly")
        else:
            self.hydrate(channel_id=channel_id, limit=limit)
            if self.should_stop():
                return
        self.fetch(channel_id=channel_id, limit=limit, skip_hydrate=skip_hydrate)

    def exit_reason(self) -> ExitReason:
        """What to record in ``runs.exit_reason``.

        An explicitly-set reason (circuit open, quota) wins; otherwise a signal
        means ``interrupted``, a control-table stop means ``stopped``, and
        anything else completed.
        """
        if self._exit_reason is not None:
            return self._exit_reason
        if self._interrupted:
            return ExitReason.INTERRUPTED
        if self._stop.is_set():
            return ExitReason.STOPPED
        return ExitReason.COMPLETED


def exit_code_for(reason: ExitReason) -> int:
    return EXIT_CODES.get(reason, 1)
