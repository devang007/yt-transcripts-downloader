"""Metadata hydration: ``discovered`` -> ``metadata_ok`` or ``skipped``.

Batches 50 ids per ``videos.list`` call, which costs one quota unit for all 50 -
the single best deal in the Data API. Everything derived here (duration, Short
vs long-form, whether it was ever a livestream) is what the skip rules and the
later audio pass depend on, so it is worth getting right once.

Ids the API declines to return are the interesting case: a deleted or private
video simply does not appear in the response. Diffing the request against the
response is the only way to notice, and those rows become ``unavailable`` rather
than sitting in ``discovered`` forever being re-requested every run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from .classify import HardBlock, QuotaExhausted, classify
from .logs import context, get_logger
from .repo import MetadataUpdate, Repo
from .states import Status
from .youtube_api import (
    MAX_IDS_PER_VIDEOS_CALL,
    SHORTS_MAX_SECONDS,
    VideoDetails,
    YouTubeAPI,
    chunked,
    parse_iso8601_duration,
    parse_rfc3339,
)

log = get_logger(__name__)

# Statuses that mean the video is gone rather than merely unhelpful.
DEAD_PRIVACY: frozenset[str] = frozenset({"private"})
DEAD_UPLOAD: frozenset[str] = frozenset({"deleted", "failed", "rejected"})


@dataclass
class HydrateResult:
    requested: int = 0
    hydrated: int = 0
    skipped: int = 0
    unavailable: int = 0
    deferred: int = 0
    calls: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


@dataclass
class MetadataBatch:
    """Outcome of hydrating one batch, split by *why* an id has no details.

    The split is the whole point. "The API did not return this id" means the
    video is gone; "yt-dlp was refused by a bot check" means nothing about the
    video at all. Collapsing the two marks perfectly good videos ``unavailable``
    - a terminal status that is never revisited - every time an IP gets blocked.
    """

    details: list[VideoDetails] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    """Genuinely gone: deleted, private, terminated."""

    deferred: list[str] = field(default_factory=list)
    """Failed for a reason that says nothing about the video. Left queued."""


@dataclass(frozen=True, slots=True)
class SkipRules:
    """Everything that decides a video is not worth fetching a transcript for."""

    max_duration_seconds: int = 43200
    include_shorts: bool = True
    include_streams: bool = False

    def evaluate(self, details: VideoDetails) -> tuple[Status, str | None]:
        """Return the status this video should land in, and why.

        Order matters. "Gone" beats "skipped", and a live or upcoming broadcast
        is checked before duration because its duration is meaningless.
        """
        if details.privacy_status in DEAD_PRIVACY:
            return Status.UNAVAILABLE, f"privacy status {details.privacy_status}"
        if details.upload_status in DEAD_UPLOAD:
            return Status.UNAVAILABLE, f"upload status {details.upload_status}"

        if details.live_broadcast_content == "upcoming":
            # Re-evaluated by repo.unskip_matured_upcoming once the date passes.
            return Status.SKIPPED, "premiere or stream has not happened yet"
        if details.live_broadcast_content == "live":
            return Status.SKIPPED, "currently live; captions are not final"

        duration = details.duration_seconds
        if duration is not None and duration > self.max_duration_seconds:
            return (
                Status.SKIPPED,
                f"duration {duration}s exceeds limit {self.max_duration_seconds}s",
            )
        if duration == 0 and details.was_livestream:
            return Status.SKIPPED, "livestream with no recorded duration"

        is_short = details.is_short_guess
        if is_short and not self.include_shorts:
            return Status.SKIPPED, "Shorts are excluded by configuration"
        if details.was_livestream and not self.include_streams:
            return Status.SKIPPED, "livestreams are excluded by configuration"

        return Status.METADATA_OK, None


def to_update(details: VideoDetails, rules: SkipRules) -> MetadataUpdate:
    """Turn API details plus skip rules into one row update."""
    status, reason = rules.evaluate(details)
    return MetadataUpdate(
        video_id=details.video_id,
        title=details.title,
        description=details.description,
        published_at=details.published_at,
        duration_seconds=details.duration_seconds,
        view_count=details.view_count,
        like_count=details.like_count,
        comment_count=details.comment_count,
        tags=details.tags,
        category_id=details.category_id,
        default_language=details.default_language,
        default_audio_language=details.default_audio_language,
        live_broadcast_content=details.live_broadcast_content,
        is_short=details.is_short_guess,
        was_livestream=details.was_livestream,
        thumbnail_url=details.thumbnail_url,
        status=status,
        status_reason=reason,
    )


def hydrate(
    repo: Repo,
    *,
    api: YouTubeAPI | None,
    rules: SkipRules,
    channel_id: str | None = None,
    limit: int | None = None,
    cookies_file: str | None = None,
    proxy: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> HydrateResult:
    """Hydrate ``discovered`` videos in batches of 50.

    Args:
        api: Data API client, or ``None`` to hydrate with yt-dlp (far more
            expensive: one request per video instead of per fifty).
        limit: Stop after this many videos.
        should_stop: Polled between batches.
    """
    result = HydrateResult()
    remaining = limit
    # Ids deferred this pass. Excluded from later queries so the loop cannot spin
    # on the same rows forever: they stay in `discovered` by design.
    deferred: set[str] = set()

    while True:
        if should_stop is not None and should_stop():
            break
        batch_size = MAX_IDS_PER_VIDEOS_CALL
        if remaining is not None:
            if remaining <= 0:
                break
            batch_size = min(batch_size, remaining)

        ids = repo.videos_needing_metadata(
            channel_id=channel_id, limit=batch_size, exclude=deferred
        )
        if not ids:
            break

        result.requested += len(ids)
        if remaining is not None:
            remaining -= len(ids)

        if api is not None:
            batch = _details_via_api(api, ids)
            result.calls += 1
        else:
            batch = _details_via_ytdlp(ids, cookies_file=cookies_file, proxy=proxy)
            result.calls += len(ids)

        updates = [to_update(d, rules) for d in batch.details]
        if updates:
            repo.apply_metadata(updates)
            for update in updates:
                if update.status is Status.SKIPPED:
                    result.skipped += 1
                    result.note(update.status_reason or "skipped")
                elif update.status is Status.UNAVAILABLE:
                    result.unavailable += 1
                    result.note(update.status_reason or "unavailable")
                else:
                    result.hydrated += 1

        if batch.unavailable:
            repo.mark_videos_unavailable(
                batch.unavailable,
                "not returned by the API: deleted, private or region-locked",
            )
            result.unavailable += len(batch.unavailable)
            result.note("not returned by the API")
            log.info("videos are gone", count=len(batch.unavailable))

        if batch.deferred:
            deferred.update(batch.deferred)
            result.deferred += len(batch.deferred)
            result.note("deferred: transient failure, left queued")
            log.warning(
                "deferred videos after a transient failure",
                count=len(batch.deferred),
                detail="left in `discovered` rather than marked unavailable",
            )

        log.info(
            "hydrated batch",
            requested=len(ids),
            hydrated=result.hydrated,
            skipped=result.skipped,
            unavailable=result.unavailable,
            deferred=result.deferred,
        )

    return result


def _details_via_api(api: YouTubeAPI, ids: Sequence[str]) -> MetadataBatch:
    """One ``videos.list`` call, with a guard against systemic absence.

    An id the Data API omits really is gone - it returns 200 with the surviving
    ids alongside. But *every* id missing from a multi-id batch is not fifty
    simultaneous deletions; it is an auth, quota or connectivity problem. Marking
    them all ``unavailable`` would permanently retire a whole channel over a
    transient fault, so that case raises instead.
    """
    details = api.video_details(ids)
    returned = {d.video_id for d in details}
    missing = [v for v in ids if v not in returned]

    if len(ids) > 1 and not returned:
        raise HardBlock(
            f"videos.list returned nothing for a batch of {len(ids)} ids. "
            "That is a systemic failure (key, quota or network), not "
            f"{len(ids)} deleted videos, so none of them are being marked "
            "unavailable."
        )
    return MetadataBatch(details=details, unavailable=missing)


def _details_via_ytdlp(
    ids: Sequence[str], *, cookies_file: str | None, proxy: str | None
) -> MetadataBatch:
    """Per-video metadata without an API key.

    One network request per video, versus one per fifty through the Data API.
    Fine for a few hundred videos, painful for tens of thousands - get a key.

    Every failure is put through :func:`~yt_tx.classify.classify` rather than
    being swallowed. This used to ``continue`` on any ``DownloadError``, which
    left the id absent from the result and got it marked ``unavailable`` by the
    caller. A single bot check therefore retired every video it touched, with no
    way to tell them from genuinely deleted ones. A block now aborts the batch.

    Raises:
        HardBlock: YouTube is refusing us; nothing is concluded about any video.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    from .discover import ytdlp_options

    options = ytdlp_options(cookies_file=cookies_file, proxy=proxy)
    options["extract_flat"] = False

    batch = MetadataBatch()
    with YoutubeDL(options) as ydl:
        for video_id in ids:
            with context(video_id=video_id):
                try:
                    info = ydl.extract_info(
                        f"https://www.youtube.com/watch?v={video_id}", download=False
                    )
                except DownloadError as exc:
                    verdict = classify(exc, cookies_configured=bool(cookies_file))
                    if verdict.is_block:
                        # Stop immediately: continuing would spend the rest of
                        # the batch confirming the same block, and every id
                        # already deferred stays queued.
                        log.warning(
                            "yt-dlp is blocked; abandoning the batch",
                            reason=verdict.reason, error_type=verdict.error_type,
                        )
                        raise HardBlock(verdict.reason) from exc
                    if verdict.status is Status.UNAVAILABLE:
                        log.info("video is gone", reason=verdict.reason)
                        batch.unavailable.append(video_id)
                    else:
                        log.warning(
                            "deferring video after a transient yt-dlp failure",
                            reason=verdict.reason, error_type=verdict.error_type,
                        )
                        batch.deferred.append(video_id)
                    continue
                if not isinstance(info, dict):
                    batch.deferred.append(video_id)
                    continue
                batch.details.append(
                    _details_from_ytdlp(cast("dict[str, Any]", info), video_id)
                )
    return batch


