"""Data access. Hand-written SQL, SQLAlchemy Core, no ORM.

Conventions that hold everywhere in this module:

* **Server-side time.** Every timestamp comparison and every derived timestamp
  uses ``UTC_TIMESTAMP(6)`` inside the SQL. Python never sends a "now". Mixing
  the two gives you leases that expire early or never, depending on which host's
  clock drifted.
* **Whole transactions retry.** Public write methods own their transaction and
  are wrapped in :func:`~yt_tx.db.with_deadlock_retry`. On errno 1213 InnoDB has
  already rolled the transaction back, so resuming halfway is not an option -
  the entire unit has to be replayable.
* **Upserts never touch ``status``.** Enumeration re-inserts videos it has seen
  before on every run. If the upsert reset ``status``, a re-run would re-download
  every transcript it already had, which defeats the entire point of the
  project. :func:`Repo.upsert_videos` lists metadata columns only, and there is a
  test that fails if ``status`` ever appears there.
* **Transitions are checked.** Every status write goes through
  :func:`~yt_tx.states.assert_transition`. Bulk updates check the source/target
  pairs once, before the statement runs.

MySQL 8.0.20+ is required for the ``AS new`` row-alias upsert syntax, on top of
the 8.0 floor that ``FOR UPDATE SKIP LOCKED`` already imposes.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, cast
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, TextClause, bindparam, text

from .db import as_utc, with_deadlock_retry
from .states import (
    CLAIMABLE,
    RECHECKABLE,
    DesiredState,
    ExitReason,
    Outcome,
    Phase,
    Status,
    TranscriptKind,
    assert_transition,
)

# Google's Data API quota resets at midnight Pacific, not UTC.
RowLike = Mapping[Any, Any]
"""A row from ``.mappings()``, or a plain dict in tests.

Not ``Mapping[str, Any]``: SQLAlchemy's ``RowMapping`` is keyed on
``str | Column``, and ``Mapping`` is invariant in its key type, so the narrower
annotation rejects every real row.
"""

QUOTA_TZ: Final = ZoneInfo("America/Los_Angeles")

MAX_RECHECKS: Final = 4
"""After this many rechecks a video is genuinely audio-only."""

DEFAULT_CLAIM_BATCH: Final = 25


def quota_day(now: datetime | None = None) -> date:
    """Today's date in Pacific time, which is the unit Google bills in."""
    from datetime import timezone

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(QUOTA_TZ).date()


