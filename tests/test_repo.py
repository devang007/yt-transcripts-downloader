"""Repo layer against a real MySQL 8 server.

Covers the two milestones whose failure modes are silent and expensive:

* an upsert that quietly resets ``status`` (re-downloads everything, forever);
* claiming that lets two workers take the same row, or loses rows when a worker
  dies (work either duplicated or invisible).

Both are only testable against real MySQL. ``SKIP LOCKED`` and ``ON DUPLICATE
KEY UPDATE ... AS new`` do not exist in SQLite, and ENUM/JSON coercion differs.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Engine, text

from tests.conftest import status_of
from yt_tx import db as ytdb
from yt_tx.repo import (
    MetadataUpdate,
    Repo,
    TranscriptWrite,
    VideoUpsert,
    quota_day,
)
from yt_tx.states import (
    DesiredState,
    ExitReason,
    IllegalTransition,
    Status,
    TranscriptKind,
)

pytestmark = pytest.mark.mysql


# --------------------------------------------------------------------------- #
# Server and connection hygiene
# --------------------------------------------------------------------------- #


def test_server_meets_requirements(engine: Engine) -> None:
    with engine.connect() as conn:
        diag = ytdb.check_server(conn)
    assert diag["skip_locked"] is True
    assert str(diag["charset_connection"]).startswith("utf8mb4")
    assert diag["time_zone"] in {"+00:00", "UTC"}
    assert "STRICT_TRANS_TABLES" in diag["sql_mode"]


def test_utf8mb4_survives_round_trip(repo: Repo, channel: str) -> None:
    """Non-BMP characters must not raise *Incorrect string value*.

    Real titles are full of emoji. On a utf8mb3 connection this raises partway
    through the first big channel, after hours of work.
    """
    nasty = "Test 🎬 emoji, 日本語, ñ, and a zero-width​ space 𝄞"
    repo.upsert_videos(
        [VideoUpsert(video_id="utf8test0001", channel_id=channel, title=nasty)]
    )
    row = repo.get_video("utf8test0001")
    assert row is not None
    assert row.title == nasty


def test_timestamps_are_utc(repo: Repo) -> None:
    server = repo.server_now()
    local = datetime.now(timezone.utc)
    assert server.tzinfo is not None
    assert abs((server - local).total_seconds()) < 120, (
        "server clock is not UTC, or session time_zone was not applied"
    )


def test_strict_mode_rejects_overlong_values(repo: Repo, channel: str) -> None:
    """Silent truncation is worse than an error, so STRICT_TRANS_TABLES is on."""
    from sqlalchemy.exc import DataError

    with pytest.raises(DataError):
        repo.upsert_videos(
            [VideoUpsert(video_id="x" * 40, channel_id=channel, title="too long id")]
        )


# --------------------------------------------------------------------------- #
# Upserts must never clobber status
# --------------------------------------------------------------------------- #


def test_upsert_never_resets_status(repo: Repo, channel: str) -> None:
    """The single most consequential property of the repo layer.

    Enumeration re-inserts every video it sees on every run. If that reset
    ``status``, a nightly cron would re-download the entire back catalogue every
    night and the project would never converge.
    """
    repo.upsert_videos(
        [VideoUpsert(video_id="statusguard1", channel_id=channel, title="First")]
    )
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET status='transcript_ok', attempts=3, needs_audio=0, "
                "recheck_count=2 WHERE video_id='statusguard1'"
            )
        )

    # Re-enumerate, exactly as a second run would.
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id="statusguard1",
                channel_id=channel,
                title="Renamed by the uploader",
            )
        ]
    )

    row = repo.get_video("statusguard1")
    assert row is not None
    assert row.status is Status.TRANSCRIPT_OK, "upsert clobbered status"
    assert row.attempts == 3, "upsert clobbered attempts"
    assert row.recheck_count == 2
    assert row.title == "Renamed by the uploader", "metadata should still refresh"


def test_upsert_sql_does_not_mention_status_in_the_update_clause() -> None:
    """Structural guard: catches the mistake at review time, not at 3am."""
    from yt_tx.repo import _UPSERT_VIDEOS

    update_clause = _UPSERT_VIDEOS.split("ON DUPLICATE KEY UPDATE", 1)[1]
    for forbidden in (
        "status", "attempts", "needs_audio", "recheck_count",
        "next_attempt_at", "recheck_after", "claimed_by", "lease_expires_at",
    ):
        assert forbidden not in update_clause, (
            f"{forbidden} must not be updated by enumeration"
        )


def test_upsert_does_not_null_out_existing_metadata(repo: Repo, channel: str) -> None:
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id="coalesce001",
                channel_id=channel,
                title="Has a title",
                description="Has a description",
            )
        ]
    )
    # A thinner source (e.g. the RSS feed) has no description.
    repo.upsert_videos([VideoUpsert(video_id="coalesce001", channel_id=channel)])
    row = repo.get_video("coalesce001")
    assert row is not None
    assert row.title == "Has a title"
    assert row.description == "Has a description"


def test_upsert_is_idempotent(repo: Repo, channel: str) -> None:
    batch = [
        VideoUpsert(video_id=f"idem{i:09d}", channel_id=channel, title=f"V{i}")
        for i in range(20)
    ]
    repo.upsert_videos(batch)
    repo.upsert_videos(batch)
    repo.upsert_videos(batch)
    assert repo.status_counts() == {Status.DISCOVERED.value: 20}


def test_count_new_videos(repo: Repo, channel: str) -> None:
    repo.upsert_videos([VideoUpsert(video_id="known0000001", channel_id=channel)])
    assert repo.count_new_videos(["known0000001", "fresh0000001"]) == 1
    assert repo.count_new_videos([]) == 0


# --------------------------------------------------------------------------- #
# Claiming and leases
# --------------------------------------------------------------------------- #


def test_claim_leases_rows(repo: Repo, seeded: str) -> None:
    claimed = repo.claim_batch("worker-a", limit=2, lease_seconds=600)
    assert len(claimed) == 2
    for row in claimed:
        fresh = repo.get_video(row.video_id)
        assert fresh is not None
        assert fresh.claimed_by == "worker-a"
        assert fresh.lease_expires_at is not None
        # Status is unchanged by claiming - a claim is a lease, not a state change.
        assert fresh.status is Status.METADATA_OK


def test_claim_orders_oldest_first(repo: Repo, seeded: str) -> None:
    claimed = repo.claim_batch("worker-a", limit=3)
    published = [c.published_at for c in claimed]
    assert published == sorted(published)  # type: ignore[type-var]


def test_second_claim_skips_leased_rows(repo: Repo, seeded: str) -> None:
    first = {v.video_id for v in repo.claim_batch("worker-a", limit=2)}
    second = {v.video_id for v in repo.claim_batch("worker-b", limit=5)}
    assert first & second == set(), "two workers claimed the same video"
    assert len(first) == 2
    assert len(second) == 1


def test_concurrent_workers_claim_disjoint_sets(engine: Engine, channel: str) -> None:
    """The property ``FOR UPDATE SKIP LOCKED`` exists to provide.

    Without SKIP LOCKED one worker blocks on the other's row locks and the two
    serialise; worse, a naive select-then-update lets both claim the same rows.
    """
    repo = Repo(engine)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id=f"conc{i:09d}",
                channel_id=channel,
                published_at=now - timedelta(minutes=i),
            )
            for i in range(120)
        ]
    )
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status='metadata_ok'"))

    results: dict[str, list[str]] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker(name: str) -> None:
        own: list[str] = []
        try:
            barrier.wait()
            # An empty batch under SKIP LOCKED does not mean the queue is empty -
            # it can just mean every remaining row was locked by a peer at that
            # instant. Concluding "done" on the first empty result is how a
            # worker abandons work that is still outstanding, so require two
            # consecutive empties. The production loop in worker.py does the same
            # thing, gated on its in-flight count.
            empty_streak = 0
            while empty_streak < 2:
                batch = repo.claim_batch(name, limit=5, lease_seconds=600)
                if not batch:
                    empty_streak += 1
                    continue
                empty_streak = 0
                own.extend(v.video_id for v in batch)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            results[name] = own

    threads = [
        threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    all_claimed = [vid for ids in results.values() for vid in ids]
    assert len(all_claimed) == len(set(all_claimed)), "a video was claimed twice"
    assert len(all_claimed) == 120, "some videos were never claimed"


def test_killed_worker_rows_return_to_the_queue(repo: Repo, seeded: str) -> None:
    """A SIGKILLed worker must not make its rows invisible forever."""
    claimed = repo.claim_batch("doomed-worker", limit=3, lease_seconds=600)
    assert len(claimed) == 3
    assert repo.claim_batch("worker-b", limit=5) == []

    # Simulate the worker vanishing: its lease matures with nothing to renew it.
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET lease_expires_at = UTC_TIMESTAMP(6) "
                "- INTERVAL 1 SECOND WHERE claimed_by = 'doomed-worker'"
            )
        )
    assert repo.count_stale_leases() == 3
    assert repo.reap_expired_leases() == 3
    assert repo.count_stale_leases() == 0

    recovered = repo.claim_batch("worker-b", limit=5)
    assert len(recovered) == 3
    for row in recovered:
        assert row.status is Status.RETRY
        assert row.claimed_by == "worker-b"


def test_expired_lease_is_claimable_even_before_the_reaper_runs(
    repo: Repo, seeded: str
) -> None:
    """The claim query filters on the lease too, so a slow reaper is not fatal."""
    repo.claim_batch("worker-a", limit=3, lease_seconds=600)
    with repo.begin() as conn:
        conn.execute(
            text("UPDATE videos SET lease_expires_at = UTC_TIMESTAMP(6) - INTERVAL 1 SECOND")
        )
    assert len(repo.claim_batch("worker-b", limit=3)) == 3


def test_disabled_channel_is_inert_for_the_whole_pipeline(
    repo: Repo, seeded: str
) -> None:
    """Disabling a channel must stop fetching too, not only discovery.

    ``is_enabled`` used to be read in exactly one place - enumeration - so
    disabling a channel mid-harvest looked like a stop button and did nothing
    to the two stages that spend the request budget.
    """
    assert repo.set_channel_enabled(seeded, enabled=False) is True
    assert repo.claim_batch("worker-a", limit=5) == []
    assert repo.videos_needing_metadata(limit=5) == []

    assert repo.set_channel_enabled(seeded, enabled=True) is True
    assert len(repo.claim_batch("worker-a", limit=5)) == 3


def test_naming_a_disabled_channel_explicitly_still_works(
    repo: Repo, seeded: str
) -> None:
    """--channel is a deliberate act and outranks the switch."""
    repo.set_channel_enabled(seeded, enabled=False)
    assert len(repo.claim_batch("worker-a", limit=5, channel_id=seeded)) == 3


def test_skip_hydrate_claims_unhydrated_videos(repo: Repo, channel: str) -> None:
    """--skip-hydrate: captions need no metadata, so `discovered` is claimable."""
    repo.upsert_videos(
        [VideoUpsert(video_id="unhydrated01", channel_id=channel, title="No metadata")]
    )
    assert status_of(repo, "unhydrated01") is Status.DISCOVERED

    assert repo.claim_batch("worker-a", limit=5) == [], "claimed without the flag"

    claimed = repo.claim_batch("worker-a", limit=5, include_unhydrated=True)
    assert [c.video_id for c in claimed] == ["unhydrated01"]


def test_extend_lease_only_for_the_holder(repo: Repo, seeded: str) -> None:
    claimed = repo.claim_batch("worker-a", limit=1, lease_seconds=60)
    ids = [c.video_id for c in claimed]
    assert repo.extend_lease(ids, "worker-b", 600) == 0, "stole another's lease"
    assert repo.extend_lease(ids, "worker-a", 600) == 1


def test_release_claims_leaves_status_alone(repo: Repo, seeded: str) -> None:
    """Draining on shutdown must requeue work without recording a failure."""
    claimed = repo.claim_batch("worker-a", limit=2)
    assert repo.release_claims("worker-a") == 2
    for row in claimed:
        fresh = repo.get_video(row.video_id)
        assert fresh is not None
        assert fresh.claimed_by is None
        assert fresh.status is Status.METADATA_OK
        assert fresh.attempts == 0


def test_claim_respects_next_attempt_at(repo: Repo, seeded: str) -> None:
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET status='retry', "
                "next_attempt_at = UTC_TIMESTAMP(6) + INTERVAL 1 HOUR"
            )
        )
    assert repo.claim_batch("worker-a", limit=5) == []
    with repo.begin() as conn:
        conn.execute(
            text("UPDATE videos SET next_attempt_at = UTC_TIMESTAMP(6) - INTERVAL 1 SECOND")
        )
    assert len(repo.claim_batch("worker-a", limit=5)) == 3


def test_claim_filters_by_channel(repo: Repo, seeded: str) -> None:
    repo.upsert_channel(channel_id="UCother", input_ref="@other", handle="@other")
    repo.upsert_videos([VideoUpsert(video_id="othervid0001", channel_id="UCother")])
    with repo.begin() as conn:
        conn.execute(
            text("UPDATE videos SET status='metadata_ok' WHERE video_id='othervid0001'")
        )
    claimed = repo.claim_batch("worker-a", limit=10, channel_id="UCother")
    assert [c.video_id for c in claimed] == ["othervid0001"]


def test_terminal_statuses_are_not_claimed(repo: Repo, seeded: str) -> None:
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status='transcript_ok'"))
    assert repo.claim_batch("worker-a", limit=10) == []


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


def test_transcript_success_is_one_transaction(repo: Repo, seeded: str) -> None:
    claimed = repo.claim_batch("worker-a", limit=1)[0]
    repo.record_transcript_success(
        claimed.video_id,
        [
            TranscriptWrite(
                video_id=claimed.video_id,
                language_code="en",
                kind=TranscriptKind.MANUAL,
                is_preferred=True,
                segment_count=312,
                char_count=15000,
                word_count=2800,
                covered_seconds=610.5,
                raw_path=f"data/transcripts/x/{claimed.video_id}.en.manual.json.gz",
                raw_sha256="a" * 64,
                plaintext="hello world",
                source="youtube-transcript-api",
            )
        ],
        available=[{"language_code": "en", "kind": "manual"}],
        run_id=None,
        worker="worker-a",
    )
    row = repo.get_video(claimed.video_id)
    assert row is not None
    assert row.status is Status.TRANSCRIPT_OK
    assert row.needs_audio is False
    assert row.claimed_by is None
    assert row.attempts == 1
    assert row.available_transcripts == [{"language_code": "en", "kind": "manual"}]

    transcripts = repo.list_transcripts(claimed.video_id)
    assert len(transcripts) == 1
    assert transcripts[0].covered_seconds == pytest.approx(610.5)
    assert repo.recent_attempts(claimed.video_id)[0]["outcome"] == "ok"


def test_refetching_replaces_the_variant_not_duplicates_it(
    repo: Repo, seeded: str
) -> None:
    """``uq_tx_variant`` plus upsert semantics: a re-fetch is idempotent."""
    video_id = repo.claim_batch("worker-a", limit=1)[0].video_id

    def write(chars: int) -> None:
        repo.record_transcript_success(
            video_id,
            [
                TranscriptWrite(
                    video_id=video_id,
                    language_code="en",
                    kind=TranscriptKind.MANUAL,
                    is_preferred=True,
                    segment_count=10,
                    char_count=chars,
                    word_count=5,
                    covered_seconds=1.0,
                    raw_path="p.json.gz",
                    raw_sha256="b" * 64,
                    plaintext="x",
                    source="yt-dlp",
                )
            ],
            available=None,
            run_id=None,
            worker="worker-a",
        )

    write(100)
    repo.reopen_video(video_id)
    write(200)
    rows = repo.list_transcripts(video_id)
    assert len(rows) == 1
    assert rows[0].char_count == 200


def test_no_transcript_schedules_a_recheck_for_a_fresh_video(
    repo: Repo, channel: str
) -> None:
    """Auto-captions often appear hours after upload, so young videos come back."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id="freshvid0001",
                channel_id=channel,
                published_at=now - timedelta(hours=2),
            )
        ]
    )
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status='metadata_ok'"))

    repo.record_terminal(
        "freshvid0001",
        status=Status.NO_TRANSCRIPT,
        reason="captions are disabled",
        needs_audio=True,
        schedule_recheck=True,
    )
    row = repo.get_video("freshvid0001")
    assert row is not None
    assert row.status is Status.NO_TRANSCRIPT
    assert row.needs_audio is True
    assert row.recheck_after is not None
    delta = row.recheck_after - repo.server_now()
    assert timedelta(hours=5) < delta < timedelta(hours=7), "expected ~+6h"


