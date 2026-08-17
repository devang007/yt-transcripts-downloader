"""Worker orchestration: exit reasons, dynamic concurrency, draining, breaker.

The exit-reason tests earn their place. ``runs.exit_reason`` is what an operator
and a cron read to decide whether a run needs attention, so a clean run reported
as ``stopped`` — or a circuit-open abort reported as ``completed`` — is a lie the
whole system is built on.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tests.fake_api import FakeFetcher, as_any, english_manual, sample_segments
from yt_tx.limiter import Stopped
from yt_tx.repo import Repo
from yt_tx.settings import Bootstrap, Knobs, MySQLConfig, WebConfig
from yt_tx.states import DesiredState, ExitReason, Status
from yt_tx.worker import DynamicGate, Pipeline, exit_code_for, worker_id

# --------------------------------------------------------------------------- #
# DynamicGate - pure
# --------------------------------------------------------------------------- #


def test_gate_admits_up_to_the_limit() -> None:
    gate = DynamicGate(2)
    first = gate.slot(lambda: False)
    second = gate.slot(lambda: False)
    first.__enter__()
    second.__enter__()
    assert gate.active == 2

    blocked = {"entered": False}

    def third() -> None:
        try:
            with gate.slot(lambda: False):
                blocked["entered"] = True
        except Stopped:
            pass

    thread = threading.Thread(target=third, daemon=True)
    thread.start()
    time.sleep(0.3)
    assert blocked["entered"] is False, "the gate let a third worker through"

    first.__exit__(None, None, None)
    thread.join(timeout=3)
    assert blocked["entered"] is True
    second.__exit__(None, None, None)


def test_gate_limit_can_change_while_threads_wait() -> None:
    """ThreadPoolExecutor cannot be resized; this is what makes the slider live."""
    gate = DynamicGate(1)
    held = gate.slot(lambda: False)
    held.__enter__()

    admitted = threading.Event()

    def waiter() -> None:
        with gate.slot(lambda: False):
            admitted.set()
            time.sleep(0.1)

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert not admitted.is_set()

    assert gate.set_limit(2) is True
    assert admitted.wait(timeout=3) is True
    held.__exit__(None, None, None)
    thread.join(timeout=3)


def test_gate_limit_is_capped() -> None:
    """A runaway concurrency setting must not fork-bomb the pool."""
    from yt_tx.worker import WORKER_CEILING

    gate = DynamicGate(1)
    gate.set_limit(10_000)
    assert gate.limit == WORKER_CEILING
    gate.set_limit(-5)
    assert gate.limit == 1
    assert gate.set_limit(gate.limit) is False


def test_gate_stop_escapes_a_full_gate() -> None:
    gate = DynamicGate(1)
    with gate.slot(lambda: False):
        with pytest.raises(Stopped):
            with gate.slot(lambda: True):
                pass  # pragma: no cover


def test_gate_drain_reports_timeout() -> None:
    gate = DynamicGate(2)
    held = gate.slot(lambda: False)
    held.__enter__()
    assert gate.drain(timeout=0.2) is False
    held.__exit__(None, None, None)
    assert gate.drain(timeout=0.2) is True


def test_worker_id_is_host_and_pid() -> None:
    import os

    identity = worker_id()
    assert str(os.getpid()) in identity
    assert len(identity) <= 64, "must fit videos.claimed_by VARCHAR(64)"


# --------------------------------------------------------------------------- #
# Exit reasons and codes
# --------------------------------------------------------------------------- #


def test_exit_codes_distinguish_the_outcomes() -> None:
    assert exit_code_for(ExitReason.COMPLETED) == 0
    assert exit_code_for(ExitReason.INTERRUPTED) == 130, "Ctrl-C convention"
    # Non-zero so an operator or CI notices work was left queued.
    assert exit_code_for(ExitReason.CIRCUIT_OPEN) == 4
    # Expected daily behaviour for a cron; tomorrow's quota picks it up.
    assert exit_code_for(ExitReason.QUOTA_EXHAUSTED) == 0
    assert exit_code_for(ExitReason.CRASHED) == 1


mysql = pytest.mark.mysql


def make_pipeline(repo: Repo, tmp_path: Path, **overrides: object) -> Pipeline:
    boot = Bootstrap(
        mysql=MySQLConfig(),
        transcript_dir=tmp_path / "tx",
        log_dir=tmp_path / "logs",
        web=WebConfig(),
        seeds={},
    )
    knobs = Knobs(
        concurrency=2,
        requests_per_second=1000.0,  # effectively unthrottled for tests
        burst=100,
        jitter=0.0,
        max_attempts=2,
        backoff_base_seconds=0.001,
        backoff_cap_seconds=0.01,
        **overrides,  # type: ignore[arg-type]
    )
    return Pipeline(repo, boot, knobs, run_id=None)


@mysql
def test_a_clean_run_reports_completed_not_stopped(
    repo: Repo, tmp_path: Path
) -> None:
    """Regression guard for a bug found by running the pipeline for real.

    ``supervised()`` used to set the same event that ``exit_reason()`` reads, so
    every successful run was recorded as ``stopped``. An operator reading
    exit_reason could not tell a clean finish from an aborted one.
    """
    pipeline = make_pipeline(repo, tmp_path)
    with pipeline.supervised():
        pass
    assert pipeline.exit_reason() is ExitReason.COMPLETED


@mysql
def test_a_signal_reports_interrupted(repo: Repo, tmp_path: Path) -> None:
    pipeline = make_pipeline(repo, tmp_path)
    with pipeline.supervised():
        pipeline.request_stop(interrupted=True)
    assert pipeline.exit_reason() is ExitReason.INTERRUPTED


@mysql
def test_a_control_table_stop_reports_stopped(repo: Repo, tmp_path: Path) -> None:
    pipeline = make_pipeline(repo, tmp_path)
    with pipeline.supervised():
        pipeline.request_stop()
    assert pipeline.exit_reason() is ExitReason.STOPPED


@mysql
def test_supervisor_picks_up_a_stop_from_the_control_table(
    repo: Repo, tmp_path: Path
) -> None:
    """The UI's Stop button works through MySQL, not through a signal."""
    pipeline = make_pipeline(repo, tmp_path)
    with pipeline.supervised():
        repo.set_control(desired_state=DesiredState.STOPPING)
        deadline = time.monotonic() + 15
        while not pipeline.should_stop() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert pipeline.should_stop() is True
    assert pipeline.exit_reason() is ExitReason.STOPPED