def _json_dump(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _in_clause(sql: str, *names: str) -> TextClause:
    """``text()`` with expanding bind params, so ``IN :ids`` works properly.

    Without ``expanding=True`` a list parameter reaches the driver as a single
    value and the ``IN`` either errors or silently matches nothing. Relying on
    PyMySQL's tuple escaping happens to work but is undocumented behaviour to
    hang a claim query on.
    """
    return text(sql).bindparams(*(bindparam(n, expanding=True) for n in names))


def _json_load(value: object) -> Any:
    """MySQL JSON comes back as ``str`` through raw ``text()`` queries."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


# --------------------------------------------------------------------------- #
# Row types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChannelRow:
    channel_id: str
    input_ref: str
    handle: str | None
    title: str | None
    uploads_playlist_id: str | None
    reported_video_count: int | None
    enumeration_cursor: str | None
    enumeration_complete: bool
    last_enumerated_at: datetime | None
    newest_published_at: datetime | None
    is_enabled: bool

    @classmethod
    def from_row(cls, row: RowLike) -> ChannelRow:
        return cls(
            channel_id=row["channel_id"],
            input_ref=row["input_ref"],
            handle=row["handle"],
            title=row["title"],
            uploads_playlist_id=row["uploads_playlist_id"],
            reported_video_count=row["reported_video_count"],
            enumeration_cursor=row["enumeration_cursor"],
            enumeration_complete=bool(row["enumeration_complete"]),
            last_enumerated_at=as_utc(row["last_enumerated_at"]),
            newest_published_at=as_utc(row["newest_published_at"]),
            is_enabled=bool(row["is_enabled"]),
        )


@dataclass(frozen=True, slots=True)
class VideoRow:
    video_id: str
    channel_id: str
    title: str | None
    published_at: datetime | None
    duration_seconds: int | None
    status: Status
    status_reason: str | None
    attempts: int
    recheck_count: int
    needs_audio: bool
    is_short: bool | None
    was_livestream: bool | None
    live_broadcast_content: str | None
    available_transcripts: Any
    view_count: int | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    tags: Any = None
    like_count: int | None = None
    comment_count: int | None = None
    category_id: str | None = None
    default_language: str | None = None
    default_audio_language: str | None = None
    metadata_fetched_at: datetime | None = None
    next_attempt_at: datetime | None = None
    recheck_after: datetime | None = None
    claimed_by: str | None = None
    lease_expires_at: datetime | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> VideoRow:
        def maybe_bool(key: str) -> bool | None:
            value = row.get(key)
            return None if value is None else bool(value)

        return cls(
            video_id=row["video_id"],
            channel_id=row["channel_id"],
            title=row.get("title"),
            published_at=as_utc(row.get("published_at")),
            duration_seconds=row.get("duration_seconds"),
            status=Status(row["status"]),
            status_reason=row.get("status_reason"),
            attempts=int(row.get("attempts") or 0),
            recheck_count=int(row.get("recheck_count") or 0),
            needs_audio=bool(row.get("needs_audio")),
            is_short=maybe_bool("is_short"),
            was_livestream=maybe_bool("was_livestream"),
            live_broadcast_content=row.get("live_broadcast_content"),
            available_transcripts=_json_load(row.get("available_transcripts_json")),
            view_count=row.get("view_count"),
            description=row.get("description"),
            thumbnail_url=row.get("thumbnail_url"),
            tags=_json_load(row.get("tags_json")),
            like_count=row.get("like_count"),
            comment_count=row.get("comment_count"),
            category_id=row.get("category_id"),
            default_language=row.get("default_language"),
            default_audio_language=row.get("default_audio_language"),
            metadata_fetched_at=as_utc(row.get("metadata_fetched_at")),
            next_attempt_at=as_utc(row.get("next_attempt_at")),
            recheck_after=as_utc(row.get("recheck_after")),
            claimed_by=row.get("claimed_by"),
            lease_expires_at=as_utc(row.get("lease_expires_at")),
        )


@dataclass(frozen=True, slots=True)
class TranscriptRow:
    id: int
    video_id: str
    language_code: str
    kind: TranscriptKind
    is_preferred: bool
    segment_count: int | None
    char_count: int | None
    word_count: int | None
    covered_seconds: float | None
    raw_path: str
    raw_sha256: str
    source: str
    fetched_at: datetime | None
    plaintext: str | None = None

    @classmethod
    def from_row(cls, row: RowLike) -> TranscriptRow:
        covered = row.get("covered_seconds")
        return cls(
            id=int(row["id"]),
            video_id=row["video_id"],
            language_code=row["language_code"],
            kind=TranscriptKind(row["kind"]),
            is_preferred=bool(row["is_preferred"]),
            segment_count=row.get("segment_count"),
            char_count=row.get("char_count"),
            word_count=row.get("word_count"),
            covered_seconds=float(covered) if covered is not None else None,
            raw_path=row["raw_path"],
            raw_sha256=row["raw_sha256"],
            source=row["source"],
            fetched_at=as_utc(row.get("fetched_at")),
            plaintext=row.get("plaintext"),
        )


@dataclass(frozen=True, slots=True)
class RunRow:
    id: int
    command: str
    args: Any
    pid: int | None
    host: str | None
    log_path: str | None
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    counts: Any
    exit_reason: str | None

    @property
    def is_active(self) -> bool:
        return self.finished_at is None

    @classmethod
    def from_row(cls, row: RowLike) -> RunRow:
        return cls(
            id=int(row["id"]),
            command=row["command"],
            args=_json_load(row.get("args_json")),
            pid=row.get("pid"),
            host=row.get("host"),
            log_path=row.get("log_path"),
            started_at=as_utc(row.get("started_at")),
            finished_at=as_utc(row.get("finished_at")),
            heartbeat_at=as_utc(row.get("heartbeat_at")),
            counts=_json_load(row.get("counts_json")),
            exit_reason=row.get("exit_reason"),
        )


@dataclass(frozen=True, slots=True)
class Control:
    desired_state: DesiredState
    concurrency: int
    requests_per_second: float
    updated_at: datetime | None

    @property
    def should_stop(self) -> bool:
        return self.desired_state is DesiredState.STOPPING

    @property
    def is_paused(self) -> bool:
        return self.desired_state is DesiredState.PAUSED


@dataclass(frozen=True, slots=True)
class VideoUpsert:
    """What enumeration knows about a video. Deliberately metadata-only."""

    video_id: str
    channel_id: str
    title: str | None = None
    published_at: datetime | None = None
    description: str | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataUpdate:
    """What ``videos.list`` (or yt-dlp) hydration produces."""

    video_id: str
    title: str | None = None
    description: str | None = None
    published_at: datetime | None = None
    duration_seconds: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    tags: list[str] | None = None
    category_id: str | None = None
    default_language: str | None = None
    default_audio_language: str | None = None
    live_broadcast_content: str | None = None
    is_short: bool | None = None
    was_livestream: bool | None = None
    thumbnail_url: str | None = None
    status: Status = Status.METADATA_OK
    status_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TranscriptWrite:
    video_id: str
    language_code: str
    kind: TranscriptKind
    is_preferred: bool
    segment_count: int
    char_count: int
    word_count: int
    covered_seconds: float
    raw_path: str
    raw_sha256: str
    plaintext: str
    source: str


@dataclass(frozen=True, slots=True)
class AttemptWrite:
    video_id: str
    phase: Phase
    outcome: Outcome
    run_id: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    http_status: int | None = None
    traceback: str | None = None
    worker: str | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ChannelStats:
    channel_id: str
    handle: str | None
    title: str | None
    input_ref: str
    is_enabled: bool
    enumeration_complete: bool
    reported_video_count: int | None
    last_enumerated_at: datetime | None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def done(self) -> int:
        return self.counts.get(Status.TRANSCRIPT_OK.value, 0)

    @property
    def no_transcript(self) -> int:
        return self.counts.get(Status.NO_TRANSCRIPT.value, 0)

    @property
    def failed(self) -> int:
        return self.counts.get(Status.FAILED.value, 0)

    @property
    def coverage_pct(self) -> float:
        total = self.total
        return 0.0 if total == 0 else round(100.0 * self.done / total, 1)


@dataclass(frozen=True, slots=True)
class Stats:
    by_status: dict[str, int]
    total: int
    remaining: int
    completed_last_5m: int
    needs_audio: int
    transcripts: int
    active_run: RunRow | None
    quota_used: int
    quota_day: date

    @property
    def coverage_pct(self) -> float:
        if self.total == 0:
            return 0.0
        done = self.by_status.get(Status.TRANSCRIPT_OK.value, 0)
        return round(100.0 * done / self.total, 1)

    @property
    def videos_per_minute(self) -> float:
        return round(self.completed_last_5m / 5.0, 1)

    def eta_seconds(self) -> tuple[int, int] | None:
        """Remaining work over the trailing rate, as a (low, high) range.

        ``None`` when there is no measured throughput. A single-number ETA
        derived from three data points is a lie told with false precision; a
        range at least signals its own uncertainty.
        """
        if self.remaining <= 0:
            return (0, 0)
        rate_per_second = self.completed_last_5m / 300.0
        if rate_per_second <= 0:
            return None
        centre = self.remaining / rate_per_second
        return (int(centre * 0.75), int(centre * 1.5))


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #

# Metadata columns only. `status`, `attempts`, `needs_audio`, `recheck_*` and the
# lease columns are absent on purpose: enumeration re-inserts rows it has already
# completed, and clobbering their status would re-download every transcript.
_UPSERT_VIDEOS: Final = """
INSERT INTO videos
  (video_id, channel_id, title, description, published_at, thumbnail_url, status)
VALUES
  (:video_id, :channel_id, :title, :description, :published_at, :thumbnail_url,
   'discovered')
AS new
ON DUPLICATE KEY UPDATE
  channel_id    = new.channel_id,
  title         = COALESCE(new.title, videos.title),
  description   = COALESCE(new.description, videos.description),
  published_at  = COALESCE(new.published_at, videos.published_at),
  thumbnail_url = COALESCE(new.thumbnail_url, videos.thumbnail_url)
"""

_CLAIM_SELECT: Final = """
SELECT video_id FROM videos
 WHERE status IN ('metadata_ok','retry'{extra_statuses})
   AND (next_attempt_at IS NULL OR next_attempt_at <= UTC_TIMESTAMP(6))
   AND (lease_expires_at IS NULL OR lease_expires_at  <  UTC_TIMESTAMP(6))
   {channel_filter}
 ORDER BY published_at ASC
 LIMIT :limit
 FOR UPDATE SKIP LOCKED
"""

# Auto-captions often appear hours or days after upload, so a fresh video is
# worth revisiting soon and an old one almost never is.
_RECHECK_CASE: Final = """
CASE
  WHEN recheck_count >= :max_rechecks THEN NULL
  WHEN published_at IS NULL THEN NULL
  WHEN published_at > UTC_TIMESTAMP(6) - INTERVAL 7 DAY
       THEN UTC_TIMESTAMP(6) + INTERVAL 6 HOUR
  WHEN published_at > UTC_TIMESTAMP(6) - INTERVAL 30 DAY
       THEN UTC_TIMESTAMP(6) + INTERVAL 3 DAY
  WHEN published_at > UTC_TIMESTAMP(6) - INTERVAL 180 DAY
       THEN UTC_TIMESTAMP(6) + INTERVAL 30 DAY
  ELSE NULL
END
"""


class Repo:
    """All database access. One instance per process, shared across threads.

    SQLAlchemy's pool is thread-safe and each method takes a connection out of it
    for the duration of one transaction, so a :class:`Repo` is safe to share
    between worker threads.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    # -- plumbing ---------------------------------------------------------- #

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    @contextmanager
    def connect(self) -> Iterator[Connection]:
        with self.engine.connect() as conn:
            yield conn

    def server_now(self) -> datetime:
        """The database's clock. The only "now" any comparison should use."""
        with self.connect() as conn:
            value = conn.execute(text("SELECT UTC_TIMESTAMP(6)")).scalar_one()
        return cast(datetime, as_utc(cast(datetime, value)))

    # -- settings ---------------------------------------------------------- #

    def get_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute(text("SELECT `key`, value_json FROM settings")).all()
        return {row[0]: _json_load(row[1]) for row in rows}

    @with_deadlock_retry()
    def put_settings(self, values: Mapping[str, Any]) -> None:
        # json.dumps, not _json_dump: a null knob (proxy, cookies_file) must be
        # stored as the JSON literal `null`, because settings.value_json is NOT
        # NULL. _json_dump maps None to SQL NULL, which is the right thing for
        # the COALESCE-guarded columns elsewhere and exactly wrong here.
        if not values:
            return
        payload = [
            {"k": k, "v": json.dumps(v, ensure_ascii=False)} for k, v in values.items()
        ]
        with self.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO settings (`key`, value_json) VALUES (:k, CAST(:v AS JSON)) "
                    "AS new ON DUPLICATE KEY UPDATE value_json = new.value_json"
                ),
                payload,
            )

    @with_deadlock_retry()
    def seed_settings(self, values: Mapping[str, Any]) -> list[str]:
        """Insert only keys that are absent. Returns the keys actually written."""
        if not values:
            return []
        existing = set(self.get_settings())
        fresh = {k: v for k, v in values.items() if k not in existing}
        if fresh:
            self.put_settings(fresh)
        return sorted(fresh)

    # -- runtime control --------------------------------------------------- #

    def get_control(self) -> Control:
        with self.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT desired_state, concurrency, requests_per_second, "
                    "updated_at FROM runtime_control WHERE id = 1"
                )
            ).mappings().one_or_none()
        if row is None:
            return Control(DesiredState.RUNNING, 3, 0.66, None)
        return Control(
            desired_state=DesiredState(row["desired_state"]),
            concurrency=int(row["concurrency"]),
            requests_per_second=float(row["requests_per_second"]),
            updated_at=as_utc(row["updated_at"]),
        )

    @with_deadlock_retry()
    def set_control(
        self,
        *,
        desired_state: DesiredState | None = None,
        concurrency: int | None = None,
        requests_per_second: float | None = None,
    ) -> None:
        sets: list[str] = []
        params: dict[str, Any] = {}
        if desired_state is not None:
            sets.append("desired_state = :state")
            params["state"] = desired_state.value
        if concurrency is not None:
            sets.append("concurrency = :conc")
            params["conc"] = concurrency
        if requests_per_second is not None:
            sets.append("requests_per_second = :rps")
            params["rps"] = requests_per_second
        if not sets:
            return
        with self.begin() as conn:
            conn.execute(
                text("INSERT IGNORE INTO runtime_control (id) VALUES (1)")
            )
            conn.execute(
                text(f"UPDATE runtime_control SET {', '.join(sets)} WHERE id = 1"),
                params,
            )

    # -- channels ---------------------------------------------------------- #

    @with_deadlock_retry()
    def upsert_channel(
        self,
        *,
        channel_id: str,
        input_ref: str,
        handle: str | None = None,
        title: str | None = None,
        uploads_playlist_id: str | None = None,
        reported_video_count: int | None = None,
    ) -> None:
        """Record a resolved channel. Resolution is cached forever."""
        with self.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO channels
                      (channel_id, input_ref, handle, title, uploads_playlist_id,
                       reported_video_count)
                    VALUES
                      (:channel_id, :input_ref, :handle, :title,
                       :uploads_playlist_id, :reported_video_count)
                    AS new
                    ON DUPLICATE KEY UPDATE
                      input_ref            = new.input_ref,
                      handle               = COALESCE(new.handle, channels.handle),
                      title                = COALESCE(new.title, channels.title),
                      uploads_playlist_id  = COALESCE(new.uploads_playlist_id,
                                                      channels.uploads_playlist_id),
                      reported_video_count = COALESCE(new.reported_video_count,
                                                      channels.reported_video_count)
                    """
                ),
                {
                    "channel_id": channel_id,
                    "input_ref": input_ref,
                    "handle": handle,
                    "title": title,
                    "uploads_playlist_id": uploads_playlist_id,
                    "reported_video_count": reported_video_count,
                },
            )

    def get_channel(self, channel_id: str) -> ChannelRow | None:
        with self.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM channels WHERE channel_id = :cid"),
                {"cid": channel_id},
            ).mappings().one_or_none()
        return None if row is None else ChannelRow.from_row(row)

    def find_channel_by_ref(self, ref: str) -> ChannelRow | None:
        """Look up an already-resolved channel by handle or original input.

        Saves a Data API call (and a quota unit) on every re-run.
        """
        with self.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT * FROM channels "
                    "WHERE channel_id = :ref OR handle = :ref OR input_ref = :ref "
                    "LIMIT 1"
                ),
                {"ref": ref},
            ).mappings().one_or_none()
        return None if row is None else ChannelRow.from_row(row)

    def _enabled_channel_ids(self) -> list[str]:
        """Channel ids the pipeline is allowed to touch.

        Resolved in Python and passed as an IN list rather than left as a
        subquery, because the claim query runs under ``FOR UPDATE`` and would
        otherwise take locks on `channels` rows too - making the UI's
        enable/disable toggle wait on a running fetch.
        """
        with self.connect() as conn:
            return list(
                conn.execute(
                    text("SELECT channel_id FROM channels WHERE is_enabled = 1")
                ).scalars().all()
            )

    def list_channels(self, *, enabled_only: bool = False) -> list[ChannelRow]:
        sql = "SELECT * FROM channels"
        if enabled_only:
            sql += " WHERE is_enabled = 1"
        sql += " ORDER BY COALESCE(handle, title, channel_id)"
        with self.connect() as conn:
            rows = conn.execute(text(sql)).mappings().all()
        return [ChannelRow.from_row(r) for r in rows]

    @with_deadlock_retry()
    def set_channel_enabled(self, channel_id: str, enabled: bool) -> bool:
        with self.begin() as conn:
            result = conn.execute(
                text("UPDATE channels SET is_enabled = :e WHERE channel_id = :cid"),
                {"e": 1 if enabled else 0, "cid": channel_id},
            )
        return result.rowcount > 0

    @with_deadlock_retry()
    def delete_channel(self, channel_id: str) -> bool:
        """Delete a channel; ON DELETE CASCADE takes its videos and transcripts.

        Transcript *files* are left behind on disk deliberately - ``doctor``
        reports them as orphans and can reclaim them. An orphan file is
        harmless; deleting files inside a DB transaction that might roll back
        is not.
        """
        with self.begin() as conn:
            result = conn.execute(
                text("DELETE FROM channels WHERE channel_id = :cid"),
                {"cid": channel_id},
            )
        return result.rowcount > 0

    @with_deadlock_retry()
    def save_enumeration_cursor(
        self,
        channel_id: str,
        *,
        cursor: str | None,
        complete: bool = False,
        newest_published_at: datetime | None = None,
    ) -> None:
        """Persist pagination progress. Called after **every** page.

        This is what makes a crash mid-channel resume instead of restarting.
        """
        with self.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE channels SET
                      enumeration_cursor   = :cursor,
                      enumeration_complete = :complete,
                      last_enumerated_at   = UTC_TIMESTAMP(6),
                      newest_published_at  = GREATEST(
                          COALESCE(newest_published_at, '1970-01-01'),
                          COALESCE(:newest, '1970-01-01'))
                    WHERE channel_id = :cid
                    """
                ),
                {
                    "cursor": cursor,
                    "complete": 1 if complete else 0,
                    "newest": newest_published_at,
                    "cid": channel_id,
                },
            )

    # -- videos: enumeration ----------------------------------------------- #

    @with_deadlock_retry()
    def upsert_videos(self, videos: Sequence[VideoUpsert]) -> int:
        """Insert discovered videos, updating metadata but never ``status``.

        Returns:
            Number of rows the server reported as affected. MySQL counts an
            unchanged duplicate as 0 and a modified duplicate as 2, so treat this
            as a rough signal, not a count of new videos.
        """
        if not videos:
            return 0
        payload = [
            {
                "video_id": v.video_id,
                "channel_id": v.channel_id,
                "title": v.title,
                "description": v.description,
                "published_at": v.published_at,
                "thumbnail_url": v.thumbnail_url,
            }
            for v in videos
        ]
        with self.begin() as conn:
            result = conn.execute(text(_UPSERT_VIDEOS), payload)
        return int(result.rowcount or 0)

    def count_new_videos(self, video_ids: Sequence[str]) -> int:
        """How many of these ids we have never seen. For accurate run counters."""
        if not video_ids:
            return 0
        with self.connect() as conn:
            existing = conn.execute(
                _in_clause("SELECT video_id FROM videos WHERE video_id IN :ids", "ids"),
                {"ids": list(video_ids)},
            ).scalars().all()
        return len(set(video_ids)) - len(set(existing))

    # -- videos: hydration ------------------------------------------------- #

    def videos_needing_metadata(
        self,
        *,
        channel_id: str | None = None,
        limit: int = 50,
        exclude: Collection[str] | None = None,
        enabled_only: bool = True,
    ) -> list[str]:
        """Ids awaiting hydration.

        ``exclude`` skips ids the caller has already tried and deferred this
        pass. Without it, a video that fails transiently stays ``discovered`` and
        comes straight back at the top of the next query, and hydrate spins on it
        forever.

        ``enabled_only`` honours the channel switch, so a disabled channel is
        inert everywhere rather than only in discovery. An explicit
        ``channel_id`` overrides it: naming a channel outright is a deliberate
        act and should work on a disabled one.
        """
        sql = "SELECT video_id FROM videos WHERE status = 'discovered'"
        params: dict[str, Any] = {"limit": limit}
        names: list[str] = []
        if channel_id:
            sql += " AND channel_id = :cid"
            params["cid"] = channel_id
        elif enabled_only:
            enabled = self._enabled_channel_ids()
            if not enabled:
                return []
            sql += " AND channel_id IN :channels"
            params["channels"] = enabled
            names.append("channels")
        if exclude:
            sql += " AND video_id NOT IN :exclude"
            params["exclude"] = list(exclude)
            names.append("exclude")
        sql += " ORDER BY published_at ASC LIMIT :limit"
        with self.connect() as conn:
            return list(conn.execute(_in_clause(sql, *names), params).scalars().all())

    @with_deadlock_retry()
    def apply_metadata(self, updates: Sequence[MetadataUpdate]) -> int:
        """Write hydrated metadata and move ``discovered`` on to its next state.

        Legality of every transition is checked before the statement runs.
        """
        if not updates:
            return 0
        targets = {u.status for u in updates}
        for target in targets:
            for source in (Status.DISCOVERED, Status.METADATA_OK, Status.RETRY):
                assert_transition("<bulk>", source, target)

        payload = [
            {
                "video_id": u.video_id,
                "title": u.title,
                "description": u.description,
                "published_at": u.published_at,
                "duration_seconds": u.duration_seconds,
                "view_count": u.view_count,
                "like_count": u.like_count,
                "comment_count": u.comment_count,
                "tags_json": _json_dump(u.tags),
                "category_id": u.category_id,
                "default_language": u.default_language,
                "default_audio_language": u.default_audio_language,
                "live_broadcast_content": u.live_broadcast_content,
                "is_short": None if u.is_short is None else int(u.is_short),
                "was_livestream": (
                    None if u.was_livestream is None else int(u.was_livestream)
                ),
                "thumbnail_url": u.thumbnail_url,
                "status": u.status.value,
                "status_reason": u.status_reason,
            }
            for u in updates
        ]
        with self.begin() as conn:
            result = conn.execute(
                text(
                    """
                    UPDATE videos SET
                      title                  = COALESCE(:title, title),
                      description            = COALESCE(:description, description),
                      published_at           = COALESCE(:published_at, published_at),
                      duration_seconds       = :duration_seconds,
                      view_count             = :view_count,
                      like_count             = :like_count,
                      comment_count          = :comment_count,
                      tags_json              = CAST(:tags_json AS JSON),
                      category_id            = :category_id,
                      default_language       = :default_language,
                      default_audio_language = :default_audio_language,
                      live_broadcast_content = :live_broadcast_content,
                      is_short               = :is_short,
                      was_livestream         = :was_livestream,
                      thumbnail_url          = COALESCE(:thumbnail_url, thumbnail_url),
                      metadata_fetched_at    = UTC_TIMESTAMP(6),
                      status                 = :status,
                      status_reason          = :status_reason,
                      next_attempt_at        = NULL
                    WHERE video_id = :video_id
                      AND status IN ('discovered','metadata_ok','retry')
                    """
                ),
                payload,
            )
        return int(result.rowcount or 0)

    @with_deadlock_retry()
    def mark_videos_unavailable(
        self, video_ids: Sequence[str], reason: str
    ) -> int:
        """For ids the Data API refused to return - deleted or private."""
        if not video_ids:
            return 0
        for source in (Status.DISCOVERED, Status.METADATA_OK, Status.RETRY):
            assert_transition("<bulk>", source, Status.UNAVAILABLE)
        with self.begin() as conn:
            result = conn.execute(
                _in_clause(
                    "UPDATE videos SET status = 'unavailable', status_reason = :reason, "
                    "metadata_fetched_at = UTC_TIMESTAMP(6), claimed_by = NULL, "
                    "lease_expires_at = NULL "
                    "WHERE video_id IN :ids "
                    "AND status IN ('discovered','metadata_ok','retry')",
                    "ids",
                ),
                {"reason": reason[:255], "ids": list(video_ids)},
            )
        return int(result.rowcount or 0)

    # -- videos: claiming -------------------------------------------------- #

    @with_deadlock_retry()
    def claim_batch(
        self,
        worker: str,
        *,
        limit: int = DEFAULT_CLAIM_BATCH,
        lease_seconds: int = 600,
        channel_id: str | None = None,
        include_unhydrated: bool = False,
        enabled_only: bool = True,
    ) -> list[VideoRow]:
        """Lease a batch of videos for the transcript stage.

        ``FOR UPDATE SKIP LOCKED`` is the reason MySQL 8.0 is a hard floor. Two
        workers running this concurrently get disjoint sets instead of one
        blocking on the other, and a worker killed mid-batch simply lets its
        lease lapse - the reaper returns those rows to the queue.

        The select and the lease update share one transaction; without that, the
        row locks would be released before the lease was written and two workers
        could claim the same video.

        ``include_unhydrated`` also claims ``discovered`` videos, which is what
        --skip-hydrate means: captions need no metadata. ``enabled_only``
        excludes disabled channels unless ``channel_id`` names one explicitly.
        """
        params: dict[str, Any] = {"limit": limit}
        names: list[str] = []
        channel_filter = ""
        if channel_id:
            channel_filter = "AND channel_id = :cid"
            params["cid"] = channel_id
        elif enabled_only:
            enabled = self._enabled_channel_ids()
            if not enabled:
                return []
            channel_filter = "AND channel_id IN :channels"
            params["channels"] = enabled
            names.append("channels")

        sql = _CLAIM_SELECT.format(
            channel_filter=channel_filter,
            extra_statuses=",'discovered'" if include_unhydrated else "",
        )

        with self.begin() as conn:
            ids = list(
                conn.execute(_in_clause(sql, *names), params).scalars().all()
            )
            if not ids:
                return []
            conn.execute(
                _in_clause(
                    "UPDATE videos SET claimed_by = :worker, "
                    "lease_expires_at = UTC_TIMESTAMP(6) + INTERVAL :lease SECOND "
                    "WHERE video_id IN :ids",
                    "ids",
                ),
                {"worker": worker, "lease": lease_seconds, "ids": ids},
            )
            # Re-apply the ordering. The SELECT ... FOR UPDATE above chose these
            # ids oldest-first, but an unordered `WHERE video_id IN (...)` hands
            # them back in whatever order InnoDB feels like (primary key, in
            # practice), silently discarding the ordering the caller relies on.
            rows = conn.execute(
                _in_clause(
                    "SELECT * FROM videos WHERE video_id IN :ids "
                    "ORDER BY published_at ASC, video_id",
                    "ids",
                ),
                {"ids": ids},
            ).mappings().all()
        return [VideoRow.from_row(r) for r in rows]

    @with_deadlock_retry()
    def extend_lease(
        self, video_ids: Sequence[str], worker: str, lease_seconds: int
    ) -> int:
        """Push a lease out for work that is legitimately taking a long time."""
        if not video_ids:
            return 0
        with self.begin() as conn:
            result = conn.execute(
                _in_clause(
                    "UPDATE videos SET lease_expires_at = "
                    "UTC_TIMESTAMP(6) + INTERVAL :lease SECOND "
                    "WHERE video_id IN :ids AND claimed_by = :worker",
                    "ids",
                ),
                {"lease": lease_seconds, "ids": list(video_ids), "worker": worker},
            )
        return int(result.rowcount or 0)

    @with_deadlock_retry()
    def release_claims(
        self, worker: str, *, video_ids: Sequence[str] | None = None
    ) -> int:
        """Drop leases without changing status. Used when draining on shutdown."""
        sql = (
            "UPDATE videos SET claimed_by = NULL, lease_expires_at = NULL "
            "WHERE claimed_by = :worker"
        )
        params: dict[str, Any] = {"worker": worker}
        names: list[str] = []
        if video_ids:
            sql += " AND video_id IN :ids"
            params["ids"] = list(video_ids)
            names.append("ids")
        with self.begin() as conn:
            result = conn.execute(_in_clause(sql, *names), params)
        return int(result.rowcount or 0)

    @with_deadlock_retry()
    def reap_expired_leases(self) -> int:
        """Return abandoned rows to the queue.

        Run at startup and every 60s. A worker that was SIGKILLed leaves rows
        claimed forever otherwise, and they become invisible to every future run.
        """
        for source in CLAIMABLE:
            assert_transition("<bulk>", source, Status.RETRY)
        with self.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE videos SET status = 'retry', claimed_by = NULL, "
                    "lease_expires_at = NULL, next_attempt_at = UTC_TIMESTAMP(6), "
                    "status_reason = 'lease expired; worker vanished' "
                    "WHERE lease_expires_at IS NOT NULL "
                    "AND lease_expires_at < UTC_TIMESTAMP(6) "
                    "AND status IN ('metadata_ok','retry')"
                )
            )
        return int(result.rowcount or 0)

    def count_stale_leases(self) -> int:
        with self.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM videos WHERE lease_expires_at IS NOT NULL "
                        "AND lease_expires_at < UTC_TIMESTAMP(6)"
                    )
                ).scalar_one()
            )

    # -- videos: outcomes -------------------------------------------------- #

    @with_deadlock_retry()
    def record_transcript_success(
        self,
        video_id: str,
        transcripts: Sequence[TranscriptWrite],
        *,
        available: object,
        run_id: int | None,
        worker: str | None,
        duration_seconds: float | None = None,
    ) -> None:
        """Commit transcripts, the video's new status, and the attempt together.

        One transaction on purpose. A crash between writing the transcript and
        setting ``transcript_ok`` would otherwise leave a stored transcript that
        every future run tries to download again.

        The caller must already have written and fsynced the gzip files. An
        orphan file on disk is reclaimable; a row pointing at a file that does
        not exist is corruption.
        """
        if not transcripts:
            raise ValueError("record_transcript_success needs at least one transcript")
        with self.begin() as conn:
            current = self._locked_status(conn, video_id)
            if current is None:
                raise KeyError(f"unknown video {video_id}")
            assert_transition(video_id, current, Status.TRANSCRIPT_OK)

            for tx in transcripts:
                conn.execute(
                    text(
                        """
                        INSERT INTO transcripts
                          (video_id, language_code, kind, is_preferred, segment_count,
                           char_count, word_count, covered_seconds, raw_path,
                           raw_sha256, plaintext, source, fetched_at)
                        VALUES
                          (:video_id, :language_code, :kind, :is_preferred,
                           :segment_count, :char_count, :word_count, :covered_seconds,
                           :raw_path, :raw_sha256, :plaintext, :source,
                           UTC_TIMESTAMP(6))
                        AS new
                        ON DUPLICATE KEY UPDATE
                          is_preferred    = new.is_preferred,
                          segment_count   = new.segment_count,
                          char_count      = new.char_count,
                          word_count      = new.word_count,
                          covered_seconds = new.covered_seconds,
                          raw_path        = new.raw_path,
                          raw_sha256      = new.raw_sha256,
                          plaintext       = new.plaintext,
                          source          = new.source,
                          fetched_at      = UTC_TIMESTAMP(6)
                        """
                    ),
                    {
                        "video_id": tx.video_id,
                        "language_code": tx.language_code,
                        "kind": tx.kind.value,
                        "is_preferred": int(tx.is_preferred),
                        "segment_count": tx.segment_count,
                        "char_count": tx.char_count,
                        "word_count": tx.word_count,
                        "covered_seconds": tx.covered_seconds,
                        "raw_path": tx.raw_path,
                        "raw_sha256": tx.raw_sha256,
                        "plaintext": tx.plaintext,
                        "source": tx.source,
                    },
                )

            conn.execute(
                text(
                    """
                    UPDATE videos SET
                      status = 'transcript_ok',
                      status_reason = NULL,
                      attempts = attempts + 1,
                      needs_audio = 0,
                      next_attempt_at = NULL,
                      recheck_after = NULL,
                      claimed_by = NULL,
                      lease_expires_at = NULL,
                      available_transcripts_json = CAST(:available AS JSON)
                    WHERE video_id = :video_id
                    """
                ),
                {"available": _json_dump(available), "video_id": video_id},
            )
            self._insert_attempt(
                conn,
                AttemptWrite(
                    video_id=video_id,
                    phase=Phase.TRANSCRIPT,
                    outcome=Outcome.OK,
                    run_id=run_id,
                    worker=worker,
                    duration_seconds=duration_seconds,
                ),
            )

    @with_deadlock_retry()
    def record_terminal(
        self,
        video_id: str,
        *,
        status: Status,
        reason: str | None,
        needs_audio: bool,
        available: object = None,
        run_id: int | None = None,
        worker: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        http_status: int | None = None,
        traceback: str | None = None,
        schedule_recheck: bool = False,
    ) -> None:
        """Reach a conclusion about a video, and schedule a recheck if apt."""
        with self.begin() as conn:
            current = self._locked_status(conn, video_id)
            if current is None:
                raise KeyError(f"unknown video {video_id}")
            assert_transition(video_id, current, status)

            recheck_expr = _RECHECK_CASE if schedule_recheck else "NULL"
            conn.execute(
                text(
                    f"""
                    UPDATE videos SET
                      status = :status,
                      status_reason = :reason,
                      attempts = attempts + 1,
                      needs_audio = :needs_audio,
                      next_attempt_at = NULL,
                      recheck_after = {recheck_expr},
                      claimed_by = NULL,
                      lease_expires_at = NULL,
                      available_transcripts_json = COALESCE(
                          CAST(:available AS JSON), available_transcripts_json)
                    WHERE video_id = :video_id
                    """
                ),
                {
                    "status": status.value,
                    "reason": None if reason is None else reason[:255],
                    "needs_audio": int(needs_audio),
                    "available": _json_dump(available),
                    "video_id": video_id,
                    "max_rechecks": MAX_RECHECKS,
                },
            )
            self._insert_attempt(
                conn,
                AttemptWrite(
                    video_id=video_id,
                    phase=Phase.TRANSCRIPT,
                    outcome=Outcome.TERMINAL,
                    run_id=run_id,
                    worker=worker,
                    error_type=error_type,
                    error_message=error_message,
                    http_status=http_status,
                    traceback=traceback,
                ),
            )

    @with_deadlock_retry()
    def record_retry(
        self,
        video_id: str,
        *,
        reason: str | None,
        delay_seconds: float,
        exhausted_status: Status = Status.FAILED,
        max_attempts: int,
        run_id: int | None = None,
        worker: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        http_status: int | None = None,
        traceback: str | None = None,
    ) -> Status:
        """Schedule another attempt, or give up if the ceiling is reached.

        Returns:
            The status the video ended up in - ``retry`` or ``failed``.
        """
        with self.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT status, attempts FROM videos WHERE video_id = :vid FOR UPDATE"
                ),
                {"vid": video_id},
            ).mappings().one_or_none()
            if row is None:
                raise KeyError(f"unknown video {video_id}")

            attempts_after = int(row["attempts"]) + 1
            target = (
                exhausted_status if attempts_after >= max_attempts else Status.RETRY
            )
            assert_transition(video_id, Status(row["status"]), target)

            if target is Status.RETRY:
                conn.execute(
                    text(
                        "UPDATE videos SET status = 'retry', status_reason = :reason, "
                        "attempts = attempts + 1, "
                        "next_attempt_at = UTC_TIMESTAMP(6) + "
                        "  INTERVAL :delay_us MICROSECOND, "
                        "claimed_by = NULL, lease_expires_at = NULL "
                        "WHERE video_id = :vid"
                    ),
                    {
                        "reason": None if reason is None else reason[:255],
                        "delay_us": int(max(0.0, delay_seconds) * 1_000_000),
                        "vid": video_id,
                    },
                )
            else:
                conn.execute(
                    text(
                        "UPDATE videos SET status = :status, status_reason = :reason, "
                        "attempts = attempts + 1, next_attempt_at = NULL, "
                        "claimed_by = NULL, lease_expires_at = NULL "
                        "WHERE video_id = :vid"
                    ),
                    {
                        "status": target.value,
                        "reason": None if reason is None else reason[:255],
                        "vid": video_id,
                    },
                )

            self._insert_attempt(
                conn,
                AttemptWrite(
                    video_id=video_id,
                    phase=Phase.TRANSCRIPT,
                    outcome=Outcome.RETRYABLE,
                    run_id=run_id,
                    worker=worker,
                    error_type=error_type,
                    error_message=error_message,
                    http_status=http_status,
                    traceback=traceback,
                ),
            )
        return target

    @with_deadlock_retry()
    def record_block(
        self,
        video_id: str,
        *,
        run_id: int | None = None,
        worker: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        http_status: int | None = None,
    ) -> None:
        """Log a block and requeue the video **without touching its status**.

        The whole point: YouTube refused *us*, so the video is not at fault. It
        goes straight back on the queue, its lease released and its attempt
        counter untouched, ready for whenever the breaker closes again.
        """
        with self.begin() as conn:
            conn.execute(
                text(
                    "UPDATE videos SET claimed_by = NULL, lease_expires_at = NULL, "
                    "next_attempt_at = NULL WHERE video_id = :vid"
                ),
                {"vid": video_id},
            )
            self._insert_attempt(
                conn,
                AttemptWrite(
                    video_id=video_id,
                    phase=Phase.TRANSCRIPT,
                    outcome=Outcome.BLOCKED,
                    run_id=run_id,
                    worker=worker,
                    error_type=error_type,
                    error_message=error_message,
                    http_status=http_status,
                ),
            )

    @with_deadlock_retry()
    def set_available_transcripts(self, video_id: str, available: object) -> None:
        """Store the transcript inventory even when nothing gets downloaded.

        This column is what later distinguishes "captions are off" from "captions
        exist but not in a language you asked for". Without it, both look like a
        video with no transcript and you cannot tell which are worth revisiting.
        """
        with self.begin() as conn:
            conn.execute(
                text(
                    "UPDATE videos SET available_transcripts_json = "
                    "CAST(:available AS JSON) WHERE video_id = :vid"
                ),
                {"available": _json_dump(available), "vid": video_id},
            )

    @staticmethod
    def _locked_status(conn: Connection, video_id: str) -> Status | None:
        value = conn.execute(
            text("SELECT status FROM videos WHERE video_id = :vid FOR UPDATE"),
            {"vid": video_id},
        ).scalar_one_or_none()
        return None if value is None else Status(value)

    # -- rechecks and retries ---------------------------------------------- #

    @with_deadlock_retry()
    def promote_due_rechecks(self, *, channel_id: str | None = None) -> int:
        """Requeue ``no_transcript`` / ``lang_missing`` videos whose recheck is due."""
        for source in RECHECKABLE:
            assert_transition("<bulk>", source, Status.METADATA_OK)
        sql = (
            "UPDATE videos SET status = 'metadata_ok', "
            "status_reason = 'recheck: captions may have appeared', "
            "recheck_count = recheck_count + 1, recheck_after = NULL, "
            "next_attempt_at = NULL "
            "WHERE status IN ('no_transcript','lang_missing') "
            "AND recheck_after IS NOT NULL AND recheck_after <= UTC_TIMESTAMP(6) "
            "AND recheck_count < :max_rechecks"
        )
        params: dict[str, Any] = {"max_rechecks": MAX_RECHECKS}
        if channel_id:
            sql += " AND channel_id = :cid"
            params["cid"] = channel_id
        with self.begin() as conn:
            result = conn.execute(text(sql), params)
        return int(result.rowcount or 0)

    @with_deadlock_retry()
    def reopen(
        self,
        *,
        statuses: Iterable[Status],
        channel_id: str | None = None,
        reset_attempts: bool = True,
        reason: str = "reopened by operator",
    ) -> int:
        """Force statuses back to ``metadata_ok``. Backs ``--retry-failed`` etc."""
        sources = list(statuses)
        if not sources:
            return 0
        for source in sources:
            assert_transition("<bulk>", source, Status.METADATA_OK)
        sql = (
            "UPDATE videos SET status = 'metadata_ok', status_reason = :reason, "
            + ("attempts = 0, " if reset_attempts else "")
            + "recheck_after = NULL, next_attempt_at = NULL, "
            "claimed_by = NULL, lease_expires_at = NULL "
            "WHERE status IN :statuses"
        )
        params: dict[str, Any] = {
            "reason": reason[:255],
            "statuses": [s.value for s in sources],
        }
        if channel_id:
            sql += " AND channel_id = :cid"
            params["cid"] = channel_id
        with self.begin() as conn:
            result = conn.execute(_in_clause(sql, "statuses"), params)
        return int(result.rowcount or 0)

    @with_deadlock_retry()
    def reopen_video(self, video_id: str, *, reason: str = "manual refetch") -> bool:
        """Reset one video so the next fetch pass picks it up again."""
        with self.begin() as conn:
            current = self._locked_status(conn, video_id)
            if current is None:
                return False
            assert_transition(video_id, current, Status.METADATA_OK)
            conn.execute(
                text(
                    "UPDATE videos SET status = 'metadata_ok', status_reason = :reason, "
                    "attempts = 0, recheck_count = 0, recheck_after = NULL, "
                    "next_attempt_at = NULL, claimed_by = NULL, lease_expires_at = NULL "
                    "WHERE video_id = :vid"
                ),
                {"reason": reason[:255], "vid": video_id},
            )
        return True

    @with_deadlock_retry()
    def unskip_matured_upcoming(self) -> int:
        """An ``upcoming`` video whose date has passed deserves another look."""
        assert_transition("<bulk>", Status.SKIPPED, Status.DISCOVERED)
        with self.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE videos SET status = 'discovered', "
                    "status_reason = 'scheduled date passed; re-evaluating' "
                    "WHERE status = 'skipped' "
                    "AND live_broadcast_content = 'upcoming' "
                    "AND published_at IS NOT NULL "
                    "AND published_at <= UTC_TIMESTAMP(6)"
                )
            )
        return int(result.rowcount or 0)

    # -- reads for the UI -------------------------------------------------- #

    def get_video(self, video_id: str) -> VideoRow | None:
        with self.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM videos WHERE video_id = :vid"), {"vid": video_id}
            ).mappings().one_or_none()
        return None if row is None else VideoRow.from_row(row)

    def list_videos(
        self,
        *,
        channel_id: str | None = None,
        status: str | None = None,
        query: str | None = None,
        sort: str = "published_desc",
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[VideoRow], int]:
        """Paginated video listing. Returns ``(rows, total_matching)``."""
        where: list[str] = ["1=1"]
        params: dict[str, Any] = {}
        if channel_id:
            where.append("channel_id = :cid")
            params["cid"] = channel_id
        if status:
            where.append("status = :status")
            params["status"] = status
        if query:
            where.append("(title LIKE :q OR video_id = :exact)")
            params["q"] = f"%{query}%"
            params["exact"] = query

        order = {
            "published_desc": "published_at DESC",
            "published_asc": "published_at ASC",
            "duration_desc": "duration_seconds DESC",
            "duration_asc": "duration_seconds ASC",
            "views_desc": "view_count DESC",
            "views_asc": "view_count ASC",
            "title_asc": "title ASC",
        }.get(sort, "published_at DESC")

        clause = " AND ".join(where)
        page = max(1, page)
        per_page = min(500, max(1, per_page))
        params["limit"] = per_page
        params["offset"] = (page - 1) * per_page

        with self.connect() as conn:
            total = int(
                conn.execute(
                    text(f"SELECT COUNT(*) FROM videos WHERE {clause}"), params
                ).scalar_one()
            )
            rows = conn.execute(
                text(
                    f"SELECT * FROM videos WHERE {clause} "
                    f"ORDER BY {order}, video_id LIMIT :limit OFFSET :offset"
                ),
                params,
            ).mappings().all()
        return [VideoRow.from_row(r) for r in rows], total

    def list_transcripts(self, video_id: str) -> list[TranscriptRow]:
        with self.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, video_id, language_code, kind, is_preferred, "
                    "segment_count, char_count, word_count, covered_seconds, "
                    "raw_path, raw_sha256, source, fetched_at "
                    "FROM transcripts WHERE video_id = :vid "
                    "ORDER BY is_preferred DESC, language_code, kind"
                ),
                {"vid": video_id},
            ).mappings().all()
        return [TranscriptRow.from_row(r) for r in rows]

    def get_transcript(
        self, video_id: str, *, language: str | None = None, kind: str | None = None
    ) -> TranscriptRow | None:
        sql = "SELECT * FROM transcripts WHERE video_id = :vid"
        params: dict[str, Any] = {"vid": video_id}
        if language:
            sql += " AND language_code = :lang"
            params["lang"] = language
        if kind:
            sql += " AND kind = :kind"
            params["kind"] = kind
        sql += " ORDER BY is_preferred DESC, id LIMIT 1"
        with self.connect() as conn:
            row = conn.execute(text(sql), params).mappings().one_or_none()
        return None if row is None else TranscriptRow.from_row(row)

    def search_transcripts(
        self, query: str, *, limit: int = 50, use_fulltext: bool = True
    ) -> list[dict[str, Any]]:
        """Search transcript text.

        Prefers MySQL FULLTEXT. Falls back to ``LIKE`` when the index has been
        dropped for a bulk load, so search degrades in speed rather than
        vanishing. Note that ``innodb_ft_min_token_size`` (default 3) makes
        shorter words invisible to the FULLTEXT path.
        """
        if not query.strip():
            return []
        if use_fulltext:
            sql = """
            SELECT t.video_id, v.title, v.channel_id, t.language_code, t.kind,
                   MATCH(t.plaintext) AGAINST (:q IN NATURAL LANGUAGE MODE) AS score,
                   SUBSTRING(t.plaintext, GREATEST(1,
                     LOCATE(:raw, t.plaintext) - 120), 320) AS snippet
              FROM transcripts t JOIN videos v ON v.video_id = t.video_id
             WHERE MATCH(t.plaintext) AGAINST (:q IN NATURAL LANGUAGE MODE)
             ORDER BY score DESC LIMIT :limit
            """
        else:
            sql = """
            SELECT t.video_id, v.title, v.channel_id, t.language_code, t.kind,
                   0 AS score,
                   SUBSTRING(t.plaintext, GREATEST(1,
                     LOCATE(:raw, t.plaintext) - 120), 320) AS snippet
              FROM transcripts t JOIN videos v ON v.video_id = t.video_id
             WHERE t.plaintext LIKE :like LIMIT :limit
            """
        params = {
            "q": query,
            "raw": query,
            "like": f"%{query}%",
            "limit": min(500, max(1, limit)),
        }
        with self.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    # -- stats ------------------------------------------------------------- #

    def status_counts(self, *, channel_id: str | None = None) -> dict[str, int]:
        sql = "SELECT status, COUNT(*) AS n FROM videos"
        params: dict[str, Any] = {}
        if channel_id:
            sql += " WHERE channel_id = :cid"
            params["cid"] = channel_id
        sql += " GROUP BY status"
        with self.connect() as conn:
            rows = conn.execute(text(sql), params).all()
        return {str(r[0]): int(r[1]) for r in rows}

    def channel_stats(self) -> list[ChannelStats]:
        """Per-channel counts in two queries, not one per channel."""
        with self.connect() as conn:
            channels = conn.execute(
                text(
                    "SELECT channel_id, handle, title, input_ref, is_enabled, "
                    "enumeration_complete, reported_video_count, last_enumerated_at "
                    "FROM channels ORDER BY COALESCE(handle, title, channel_id)"
                )
            ).mappings().all()
            counts = conn.execute(
                text(
                    "SELECT channel_id, status, COUNT(*) AS n FROM videos "
                    "GROUP BY channel_id, status"
                )
            ).all()

        grouped: dict[str, dict[str, int]] = {}
        for channel_id, status, n in counts:
            grouped.setdefault(str(channel_id), {})[str(status)] = int(n)

        return [
            ChannelStats(
                channel_id=c["channel_id"],
                handle=c["handle"],
                title=c["title"],
                input_ref=c["input_ref"],
                is_enabled=bool(c["is_enabled"]),
                enumeration_complete=bool(c["enumeration_complete"]),
                reported_video_count=c["reported_video_count"],
                last_enumerated_at=as_utc(c["last_enumerated_at"]),
                counts=grouped.get(c["channel_id"], {}),
            )
            for c in channels
        ]

    def stats(self) -> Stats:
        by_status = self.status_counts()
        with self.connect() as conn:
            completed = int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM fetch_attempts "
                        "WHERE outcome = 'ok' AND phase = 'transcript' "
                        "AND started_at >= UTC_TIMESTAMP(6) - INTERVAL 5 MINUTE"
                    )
                ).scalar_one()
            )
            needs_audio = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM videos WHERE needs_audio = 1")
                ).scalar_one()
            )
            transcripts = int(
                conn.execute(text("SELECT COUNT(*) FROM transcripts")).scalar_one()
            )

        total = sum(by_status.values())
        remaining = sum(
            by_status.get(s.value, 0)
            for s in (Status.DISCOVERED, Status.METADATA_OK, Status.RETRY)
        )
        day = quota_day()
        return Stats(
            by_status=by_status,
            total=total,
            remaining=remaining,
            completed_last_5m=completed,
            needs_audio=needs_audio,
            transcripts=transcripts,
            active_run=self.active_run(),
            quota_used=self.quota_used(day),
            quota_day=day,
        )

    # -- runs -------------------------------------------------------------- #

    @with_deadlock_retry()
    def create_run(
        self,
        command: str,
        *,
        args: object = None,
        pid: int | None = None,
        log_path: str | None = None,
    ) -> int:
        with self.begin() as conn:
            result = conn.execute(
                text(
                    "INSERT INTO runs (command, args_json, pid, host, log_path, "
                    "started_at, heartbeat_at) VALUES (:command, CAST(:args AS JSON), "
                    ":pid, :host, :log_path, UTC_TIMESTAMP(6), UTC_TIMESTAMP(6))"
                ),
                {
                    "command": command,
                    "args": _json_dump(args),
                    "pid": pid if pid is not None else os.getpid(),
                    "host": socket.gethostname()[:128],
                    "log_path": log_path,
                },
            )
            return int(cast(int, result.lastrowid))

    @with_deadlock_retry()
    def set_run_pid(self, run_id: int, pid: int, log_path: str | None = None) -> None:
        with self.begin() as conn:
            conn.execute(
                text(
                    "UPDATE runs SET pid = :pid, "
                    "log_path = COALESCE(:log_path, log_path) WHERE id = :id"
                ),
                {"pid": pid, "log_path": log_path, "id": run_id},
            )

    @with_deadlock_retry()
    def heartbeat(self, run_id: int, counts: object = None) -> None:
        with self.begin() as conn:
            conn.execute(
                text(
                    "UPDATE runs SET heartbeat_at = UTC_TIMESTAMP(6), "
                    "counts_json = COALESCE(CAST(:counts AS JSON), counts_json) "
                    "WHERE id = :id"
                ),
                {"counts": _json_dump(counts), "id": run_id},
            )

    @with_deadlock_retry()
    def finish_run(
        self, run_id: int, *, exit_reason: ExitReason, counts: object = None
    ) -> None:
        with self.begin() as conn:
            conn.execute(
                text(
                    "UPDATE runs SET finished_at = UTC_TIMESTAMP(6), "
                    "exit_reason = :reason, "
                    "counts_json = COALESCE(CAST(:counts AS JSON), counts_json) "
                    "WHERE id = :id AND finished_at IS NULL"
                ),
                {
                    "reason": exit_reason.value,
                    "counts": _json_dump(counts),
                    "id": run_id,
                },
            )

    def get_run(self, run_id: int) -> RunRow | None:
        with self.connect() as conn:
            row = conn.execute(
                text("SELECT * FROM runs WHERE id = :id"), {"id": run_id}
            ).mappings().one_or_none()
        return None if row is None else RunRow.from_row(row)

    def list_runs(self, *, limit: int = 50) -> list[RunRow]:
        with self.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM runs ORDER BY id DESC LIMIT :limit"),
                {"limit": limit},
            ).mappings().all()
        return [RunRow.from_row(r) for r in rows]

    def active_run(self) -> RunRow | None:
        with self.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT * FROM runs WHERE finished_at IS NULL "
                    "ORDER BY id DESC LIMIT 1"
                )
            ).mappings().one_or_none()
        return None if row is None else RunRow.from_row(row)

    def active_runs(self) -> list[RunRow]:
        with self.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM runs WHERE finished_at IS NULL ORDER BY id")
            ).mappings().all()
        return [RunRow.from_row(r) for r in rows]

    # -- attempts ---------------------------------------------------------- #

    @with_deadlock_retry()
    def record_attempt(self, attempt: AttemptWrite) -> None:
        with self.begin() as conn:
            self._insert_attempt(conn, attempt)

    @staticmethod
    def _insert_attempt(conn: Connection, attempt: AttemptWrite) -> None:
        """Insert one attempt row. Always inside the caller's transaction."""
        started = "UTC_TIMESTAMP(6)"
        if attempt.duration_seconds:
            started = (
                "UTC_TIMESTAMP(6) - INTERVAL :duration_us MICROSECOND"
            )
        conn.execute(
            text(
                f"""
                INSERT INTO fetch_attempts
                  (run_id, video_id, phase, started_at, finished_at, outcome,
                   error_type, error_message, http_status, traceback, worker)
                VALUES
                  (:run_id, :video_id, :phase, {started}, UTC_TIMESTAMP(6), :outcome,
                   :error_type, :error_message, :http_status, :traceback, :worker)
                """
            ),
            {
                "run_id": attempt.run_id,
                "video_id": attempt.video_id,
                "phase": attempt.phase.value,
                "outcome": attempt.outcome.value,
                "error_type": (
                    None if attempt.error_type is None else attempt.error_type[:128]
                ),
                "error_message": (
                    None
                    if attempt.error_message is None
                    else attempt.error_message[:1024]
                ),
                "http_status": attempt.http_status,
                "traceback": attempt.traceback,
                "worker": None if attempt.worker is None else attempt.worker[:64],
                "duration_us": int((attempt.duration_seconds or 0) * 1_000_000),
            },
        )

    def recent_attempts(self, video_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT run_id, phase, started_at, finished_at, outcome, "
                    "error_type, error_message, http_status, worker "
                    "FROM fetch_attempts WHERE video_id = :vid "
                    "ORDER BY id DESC LIMIT :limit"
                ),
                {"vid": video_id, "limit": limit},
            ).mappings().all()
        return [dict(r) for r in rows]

    @with_deadlock_retry()
    def prune_attempts(self, older_than_days: int) -> int:
        """``fetch_attempts`` grows by one row per attempt per video.

        Deleted in bounded chunks so a months-old backlog does not hold a
        multi-million-row lock and stall every worker.
        """
        deleted = 0
        while True:
            with self.begin() as conn:
                result = conn.execute(
                    text(
                        "DELETE FROM fetch_attempts "
                        "WHERE started_at < UTC_TIMESTAMP(6) - INTERVAL :days DAY "
                        "LIMIT 10000"
                    ),
                    {"days": older_than_days},
                )
            count = int(result.rowcount or 0)
            deleted += count
            if count < 10000:
                return deleted

    # -- quota ------------------------------------------------------------- #

    def quota_used(self, day: date | None = None) -> int:
        target = day or quota_day()
        with self.connect() as conn:
            value = conn.execute(
                text("SELECT units_used FROM api_quota WHERE day = :day"),
                {"day": target},
            ).scalar_one_or_none()
        return int(value or 0)

    @with_deadlock_retry()
    def add_quota(self, units: int, *, day: date | None = None) -> int:
        """Charge units against today's Pacific-time budget; return the new total."""
        target = day or quota_day()
        with self.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO api_quota (day, units_used) VALUES (:day, :units) "
                    "AS new ON DUPLICATE KEY UPDATE "
                    "units_used = api_quota.units_used + new.units_used"
                ),
                {"day": target, "units": units},
            )
            value = conn.execute(
                text("SELECT units_used FROM api_quota WHERE day = :day"),
                {"day": target},
            ).scalar_one()
        return int(value)

    # -- phase 2 hook ------------------------------------------------------ #

    def audio_queue(self, *, limit: int | None = None) -> list[VideoRow]:
        """Videos awaiting an audio pass, shortest first.

        ``duration_seconds`` is already stored, so a consumer can estimate GPU
        time and hard-skip anything over threshold before downloading a byte.
        """
        sql = (
            "SELECT * FROM videos WHERE needs_audio = 1 "
            "AND status IN ('no_transcript','lang_missing') "
            "ORDER BY duration_seconds ASC"
        )
        params: dict[str, Any] = {}
        if limit:
            sql += " LIMIT :limit"
            params["limit"] = limit
        with self.connect() as conn:
            rows = conn.execute(text(sql), params).mappings().all()
        return [VideoRow.from_row(r) for r in rows]

    # -- doctor ------------------------------------------------------------ #

    def all_transcript_paths(self) -> list[tuple[str, str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                text("SELECT video_id, raw_path, raw_sha256 FROM transcripts")
            ).all()
        return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]

    @with_deadlock_retry()
    def delete_transcript_rows(self, ids: Sequence[int]) -> int:
        if not ids:
            return 0
        with self.begin() as conn:
            result = conn.execute(
                _in_clause("DELETE FROM transcripts WHERE id IN :ids", "ids"),
                {"ids": list(ids)},
            )
        return int(result.rowcount or 0)

    def transcript_rows_with_missing_files(
        self, project_root: Path | None = None
    ) -> list[tuple[int, str, str]]:
        """Rows whose gzip file is gone: the corruption direction that matters.

        The other direction - a file with no row - is harmless and reclaimable,
        which is why writes fsync the file *before* committing the row.
        """
        base = project_root or Path.cwd()
        with self.connect() as conn:
            rows = conn.execute(
                text("SELECT id, video_id, raw_path FROM transcripts")
            ).all()
        missing: list[tuple[int, str, str]] = []
        for row_id, video_id, raw_path in rows:
            path = Path(str(raw_path))
            resolved = path if path.is_absolute() else base / path
            if not resolved.exists():
                missing.append((int(row_id), str(video_id), str(raw_path)))
        return missing

    @with_deadlock_retry()
    def mark_orphan_runs_crashed(self, alive: set[int]) -> list[int]:
        """Close out run rows whose process is gone.

        Without this the UI shows a phantom "RUNNING" forever after a machine
        reboot or an OOM kill, and the operator has no way to start a new run.
        """
        crashed: list[int] = []
        for run in self.active_runs():
            if run.pid is not None and run.pid in alive:
                continue
            self.finish_run(run.id, exit_reason=ExitReason.CRASHED)
            crashed.append(run.id)
        return crashed