def test_recheck_interval_widens_with_age(repo: Repo, channel: str) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cases = {
        "agevid000001": (timedelta(days=2), timedelta(hours=6)),
        "agevid000002": (timedelta(days=20), timedelta(days=3)),
        "agevid000003": (timedelta(days=100), timedelta(days=30)),
        "agevid000004": (timedelta(days=400), None),  # too old: never
    }
    repo.upsert_videos(
        [
            VideoUpsert(video_id=vid, channel_id=channel, published_at=now - age)
            for vid, (age, _) in cases.items()
        ]
    )
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status='metadata_ok'"))

    for vid, (_, expected) in cases.items():
        repo.record_terminal(
            vid,
            status=Status.NO_TRANSCRIPT,
            reason="none",
            needs_audio=True,
            schedule_recheck=True,
        )
        row = repo.get_video(vid)
        assert row is not None
        if expected is None:
            assert row.recheck_after is None, f"{vid} should never be rechecked"
        else:
            assert row.recheck_after is not None, vid
            delta = row.recheck_after - repo.server_now()
            assert expected * 0.8 < delta < expected * 1.2, f"{vid}: {delta}"


def test_promote_due_rechecks_requeues_and_counts(repo: Repo, channel: str) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id="recheck00001",
                channel_id=channel,
                published_at=now - timedelta(days=1),
            )
        ]
    )
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET status='no_transcript', needs_audio=1, "
                "recheck_after = UTC_TIMESTAMP(6) - INTERVAL 1 MINUTE"
            )
        )
    assert repo.promote_due_rechecks() == 1
    row = repo.get_video("recheck00001")
    assert row is not None
    assert row.status is Status.METADATA_OK
    assert row.recheck_count == 1
    assert row.recheck_after is None