@mysql
def test_supervisor_applies_live_knobs(repo: Repo, tmp_path: Path) -> None:
    """A slider that does nothing until restart is worse than no slider."""
    pipeline = make_pipeline(repo, tmp_path)
    with pipeline.supervised():
        repo.set_control(concurrency=7, requests_per_second=3.5)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if pipeline.gate.limit == 7 and pipeline.bucket.rate == pytest.approx(3.5):
                break
            time.sleep(0.2)
        assert pipeline.gate.limit == 7
        assert pipeline.bucket.rate == pytest.approx(3.5)


@mysql
def test_supervisor_reaps_abandoned_leases(repo: Repo, tmp_path: Path, seeded: str) -> None:
    from sqlalchemy import text

    repo.claim_batch("ghost", limit=3, lease_seconds=600)
    with repo.begin() as conn:
        conn.execute(
            text("UPDATE videos SET lease_expires_at = UTC_TIMESTAMP(6) - INTERVAL 1 SECOND")
        )
    pipeline = make_pipeline(repo, tmp_path)
    # fetch() reaps at startup before claiming anything.
    pipeline.request_stop()
    pipeline.fetch()
    assert repo.count_stale_leases() == 0


@mysql
def test_status_snapshot_carries_breaker_state_for_the_ui(
    repo: Repo, tmp_path: Path
) -> None:
    pipeline = make_pipeline(repo, tmp_path)
    snapshot = pipeline.status_snapshot()
    assert snapshot["breaker"]["state"] == "closed"
    assert snapshot["concurrency"] == 2
    assert "transcript_ok" in snapshot


@mysql
def test_pause_holds_the_fetch_loop(repo: Repo, tmp_path: Path, seeded: str) -> None:
    pipeline = make_pipeline(repo, tmp_path)
    repo.set_control(desired_state=DesiredState.PAUSED)

    finished = threading.Event()

    def run() -> None:
        with pipeline.supervised():
            pipeline.fetch()
        finished.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    # Wait for the supervisor to observe the pause, then confirm nothing moved.
    time.sleep(4)
    assert finished.is_set() is False, "a paused worker should not run to completion"
    assert repo.status_counts().get(Status.TRANSCRIPT_OK.value, 0) == 0

    repo.set_control(desired_state=DesiredState.STOPPING)
    thread.join(timeout=30)
    assert finished.is_set() is True


@mysql
def test_stop_releases_every_lease_it_still_holds(
    repo: Repo, tmp_path: Path, seeded: str
) -> None:
    """Draining must not leave rows claimed by a process that has exited."""
    pipeline = make_pipeline(repo, tmp_path)
    pipeline.request_stop()
    pipeline.fetch()

    rows, _ = repo.list_videos(per_page=100)
    assert all(row.claimed_by is None for row in rows)
    # Everything is still queued, with no attempts consumed.
    assert all(row.attempts == 0 for row in rows)
    assert repo.status_counts() == {Status.METADATA_OK.value: 3}