def _details_from_ytdlp(info: dict[str, Any], video_id: str) -> VideoDetails:
    """Map yt-dlp's info dict onto the same shape the Data API produces."""
    duration = info.get("duration")
    live_status = info.get("live_status")
    broadcast = {
        "is_live": "live",
        "is_upcoming": "upcoming",
        "was_live": "none",
        "not_live": "none",
        "post_live": "none",
    }.get(str(live_status), "none")

    upload_date = info.get("upload_date")
    published = None
    if isinstance(upload_date, str) and len(upload_date) == 8:
        published = parse_rfc3339(
            f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}T00:00:00Z"
        )
    timestamp = info.get("timestamp")
    if isinstance(timestamp, (int, float)):
        from datetime import datetime, timezone

        published = datetime.fromtimestamp(float(timestamp), timezone.utc).replace(
            tzinfo=None
        )

    tags = info.get("tags")
    return VideoDetails(
        video_id=video_id,
        title=str(info["title"])[:1024] if info.get("title") else None,
        description=info.get("description") or None,
        published_at=published,
        duration_seconds=(
            int(duration) if isinstance(duration, (int, float)) else None
        ),
        view_count=_int_or_none(info.get("view_count")),
        like_count=_int_or_none(info.get("like_count")),
        comment_count=_int_or_none(info.get("comment_count")),
        tags=[str(t) for t in tags] if isinstance(tags, list) else None,
        category_id=None,
        default_language=(
            str(info["language"])[:16] if isinstance(info.get("language"), str) else None
        ),
        default_audio_language=None,
        live_broadcast_content=broadcast,
        was_livestream=bool(info.get("was_live")) or live_status == "was_live",
        thumbnail_url=(
            str(info["thumbnail"])[:512]
            if isinstance(info.get("thumbnail"), str)
            else None
        ),
        privacy_status=(
            "private" if info.get("availability") == "private" else None
        ),
        upload_status=None,
    )


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


__all__ = [
    "HydrateResult",
    "SkipRules",
    "hydrate",
    "to_update",
    "MAX_IDS_PER_VIDEOS_CALL",
    "SHORTS_MAX_SECONDS",
    "QuotaExhausted",
    "parse_iso8601_duration",
    "chunked",
]