def test_recheck_cap_is_enforced(repo: Repo, channel: str) -> None:
    """After four rechecks a video is genuinely audio-only; stop spending requests."""
    from yt_tx.repo import MAX_RECHECKS

    repo.upsert_videos([VideoUpsert(video_id="capped000001", channel_id=channel)])
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET status='no_transcript', "
                "recheck_count = :n, "
                "recheck_after = UTC_TIMESTAMP(6) - INTERVAL 1 MINUTE"
            ),
            {"n": MAX_RECHECKS},
        )
    assert repo.promote_due_rechecks() == 0
    assert status_of(repo, "capped000001") is Status.NO_TRANSCRIPT


def test_future_recheck_is_not_promoted(repo: Repo, channel: str) -> None:
    repo.upsert_videos([VideoUpsert(video_id="future000001", channel_id=channel)])
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET status='no_transcript', "
                "recheck_after = UTC_TIMESTAMP(6) + INTERVAL 1 DAY"
            )
        )
    assert repo.promote_due_rechecks() == 0


def test_retry_then_exhaustion_becomes_failed(repo: Repo, seeded: str) -> None:
    video_id = repo.claim_batch("worker-a", limit=1)[0].video_id
    for expected in (Status.RETRY, Status.RETRY, Status.FAILED):
        result = repo.record_retry(
            video_id,
            reason="HTTP 503",
            delay_seconds=0.0,
            max_attempts=3,
            error_type="YouTubeRequestFailed",
            error_message="503 Server Error",
            http_status=503,
        )
        assert result is expected
        if expected is Status.RETRY:
            repo.claim_batch("worker-a", limit=1)
    row = repo.get_video(video_id)
    assert row is not None
    assert row.status is Status.FAILED
    assert row.attempts == 3