@mysql
def test_fetch_drains_the_queue_and_counts_outcomes(
    repo: Repo, tmp_path: Path, seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full fetch pass, end to end, with a fixture-driven fetcher."""
    import yt_tx.worker as worker_module

    fetcher = FakeFetcher(available=english_manual(), segments=sample_segments())
    monkeypatch.setattr(
        worker_module, "make_fetcher", lambda *a, **k: as_any(fetcher)
    )

    pipeline = make_pipeline(repo, tmp_path)
    with pipeline.supervised():
        pipeline.fetch()

    assert pipeline.exit_reason() is ExitReason.COMPLETED
    assert repo.status_counts() == {Status.TRANSCRIPT_OK.value: 3}
    counts = pipeline.counters.snapshot()
    assert counts["transcript_ok"] == 3
    assert counts["blocked"] == 0
    assert len(fetcher.download_calls) == 3


@mysql
def test_a_block_trips_the_breaker_without_touching_video_status(
    repo: Repo, tmp_path: Path, seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end version of the invariant the whole design rests on."""
    import yt_tx.worker as worker_module
    from youtube_transcript_api import IpBlocked

    import yt_tx.limiter as limiter_module

    fetcher = FakeFetcher(list_error=IpBlocked("vid0000000000001"))
    monkeypatch.setattr(
        worker_module, "make_fetcher", lambda *a, **k: as_any(fetcher)
    )
    # Cooldowns of zero so the breaker escalates to exhaustion promptly.
    pipeline = make_pipeline(
        repo, tmp_path,
        consecutive_blocks_to_open=1,
        cooldown_schedule_seconds=[0],
        max_reopens=1,
    )
    monkeypatch.setattr(limiter_module, "DEFAULT_POLL_INTERVAL", 0.01)

    with pipeline.supervised():
        pipeline.fetch()

    assert pipeline.exit_reason() is ExitReason.CIRCUIT_OPEN
    assert exit_code_for(pipeline.exit_reason()) != 0, "work remains; say so"

    # Every video is exactly as it was: queued, unmarked, unattempted.
    rows, _ = repo.list_videos(per_page=100)
    assert all(row.status is Status.METADATA_OK for row in rows), (
        "an IP block poisoned video rows"
    )
    assert all(row.attempts == 0 for row in rows)
    assert all(row.claimed_by is None for row in rows)
    assert all(row.needs_audio is False for row in rows)
    assert pipeline.counters.snapshot()["blocked"] >= 1


@mysql
def test_dry_run_makes_no_requests_and_leaves_nothing_claimed(
    repo: Repo, tmp_path: Path, seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yt_tx.worker as worker_module

    fetcher = FakeFetcher(available=english_manual(), segments=sample_segments())
    monkeypatch.setattr(
        worker_module, "make_fetcher", lambda *a, **k: as_any(fetcher)
    )
    pipeline = make_pipeline(repo, tmp_path)
    pipeline.fetch(dry_run=True)

    assert fetcher.list_calls == []
    assert fetcher.download_calls == []
    rows, _ = repo.list_videos(per_page=100)
    assert all(row.claimed_by is None for row in rows)
    assert repo.status_counts() == {Status.METADATA_OK.value: 3}


@mysql
def test_fetch_promotes_due_rechecks_before_claiming(
    repo: Repo, tmp_path: Path, channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recheck that never gets promoted is a video that is never revisited."""
    import yt_tx.worker as worker_module
    from sqlalchemy import text

    from yt_tx.repo import VideoUpsert

    repo.upsert_videos([VideoUpsert(video_id="duerecheck01", channel_id=channel)])
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET status='no_transcript', needs_audio=1, "
                "recheck_after = UTC_TIMESTAMP(6) - INTERVAL 1 MINUTE"
            )
        )

    fetcher = FakeFetcher(available=english_manual(), segments=sample_segments())
    monkeypatch.setattr(
        worker_module, "make_fetcher", lambda *a, **k: as_any(fetcher)
    )
    pipeline = make_pipeline(repo, tmp_path)
    with pipeline.supervised():
        pipeline.fetch()

    row = repo.get_video("duerecheck01")
    assert row is not None
    assert row.status is Status.TRANSCRIPT_OK, "captions appeared on the recheck"
    assert row.recheck_count == 1
    assert row.needs_audio is False


@mysql
def test_limit_bounds_the_fetch_pass(
    repo: Repo, tmp_path: Path, seeded: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yt_tx.worker as worker_module

    fetcher = FakeFetcher(available=english_manual(), segments=sample_segments())
    monkeypatch.setattr(
        worker_module, "make_fetcher", lambda *a, **k: as_any(fetcher)
    )
    pipeline = make_pipeline(repo, tmp_path)
    with pipeline.supervised():
        pipeline.fetch(limit=1)
    assert len(fetcher.download_calls) == 1
    assert repo.status_counts()[Status.TRANSCRIPT_OK.value] == 1
