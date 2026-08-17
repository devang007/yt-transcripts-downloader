"""The video state machine.

``videos.status`` is the single source of truth for what work remains. This
module holds the vocabulary and the legal transitions; :mod:`yt_tx.repo`
enforces them on every write and :mod:`yt_tx.classify` chooses them from
exceptions.

The table in the module docstring of :func:`is_reprocessed` mirrors the spec's
state table, and :data:`LEGAL_TRANSITIONS` is asserted against it in tests.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Status(StrEnum):
    """Values of the ``videos.status`` ENUM, in ENUM declaration order.

    Order matters: adding a value requires appending to the end of the MySQL
    ENUM (in-place in 8.0) rather than inserting in the middle (full rewrite).
    Keep this class and the DDL in the same order.
    """

    DISCOVERED = "discovered"
    METADATA_OK = "metadata_ok"
    TRANSCRIPT_OK = "transcript_ok"
    NO_TRANSCRIPT = "no_transcript"
    LANG_MISSING = "lang_missing"
    UNAVAILABLE = "unavailable"
    AGE_RESTRICTED = "age_restricted"
    SKIPPED = "skipped"
    RETRY = "retry"
    FAILED = "failed"


class Outcome(StrEnum):
    """Values of the ``fetch_attempts.outcome`` ENUM.

    ``BLOCKED`` is the load-bearing one. It means *the fetcher* is blocked, not
    that *this video* has a problem, so it must never change a video's status.
    Marking videos failed during an IP block poisons thousands of rows in one
    run with no way to tell them apart from genuinely caption-less videos.
    """

    OK = "ok"
    RETRYABLE = "retryable"
    TERMINAL = "terminal"
    BLOCKED = "blocked"


class Phase(StrEnum):
    METADATA = "metadata"
    TRANSCRIPT = "transcript"
    AUDIO = "audio"


class ExitReason(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CIRCUIT_OPEN = "circuit_open"
    QUOTA_EXHAUSTED = "quota_exhausted"
    CRASHED = "crashed"
    STOPPED = "stopped"


class DesiredState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"


class TranscriptKind(StrEnum):
    MANUAL = "manual"
    ASR = "asr"
    TRANSLATED = "translated"
    WHISPER = "whisper"


# Statuses that the transcript stage will claim.
CLAIMABLE: Final[frozenset[Status]] = frozenset({Status.METADATA_OK, Status.RETRY})

# What the transcript stage claims with --skip-hydrate: metadata is not a
# precondition for captions. `fetch_video` reads only `video_id`, `channel_id`
# and `attempts` off the row, so hydration buys titles, durations and the skip
# rules - not the transcript itself. Without an API key hydration costs one
# request per video, which is the whole runtime of a large harvest.
CLAIMABLE_UNHYDRATED: Final[frozenset[Status]] = CLAIMABLE | {Status.DISCOVERED}

# Statuses that the hydrate stage will pick up.
HYDRATABLE: Final[frozenset[Status]] = frozenset({Status.DISCOVERED})

# Reached a conclusion; never revisited without an explicit operator flag.
TERMINAL: Final[frozenset[Status]] = frozenset(
    {
        Status.TRANSCRIPT_OK,
        Status.NO_TRANSCRIPT,
        Status.LANG_MISSING,
        Status.UNAVAILABLE,
        Status.AGE_RESTRICTED,
        Status.SKIPPED,
        Status.FAILED,
    }
)

# Counts as "done" for coverage percentages: we have the text.
SUCCESS: Final[frozenset[Status]] = frozenset({Status.TRANSCRIPT_OK})

# Eligible for a later audio pass. Only genuinely text-less videos qualify:
# a lang_missing video has captions, just not in a configured language, and the
# fix for that is translation rather than ASR.
NEEDS_AUDIO_STATUSES: Final[frozenset[Status]] = frozenset({Status.NO_TRANSCRIPT})

# Revisited when ``recheck_after`` matures, because auto-captions often show up
# hours or days after upload.
RECHECKABLE: Final[frozenset[Status]] = frozenset(
    {Status.NO_TRANSCRIPT, Status.LANG_MISSING}
)


LEGAL_TRANSITIONS: Final[dict[Status, frozenset[Status]]] = {
    # The fetch terminals are reachable directly because --skip-hydrate sends
    # `discovered` videos straight through the transcript stage, never stopping
    # at metadata_ok.
    Status.DISCOVERED: frozenset(
        {
            Status.DISCOVERED,
            Status.METADATA_OK,
            Status.SKIPPED,
            Status.UNAVAILABLE,
            Status.RETRY,
            Status.FAILED,
            Status.TRANSCRIPT_OK,
            Status.NO_TRANSCRIPT,
            Status.LANG_MISSING,
            Status.AGE_RESTRICTED,
        }
    ),
    Status.METADATA_OK: frozenset(
        {
            Status.METADATA_OK,
            Status.TRANSCRIPT_OK,
            Status.NO_TRANSCRIPT,
            Status.LANG_MISSING,
            Status.UNAVAILABLE,
            Status.AGE_RESTRICTED,
            Status.SKIPPED,
            Status.RETRY,
            Status.FAILED,
        }
    ),
    Status.RETRY: frozenset(
        {
            Status.RETRY,
            Status.METADATA_OK,
            Status.TRANSCRIPT_OK,
            Status.NO_TRANSCRIPT,
            Status.LANG_MISSING,
            Status.UNAVAILABLE,
            Status.AGE_RESTRICTED,
            Status.SKIPPED,
            Status.FAILED,
        }
    ),
    # A recheck sends no_transcript / lang_missing back through the fetch stage.
    Status.NO_TRANSCRIPT: frozenset(
        {
            Status.NO_TRANSCRIPT,
            Status.METADATA_OK,
            Status.TRANSCRIPT_OK,
            Status.LANG_MISSING,
            Status.UNAVAILABLE,
            Status.AGE_RESTRICTED,
            Status.RETRY,
            Status.FAILED,
        }
    ),
    Status.LANG_MISSING: frozenset(
        {
            Status.LANG_MISSING,
            Status.METADATA_OK,
            Status.TRANSCRIPT_OK,
            Status.NO_TRANSCRIPT,
            Status.UNAVAILABLE,
            Status.AGE_RESTRICTED,
            Status.RETRY,
            Status.FAILED,
        }
    ),
    # An `upcoming` video whose date passes becomes fetchable again.
    Status.SKIPPED: frozenset(
        {
            Status.SKIPPED,
            Status.METADATA_OK,
            Status.DISCOVERED,
            Status.UNAVAILABLE,
        }
    ),
    # --retry-failed reopens these.
    Status.FAILED: frozenset(
        {
            Status.FAILED,
            Status.METADATA_OK,
            Status.RETRY,
            Status.TRANSCRIPT_OK,
            Status.NO_TRANSCRIPT,
            Status.LANG_MISSING,
            Status.UNAVAILABLE,
            Status.AGE_RESTRICTED,
        }
    ),
    # Only reopened when cookies are configured.
    Status.AGE_RESTRICTED: frozenset(
        {
            Status.AGE_RESTRICTED,
            Status.METADATA_OK,
            Status.TRANSCRIPT_OK,
            Status.NO_TRANSCRIPT,
            Status.LANG_MISSING,
            Status.UNAVAILABLE,
            Status.RETRY,
            Status.FAILED,
        }
    ),
    # Private / deleted / region-locked is as final as it gets. A --force-recheck
    # may still re-run metadata, which is why metadata_ok is permitted.
    Status.UNAVAILABLE: frozenset({Status.UNAVAILABLE, Status.METADATA_OK}),
    # Success is absorbing. Re-fetching a specific video goes through the
    # explicit refetch path, which resets status to metadata_ok first.
    Status.TRANSCRIPT_OK: frozenset({Status.TRANSCRIPT_OK, Status.METADATA_OK}),
}


class IllegalTransition(AssertionError):
    """A status change the state machine forbids. Always a programming error."""

    def __init__(self, video_id: str, before: Status, after: Status) -> None:
        self.video_id = video_id
        self.before = before
        self.after = after
        super().__init__(
            f"illegal status transition for {video_id}: {before} -> {after}"
        )


def assert_transition(video_id: str, before: Status | str, after: Status | str) -> None:
    """Raise :class:`IllegalTransition` unless the move is legal.

    Called by :mod:`yt_tx.repo` before every status write. A violation means the
    pipeline has a logic bug, so it raises rather than logging - a silent illegal
    transition is how a video ends up permanently invisible to every stage.
    """
    src = Status(before)
    dst = Status(after)
    if dst not in LEGAL_TRANSITIONS[src]:
        raise IllegalTransition(video_id, src, dst)


def is_reprocessed(status: Status) -> bool:
    """Whether a plain re-run picks this status back up.

    ==================  ===========================================
    status              picked up again?
    ==================  ===========================================
    discovered          yes - hydrate
    metadata_ok         yes - fetch
    retry               yes, once ``next_attempt_at`` is due
    no_transcript       only when ``recheck_after`` matures
    lang_missing        only when languages change, or --force-recheck
    transcript_ok       no
    unavailable         no
    age_restricted      only with cookies configured
    skipped             only when an ``upcoming`` date passes
    failed              only with --retry-failed
    ==================  ===========================================
    """
    return status in {Status.DISCOVERED, Status.METADATA_OK, Status.RETRY}