def test_retry_sets_a_future_attempt_time(repo: Repo, seeded: str) -> None:
    video_id = repo.claim_batch("worker-a", limit=1)[0].video_id
    repo.record_retry(
        video_id, reason="timeout", delay_seconds=120.0, max_attempts=4
    )
    row = repo.get_video(video_id)
    assert row is not None
    assert row.next_attempt_at is not None
    delta = row.next_attempt_at - repo.server_now()
    assert timedelta(seconds=100) < delta < timedelta(seconds=140)

    # This video specifically must be invisible until its backoff elapses. The
    # other seeded videos are still fair game, which is why this checks
    # membership rather than an empty queue.
    claimed = {v.video_id for v in repo.claim_batch("worker-b", limit=5)}
    assert video_id not in claimed
    assert len(claimed) == 2


def test_block_requeues_without_touching_status(repo: Repo, seeded: str) -> None:
    """The invariant the whole design turns on.

    A block is a fetcher condition. The video keeps its status and its attempt
    count, and becomes immediately claimable again once the breaker closes.
    """
    video_id = repo.claim_batch("worker-a", limit=1)[0].video_id
    before = repo.get_video(video_id)
    assert before is not None

    repo.record_block(
        video_id,
        worker="worker-a",
        error_type="IpBlocked",
        error_message="YouTube is blocking requests from your IP",
        http_status=429,
    )

    after = repo.get_video(video_id)
    assert after is not None
    assert after.status is before.status is Status.METADATA_OK
    assert after.attempts == before.attempts == 0
    assert after.claimed_by is None
    assert after.needs_audio is False
    assert repo.recent_attempts(video_id)[0]["outcome"] == "blocked"
    assert len(repo.claim_batch("worker-b", limit=1)) == 1


def test_available_transcripts_recorded_even_with_no_download(
    repo: Repo, seeded: str
) -> None:
    """This column is what tells "captions off" apart from "wrong language"."""
    video_id = repo.claim_batch("worker-a", limit=1)[0].video_id
    inventory = [
        {"language_code": "ja", "language": "Japanese", "kind": "asr",
         "is_translatable": True},
    ]
    repo.set_available_transcripts(video_id, inventory)
    repo.record_terminal(
        video_id,
        status=Status.LANG_MISSING,
        reason="no transcript in configured languages (wanted en, hi)",
        needs_audio=False,
    )
    row = repo.get_video(video_id)
    assert row is not None
    assert row.status is Status.LANG_MISSING
    assert row.available_transcripts == inventory
    assert row.needs_audio is False, "captions exist; this is not an ASR job"


def test_illegal_transition_is_rejected(repo: Repo, seeded: str) -> None:
    """A silent illegal transition is how a video becomes permanently invisible."""
    video_id = "vid0000000000001"
    with repo.begin() as conn:
        conn.execute(
            text("UPDATE videos SET status='unavailable' WHERE video_id = :v"),
            {"v": video_id},
        )
    with pytest.raises(IllegalTransition):
        repo.record_terminal(
            video_id, status=Status.LANG_MISSING, reason="nope", needs_audio=False
        )
    assert status_of(repo, video_id) is Status.UNAVAILABLE


def test_reopen_failed_videos(repo: Repo, seeded: str) -> None:
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status='failed', attempts=4"))
    assert repo.reopen(statuses=[Status.FAILED]) == 3
    row = repo.get_video("vid0000000000001")
    assert row is not None
    assert row.status is Status.METADATA_OK
    assert row.attempts == 0


def test_reopen_video_resets_one(repo: Repo, seeded: str) -> None:
    with repo.begin() as conn:
        conn.execute(
            text("UPDATE videos SET status='transcript_ok' WHERE video_id='vid0000000000001'")
        )
    assert repo.reopen_video("vid0000000000001") is True
    assert status_of(repo, "vid0000000000001") is Status.METADATA_OK
    assert repo.reopen_video("nonexistent1") is False


def test_unskip_matured_upcoming(repo: Repo, channel: str) -> None:
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id="premiere0001",
                channel_id=channel,
                published_at=datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(hours=1),
            )
        ]
    )
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET status='skipped', live_broadcast_content='upcoming'"
            )
        )
    assert repo.unskip_matured_upcoming() == 1
    assert status_of(repo, "premiere0001") is Status.DISCOVERED


# --------------------------------------------------------------------------- #
# Hydration
# --------------------------------------------------------------------------- #


def test_apply_metadata(repo: Repo, channel: str) -> None:
    repo.upsert_videos([VideoUpsert(video_id="hydrate00001", channel_id=channel)])
    assert repo.apply_metadata(
        [
            MetadataUpdate(
                video_id="hydrate00001",
                title="Hydrated",
                duration_seconds=3725,
                view_count=1234567,
                tags=["python", "mysql"],
                live_broadcast_content="none",
                is_short=False,
                was_livestream=True,
                status=Status.METADATA_OK,
            )
        ]
    ) == 1
    row = repo.get_video("hydrate00001")
    assert row is not None
    assert row.status is Status.METADATA_OK
    assert row.duration_seconds == 3725
    assert row.tags == ["python", "mysql"]
    assert row.was_livestream is True
    assert row.is_short is False
    assert row.metadata_fetched_at is not None


def test_apply_metadata_can_skip(repo: Repo, channel: str) -> None:
    repo.upsert_videos([VideoUpsert(video_id="skipme000001", channel_id=channel)])
    repo.apply_metadata(
        [
            MetadataUpdate(
                video_id="skipme000001",
                status=Status.SKIPPED,
                status_reason="live now",
                live_broadcast_content="live",
            )
        ]
    )
    assert status_of(repo, "skipme000001") is Status.SKIPPED


def test_apply_metadata_does_not_touch_completed_videos(
    repo: Repo, channel: str
) -> None:
    repo.upsert_videos([VideoUpsert(video_id="donealready1", channel_id=channel)])
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status='transcript_ok'"))
    assert repo.apply_metadata([MetadataUpdate(video_id="donealready1", title="X")]) == 0
    assert status_of(repo, "donealready1") is Status.TRANSCRIPT_OK


def test_mark_videos_unavailable(repo: Repo, seeded: str) -> None:
    assert repo.mark_videos_unavailable(["vid0000000000001"], "deleted or private") == 1
    assert status_of(repo, "vid0000000000001") is Status.UNAVAILABLE


def test_videos_needing_metadata(repo: Repo, seeded: str) -> None:
    assert repo.videos_needing_metadata() == []
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status='discovered'"))
    assert len(repo.videos_needing_metadata(limit=2)) == 2


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #


def test_channel_resolution_is_cached(repo: Repo, channel: str) -> None:
    assert repo.find_channel_by_ref("@testchannel") is not None
    assert repo.find_channel_by_ref(channel) is not None
    assert repo.find_channel_by_ref("@nope") is None


def test_enumeration_cursor_round_trip(repo: Repo, channel: str) -> None:
    repo.save_enumeration_cursor(channel, cursor="CAUQAA", complete=False)
    row = repo.get_channel(channel)
    assert row is not None
    assert row.enumeration_cursor == "CAUQAA"
    assert row.enumeration_complete is False
    assert row.last_enumerated_at is not None

    repo.save_enumeration_cursor(channel, cursor=None, complete=True)
    row = repo.get_channel(channel)
    assert row is not None
    assert row.enumeration_cursor is None
    assert row.enumeration_complete is True


def test_newest_published_at_only_moves_forward(repo: Repo, channel: str) -> None:
    """Incremental runs compare against this; regressing it re-enumerates."""
    newer = datetime(2024, 6, 1, 12, 0, 0)
    older = datetime(2020, 1, 1, 12, 0, 0)
    repo.save_enumeration_cursor(channel, cursor=None, newest_published_at=newer)
    repo.save_enumeration_cursor(channel, cursor=None, newest_published_at=older)
    row = repo.get_channel(channel)
    assert row is not None
    assert row.newest_published_at is not None
    assert row.newest_published_at.replace(tzinfo=None) == newer


def test_delete_channel_cascades(repo: Repo, seeded: str) -> None:
    assert repo.delete_channel(seeded) is True
    assert repo.status_counts() == {}


def test_set_channel_enabled(repo: Repo, channel: str) -> None:
    assert repo.set_channel_enabled(channel, False) is True
    assert repo.list_channels(enabled_only=True) == []
    assert len(repo.list_channels()) == 1


def test_channel_stats_coverage(repo: Repo, seeded: str) -> None:
    with repo.begin() as conn:
        conn.execute(
            text("UPDATE videos SET status='transcript_ok' WHERE video_id='vid0000000000001'")
        )
        conn.execute(
            text("UPDATE videos SET status='no_transcript' WHERE video_id='vid0000000000002'")
        )
    stats = repo.channel_stats()
    assert len(stats) == 1
    assert stats[0].total == 3
    assert stats[0].done == 1
    assert stats[0].no_transcript == 1
    assert stats[0].coverage_pct == pytest.approx(33.3)


# --------------------------------------------------------------------------- #
# Settings, control, runs, quota
# --------------------------------------------------------------------------- #


def test_settings_round_trip(repo: Repo) -> None:
    repo.put_settings({"languages": ["en", "hi"], "concurrency": 5, "proxy": None})
    values = repo.get_settings()
    assert values["languages"] == ["en", "hi"]
    assert values["concurrency"] == 5
    assert values["proxy"] is None


def test_seed_settings_does_not_overwrite(repo: Repo) -> None:
    repo.put_settings({"concurrency": 9})
    written = repo.seed_settings({"concurrency": 3, "burst": 4})
    assert written == ["burst"]
    assert repo.get_settings()["concurrency"] == 9


def test_control_defaults_and_updates(repo: Repo) -> None:
    control = repo.get_control()
    assert control.desired_state is DesiredState.RUNNING
    assert control.concurrency == 3

    repo.set_control(desired_state=DesiredState.PAUSED, requests_per_second=1.25)
    control = repo.get_control()
    assert control.is_paused is True
    assert control.requests_per_second == pytest.approx(1.25)

    repo.set_control(desired_state=DesiredState.STOPPING)
    assert repo.get_control().should_stop is True


def test_run_lifecycle(repo: Repo) -> None:
    run_id = repo.create_run("fetch", args={"limit": 10}, log_path="logs/run-1.jsonl")
    run = repo.get_run(run_id)
    assert run is not None
    assert run.is_active is True
    assert run.args == {"limit": 10}
    assert run.host

    repo.heartbeat(run_id, {"ok": 4})
    repo.finish_run(run_id, exit_reason=ExitReason.COMPLETED, counts={"ok": 7})
    run = repo.get_run(run_id)
    assert run is not None
    assert run.is_active is False
    assert run.exit_reason == "completed"
    assert run.counts == {"ok": 7}
    assert repo.active_run() is None


def test_finish_run_is_idempotent(repo: Repo) -> None:
    run_id = repo.create_run("fetch")
    repo.finish_run(run_id, exit_reason=ExitReason.COMPLETED)
    repo.finish_run(run_id, exit_reason=ExitReason.CRASHED)
    run = repo.get_run(run_id)
    assert run is not None
    assert run.exit_reason == "completed", "a finished run must not be reclassified"


def test_orphan_runs_are_marked_crashed(repo: Repo) -> None:
    """Otherwise the UI shows a phantom RUNNING forever after a reboot."""
    alive_id = repo.create_run("fetch", pid=99999999)
    dead_id = repo.create_run("fetch", pid=99999998)
    crashed = repo.mark_orphan_runs_crashed(alive={99999999})
    assert crashed == [dead_id]
    dead = repo.get_run(dead_id)
    alive = repo.get_run(alive_id)
    assert dead is not None and dead.exit_reason == "crashed"
    assert alive is not None and alive.is_active is True


def test_quota_ledger_accumulates(repo: Repo) -> None:
    day = quota_day()
    assert repo.quota_used(day) == 0
    assert repo.add_quota(1, day=day) == 1
    assert repo.add_quota(50, day=day) == 51
    assert repo.quota_used(day) == 51


def test_prune_attempts(repo: Repo, seeded: str) -> None:
    from yt_tx.repo import AttemptWrite
    from yt_tx.states import Outcome, Phase

    repo.record_attempt(
        AttemptWrite(
            video_id="vid0000000000001", phase=Phase.TRANSCRIPT, outcome=Outcome.OK
        )
    )
    with repo.begin() as conn:
        conn.execute(
            text("UPDATE fetch_attempts SET started_at = UTC_TIMESTAMP(6) - INTERVAL 60 DAY")
        )
    assert repo.prune_attempts(30) == 1
    assert repo.prune_attempts(30) == 0


# --------------------------------------------------------------------------- #
# Stats, listing, search
# --------------------------------------------------------------------------- #


def test_stats_shape(repo: Repo, seeded: str) -> None:
    stats = repo.stats()
    assert stats.total == 3
    assert stats.remaining == 3
    assert stats.by_status == {Status.METADATA_OK.value: 3}
    assert stats.coverage_pct == 0.0
    assert stats.eta_seconds() is None, "no measured throughput yet"


def test_eta_is_a_range(repo: Repo, seeded: str) -> None:
    video_id = repo.claim_batch("worker-a", limit=1)[0].video_id
    repo.record_transcript_success(
        video_id,
        [
            TranscriptWrite(
                video_id=video_id, language_code="en", kind=TranscriptKind.ASR,
                is_preferred=True, segment_count=1, char_count=1, word_count=1,
                covered_seconds=1.0, raw_path="p", raw_sha256="c" * 64,
                plaintext="x", source="yt-dlp",
            )
        ],
        available=None, run_id=None, worker="worker-a",
    )
    stats = repo.stats()
    assert stats.completed_last_5m == 1
    eta = stats.eta_seconds()
    assert eta is not None
    assert eta[0] < eta[1]


def test_list_videos_pagination_and_filters(repo: Repo, seeded: str) -> None:
    rows, total = repo.list_videos(per_page=2, page=1)
    assert total == 3
    assert len(rows) == 2
    rows, _ = repo.list_videos(per_page=2, page=2)
    assert len(rows) == 1

    rows, total = repo.list_videos(status=Status.METADATA_OK.value)
    assert total == 3
    rows, total = repo.list_videos(query="Video 1")
    assert total == 1
    rows, total = repo.list_videos(query="vid0000000000002")
    assert total == 1


def test_fulltext_search(repo: Repo, seeded: str) -> None:
    video_id = repo.claim_batch("worker-a", limit=1)[0].video_id
    repo.record_transcript_success(
        video_id,
        [
            TranscriptWrite(
                video_id=video_id, language_code="en", kind=TranscriptKind.MANUAL,
                is_preferred=True, segment_count=3, char_count=60, word_count=11,
                covered_seconds=9.0, raw_path="p", raw_sha256="d" * 64,
                plaintext="the mitochondria is the powerhouse of the cell indeed",
                source="youtube-transcript-api",
            )
        ],
        available=None, run_id=None, worker="worker-a",
    )
    hits = repo.search_transcripts("mitochondria")
    assert len(hits) == 1
    assert hits[0]["video_id"] == video_id
    assert "mitochondria" in hits[0]["snippet"]

    # The LIKE fallback must find it too, for when the index is dropped.
    assert len(repo.search_transcripts("powerhouse", use_fulltext=False)) == 1
    assert repo.search_transcripts("   ") == []


def test_audio_queue_is_shortest_first(repo: Repo, channel: str) -> None:
    repo.upsert_videos(
        [
            VideoUpsert(video_id="audioq000001", channel_id=channel),
            VideoUpsert(video_id="audioq000002", channel_id=channel),
        ]
    )
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET status='no_transcript', needs_audio=1, "
                "duration_seconds = IF(video_id='audioq000001', 3600, 120)"
            )
        )
    queue = repo.audio_queue()
    assert [v.video_id for v in queue] == ["audioq000002", "audioq000001"]


def test_fulltext_index_helpers(engine: Engine) -> None:
    with engine.begin() as conn:
        assert ytdb.fulltext_index_exists(conn) is True
        assert ytdb.drop_fulltext_index(conn) is True
        assert ytdb.fulltext_index_exists(conn) is False
        assert ytdb.build_fulltext_index(conn) is True
        assert ytdb.build_fulltext_index(conn) is False


def test_deadlock_retry_replays_the_transaction() -> None:
    """errno 1213 has already rolled back, so the whole unit must be replayable."""
    import pymysql
    from sqlalchemy.exc import OperationalError

    from yt_tx.db import with_deadlock_retry

    calls = {"n": 0}

    @with_deadlock_retry(attempts=3, base_delay=0.0)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise OperationalError(
                "UPDATE videos", {}, pymysql.err.OperationalError(1213, "Deadlock")
            )
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_deadlock_retry_gives_up_and_reraises() -> None:
    import pymysql
    from sqlalchemy.exc import OperationalError

    from yt_tx.db import with_deadlock_retry

    @with_deadlock_retry(attempts=2, base_delay=0.0)
    def always() -> None:
        raise OperationalError(
            "UPDATE videos", {}, pymysql.err.OperationalError(1205, "Lock wait timeout")
        )

    with pytest.raises(OperationalError):
        always()


def test_non_deadlock_errors_are_not_retried() -> None:
    import pymysql
    from sqlalchemy.exc import IntegrityError

    from yt_tx.db import with_deadlock_retry

    calls = {"n": 0}

    @with_deadlock_retry(attempts=3, base_delay=0.0)
    def duplicate() -> None:
        calls["n"] += 1
        raise IntegrityError(
            "INSERT", {}, pymysql.err.IntegrityError(1062, "Duplicate entry")
        )

    with pytest.raises(IntegrityError):
        duplicate()
    assert calls["n"] == 1, "a constraint violation must not be retried"
