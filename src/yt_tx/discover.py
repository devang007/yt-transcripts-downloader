"""Channel resolution and video enumeration.

Two stages that share a concern: never redo work.

**resolve** turns whatever the user typed - a handle, a legacy ``/user/`` path, a
custom ``/c/`` vanity URL, a bare ``UC…`` id, or a full URL with tracking
parameters glued on - into a canonical channel id plus uploads playlist id. The
answer is cached in ``channels`` and never looked up twice.

**discover** paginates the uploads playlist 50 at a time and persists
``enumeration_cursor`` after **every** page. That single detail is what makes a
crash 8,000 videos into a 40,000-video channel resume where it stopped instead of
starting over. It costs one UPDATE per page.

Incremental mode reads the channel's RSS feed first: ~15 newest videos, zero
quota, no key. If nothing there is newer than ``newest_published_at``, the
channel is skipped without a single API unit spent. That is what a daily cron
should use; full pagination is for the first pass and for repair.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, cast
from urllib.parse import unquote, urlparse

from .classify import QuotaExhausted
from .logs import get_logger
from .repo import ChannelRow, Repo, VideoUpsert
from .youtube_api import (
    ChannelNotFound,
    PLAYLIST_PAGINATION_CEILING,
    ResolvedChannel,
    YouTubeAPI,
    YouTubeAPIError,
    fetch_rss_latest,
)

log = get_logger(__name__)

CHANNEL_ID_RE: Final = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
UPLOADS_FROM_CHANNEL: Final = re.compile(r"^UC")

# Tabs that hold videos the uploads playlist can omit.
TAB_URLS: Final[dict[str, str]] = {
    "videos": "https://www.youtube.com/channel/{cid}/videos",
    "shorts": "https://www.youtube.com/channel/{cid}/shorts",
    "streams": "https://www.youtube.com/channel/{cid}/streams",
}


class RefKind(StrEnum):
    CHANNEL_ID = "channel_id"
    HANDLE = "handle"
    CUSTOM = "custom"
    USER = "user"
    PLAYLIST = "playlist"


@dataclass(frozen=True, slots=True)
class ChannelRef:
    """A parsed channel reference. Pure data - parsing never touches the network."""

    kind: RefKind
    value: str
    original: str

    @property
    def is_canonical(self) -> bool:
        return self.kind is RefKind.CHANNEL_ID


class ResolveError(RuntimeError):
    """A reference could not be turned into a channel id."""


def parse_channel_ref(ref: str) -> ChannelRef:
    """Normalise any of the forms a user might paste.

    Handles ``@handle``, ``UC…``, ``/channel/UC…``, ``/c/Name``, ``/user/Name``,
    full URLs with ``/videos`` or ``/shorts`` suffixes, query strings, and
    percent-encoding. Purely syntactic: it decides *what kind* of reference this
    is, not whether it exists.

    Raises:
        ResolveError: the string is empty or unrecognisable.
    """
    raw = (ref or "").strip()
    if not raw:
        raise ResolveError("empty channel reference")

    # Bare forms first, before any URL parsing.
    if CHANNEL_ID_RE.match(raw):
        return ChannelRef(RefKind.CHANNEL_ID, raw, raw)
    if raw.startswith("@") and "/" not in raw:
        return ChannelRef(RefKind.HANDLE, raw, raw)

    candidate = raw
    if "://" not in candidate:
        if candidate.startswith(("www.", "youtube.com", "m.youtube.com", "youtu.be")):
            candidate = f"https://{candidate}"
        elif candidate.startswith("/"):
            candidate = f"https://www.youtube.com{candidate}"

    if "://" in candidate:
        parsed = urlparse(candidate)
        host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
        if host not in {"youtube.com", "youtu.be", "music.youtube.com"}:
            raise ResolveError(f"not a YouTube URL: {ref!r}")
        segments = [unquote(s) for s in parsed.path.split("/") if s]
        if not segments:
            raise ResolveError(f"no channel in URL: {ref!r}")

        head = segments[0]
        if head == "channel" and len(segments) >= 2:
            value = segments[1]
            if not CHANNEL_ID_RE.match(value):
                raise ResolveError(f"malformed channel id in {ref!r}: {value!r}")
            return ChannelRef(RefKind.CHANNEL_ID, value, raw)
        if head == "user" and len(segments) >= 2:
            return ChannelRef(RefKind.USER, segments[1], raw)
        if head == "c" and len(segments) >= 2:
            return ChannelRef(RefKind.CUSTOM, segments[1], raw)
        if head.startswith("@"):
            return ChannelRef(RefKind.HANDLE, head, raw)
        if head == "playlist":
            from urllib.parse import parse_qs

            ids = parse_qs(parsed.query).get("list")
            if ids:
                return ChannelRef(RefKind.PLAYLIST, ids[0], raw)
            raise ResolveError(f"playlist URL without a list id: {ref!r}")
        # A bare vanity path such as youtube.com/SomeName.
        if head not in {"watch", "shorts", "embed", "feed", "results"}:
            return ChannelRef(RefKind.CUSTOM, head, raw)
        raise ResolveError(f"URL points at a video, not a channel: {ref!r}")

    # No scheme, no slash: treat as a vanity/legacy name.
    return ChannelRef(RefKind.CUSTOM, raw, raw)


def uploads_playlist_for(channel_id: str) -> str:
    """``UC…`` -> ``UU…``.

    The uploads playlist id is the channel id with the second character changed
    from C to U. Documented, stable, and saves a lookup - but the API's own
    answer is preferred whenever we have it.
    """
    if not CHANNEL_ID_RE.match(channel_id):
        raise ValueError(f"not a canonical channel id: {channel_id!r}")
    return "UU" + channel_id[2:]


# --------------------------------------------------------------------------- #
# yt-dlp fallback
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FlatEntry:
    video_id: str
    title: str | None
    duration_seconds: int | None = None


def ytdlp_options(
    *, cookies_file: str | None = None, proxy: str | None = None
) -> dict[str, Any]:
    """Shared yt-dlp options. Quiet, no downloads, cookies and proxy plumbed."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": False,
        "ignoreerrors": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 30,
        "retries": 2,
        "logger": _YtDlpLogBridge(),
    }
    if cookies_file:
        options["cookiefile"] = cookies_file
    if proxy:
        options["proxy"] = proxy
    return options


class _YtDlpLogBridge:
    """Route yt-dlp's chatter into structlog instead of stdout."""

    def debug(self, message: str) -> None:
        if message.startswith("[debug] "):
            return
        log.debug("yt-dlp", message=message[:500])

    def info(self, message: str) -> None:
        log.debug("yt-dlp", message=message[:500])

    def warning(self, message: str) -> None:
        log.warning("yt-dlp", message=message[:500])

    def error(self, message: str) -> None:
        log.error("yt-dlp", message=message[:500])


def flat_playlist(
    url: str,
    *,
    limit: int | None = None,
    cookies_file: str | None = None,
    proxy: str | None = None,
) -> list[FlatEntry]:
    """Enumerate a channel tab or playlist with yt-dlp, without downloading.

    Used for the ``/shorts`` and ``/streams`` tabs, which the uploads playlist
    does not always include, and as the enumeration path when no API key is
    configured.
    """
    from yt_dlp import YoutubeDL

    options = ytdlp_options(cookies_file=cookies_file, proxy=proxy)
    if limit:
        options["playlistend"] = limit

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not isinstance(info, dict):
        return []
    entries = cast("dict[str, Any]", info).get("entries") or []
    out: list[FlatEntry] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, Any]", raw)
        video_id = entry.get("id")
        if not isinstance(video_id, str) or len(video_id) != 11:
            continue
        duration = entry.get("duration")
        out.append(
            FlatEntry(
                video_id=video_id,
                title=(
                    str(entry["title"])[:1024]
                    if isinstance(entry.get("title"), str)
                    else None
                ),
                duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
            )
        )
    return out


def resolve_with_ytdlp(ref: ChannelRef) -> ResolvedChannel:
    """Resolve a vanity or legacy reference that the Data API cannot look up.

    ``channels.list`` has ``forHandle`` and ``forUsername`` but nothing for a
    ``/c/Name`` vanity URL. The alternative would be ``search.list`` at 100 units
    a call, which is out of the question, so yt-dlp reads the channel page
    instead - free, and it works for every form.
    """
    from yt_dlp import YoutubeDL

    url = {
        RefKind.CHANNEL_ID: f"https://www.youtube.com/channel/{ref.value}",
        RefKind.HANDLE: f"https://www.youtube.com/{ref.value}",
        RefKind.CUSTOM: f"https://www.youtube.com/c/{ref.value}",
        RefKind.USER: f"https://www.youtube.com/user/{ref.value}",
        RefKind.PLAYLIST: f"https://www.youtube.com/playlist?list={ref.value}",
    }[ref.kind]

    options = ytdlp_options()
    # Only the channel metadata is wanted here, so stop after one entry rather
    # than walking the whole channel.
    options["playlistend"] = 1
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise ResolveError(f"yt-dlp could not resolve {ref.original!r}")

    data = cast("dict[str, Any]", info)
    channel_id = data.get("channel_id") or data.get("uploader_id") or data.get("id")
    if not isinstance(channel_id, str) or not CHANNEL_ID_RE.match(channel_id):
        raise ResolveError(
            f"yt-dlp returned no usable channel id for {ref.original!r} "
            f"(got {channel_id!r})"
        )
    title = data.get("channel") or data.get("uploader") or data.get("title")
    handle = data.get("uploader_id")
    return ResolvedChannel(
        channel_id=channel_id,
        title=str(title)[:512] if title else None,
        handle=(
            str(handle)[:128]
            if isinstance(handle, str) and handle.startswith("@")
            else None
        ),
        uploads_playlist_id=uploads_playlist_for(channel_id),
        # Deliberately unknown. yt-dlp's playlist_count reflects the *truncated*
        # listing when playlistend is set, so reading it here reports 1-3 videos
        # for a channel with hundreds. A null is honest; a wrong number would
        # make the truncation warning in _warn_if_truncated nonsense.
        video_count=None,
    )


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def resolve_channel(
    repo: Repo,
    ref: str,
    *,
    api: YouTubeAPI | None,
    use_cache: bool = True,
) -> ChannelRow:
    """Resolve and persist a channel reference. Cached forever after the first hit."""
    parsed = parse_channel_ref(ref)

    if use_cache:
        cached = repo.find_channel_by_ref(parsed.value) or repo.find_channel_by_ref(ref)
        if cached is not None and cached.uploads_playlist_id:
            log.debug("channel resolved from cache", channel_id=cached.channel_id)
            return cached

    resolved = _resolve_uncached(parsed, api)
    uploads = resolved.uploads_playlist_id or uploads_playlist_for(resolved.channel_id)

    repo.upsert_channel(
        channel_id=resolved.channel_id,
        input_ref=ref[:255],
        handle=resolved.handle,
        title=resolved.title,
        uploads_playlist_id=uploads,
        reported_video_count=resolved.video_count,
    )
    row = repo.get_channel(resolved.channel_id)
    if row is None:  # pragma: no cover - only if the insert vanished
        raise ResolveError(f"failed to persist channel {resolved.channel_id}")
    log.info(
        "channel resolved",
        channel_id=row.channel_id,
        handle=row.handle,
        title=row.title,
        reported_video_count=row.reported_video_count,
    )
    return row


def _resolve_uncached(
    parsed: ChannelRef, api: YouTubeAPI | None
) -> ResolvedChannel:
    if api is not None:
        try:
            if parsed.kind is RefKind.CHANNEL_ID:
                return api.resolve_channel(channel_id=parsed.value)
            if parsed.kind is RefKind.HANDLE:
                return api.resolve_channel(handle=parsed.value)
            if parsed.kind is RefKind.USER:
                return api.resolve_channel(username=parsed.value)
            if parsed.kind is RefKind.CUSTOM:
                # A vanity name is very often also the handle; try that before
                # falling back, since it is one cheap unit either way.
                try:
                    return api.resolve_channel(handle=parsed.value)
                except ChannelNotFound:
                    try:
                        return api.resolve_channel(username=parsed.value)
                    except ChannelNotFound:
                        pass
        except QuotaExhausted:
            raise
        except (YouTubeAPIError, ChannelNotFound) as exc:
            log.warning(
                "api resolution failed; falling back to yt-dlp",
                ref=parsed.original,
                error=str(exc)[:300],
            )

    if parsed.kind is RefKind.PLAYLIST:
        raise ResolveError(
            f"{parsed.original!r} is a playlist, not a channel; "
            "playlist ingestion is not supported"
        )
    return resolve_with_ytdlp(parsed)


# --------------------------------------------------------------------------- #
# Enumeration
# --------------------------------------------------------------------------- #


@dataclass
class DiscoverResult:
    channel_id: str
    pages: int = 0
    seen: int = 0
    new: int = 0
    complete: bool = False
    skipped_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    tabs: dict[str, int] = field(default_factory=dict)

    @property
    def was_skipped(self) -> bool:
        return self.skipped_reason is not None


def discover_channel(
    repo: Repo,
    channel: ChannelRow,
    *,
    api: YouTubeAPI | None,
    incremental: bool = False,
    limit: int | None = None,
    include_shorts: bool = False,
    include_streams: bool = False,
    cookies_file: str | None = None,
    proxy: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> DiscoverResult:
    """Enumerate one channel's videos into ``videos``.

    Args:
        incremental: Check RSS first and skip the channel entirely if it has
            nothing newer than ``newest_published_at``.
        limit: Stop after roughly this many videos (for smoke tests).
        include_shorts: Union in the ``/shorts`` tab via yt-dlp.
        include_streams: Union in the ``/streams`` tab via yt-dlp.
        should_stop: Polled between pages so a stop request lands promptly.

    Returns:
        A :class:`DiscoverResult`. ``warnings`` carries anything the operator
        needs to know, notably a suspected pagination ceiling.
    """
    result = DiscoverResult(channel_id=channel.channel_id)

    if incremental:
        verdict = _incremental_verdict(repo, channel, proxy=proxy)
        if verdict is not None:
            result.skipped_reason = verdict
            log.info(
                "channel unchanged; skipping enumeration",
                channel_id=channel.channel_id,
                reason=verdict,
            )
            return result

    if api is not None and channel.uploads_playlist_id:
        _discover_via_api(
            repo, channel, api=api, limit=limit, result=result, should_stop=should_stop
        )
    else:
        log.info(
            "enumerating with yt-dlp",
            channel_id=channel.channel_id,
            reason="no api key" if api is None else "no uploads playlist",
        )
        _discover_tab(
            repo, channel, "videos", result,
            limit=limit, cookies_file=cookies_file, proxy=proxy,
        )
        result.complete = True
        repo.save_enumeration_cursor(channel.channel_id, cursor=None, complete=True)

    for tab, enabled in (("shorts", include_shorts), ("streams", include_streams)):
        if not enabled:
            continue
        if should_stop is not None and should_stop():
            break
        try:
            _discover_tab(
                repo, channel, tab, result,
                limit=limit, cookies_file=cookies_file, proxy=proxy,
            )
        except Exception as exc:  # noqa: BLE001 - a missing tab must not fail the run
            message = f"{tab} tab enumeration failed: {type(exc).__name__}: {exc}"
            result.warnings.append(message[:500])
            log.warning("tab enumeration failed", channel_id=channel.channel_id,
                        tab=tab, error=str(exc)[:300])

    return result


def _incremental_verdict(
    repo: Repo, channel: ChannelRow, *, proxy: str | None
) -> str | None:
    """Return a skip reason, or ``None`` if enumeration should proceed.

    RSS is free and needs no key, so this check costs nothing and saves a full
    pagination pass on every unchanged channel - which, on a daily cron over a
    hundred channels, is the difference between fitting in quota and not.
    """
    if not channel.enumeration_complete:
        return None  # a half-enumerated channel must be finished first
    try:
        entries = fetch_rss_latest(channel.channel_id, proxy=proxy)
    except (ChannelNotFound, YouTubeAPIError, OSError) as exc:
        log.warning(
            "rss check failed; falling back to full enumeration",
            channel_id=channel.channel_id, error=str(exc)[:300],
        )
        return None
    if not entries:
        return None

    # Persist what RSS already told us; it is free metadata.
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id=e.video_id,
                channel_id=channel.channel_id,
                title=e.title,
                published_at=e.published_at,
            )
            for e in entries
        ]
    )

    published = [e.published_at for e in entries if e.published_at is not None]
    if not published:
        return None
    newest = max(published)
    known = channel.newest_published_at
    if known is None:
        return None
    if newest <= known.replace(tzinfo=None):
        return f"rss newest {newest.isoformat()} is not newer than {known.isoformat()}"
    return None


def _discover_via_api(
    repo: Repo,
    channel: ChannelRow,
    *,
    api: YouTubeAPI,
    limit: int | None,
    result: DiscoverResult,
    should_stop: Callable[[], bool] | None,
) -> None:
    playlist_id = channel.uploads_playlist_id
    assert playlist_id is not None
    # Resume from wherever the last run stopped.
    token = channel.enumeration_cursor
    if token:
        log.info("resuming enumeration", channel_id=channel.channel_id, cursor=token)

    newest_seen: datetime | None = None
    total_results: int | None = None

    while True:
        if should_stop is not None and should_stop():
            log.info("stop requested mid-enumeration; cursor saved",
                     channel_id=channel.channel_id)
            return

        page = api.playlist_page(playlist_id, page_token=token)
        result.pages += 1
        total_results = page.total_results or total_results

        if page.items:
            ids = [item.video_id for item in page.items]
            result.new += repo.count_new_videos(ids)
            repo.upsert_videos(
                [
                    VideoUpsert(
                        video_id=item.video_id,
                        channel_id=channel.channel_id,
                        title=item.title,
                        published_at=item.published_at,
                        description=item.description,
                        thumbnail_url=item.thumbnail_url,
                    )
                    for item in page.items
                ]
            )
            result.seen += len(page.items)
            for item in page.items:
                if item.published_at is not None and (
                    newest_seen is None or item.published_at > newest_seen
                ):
                    newest_seen = item.published_at

        token = page.next_page_token
        # Persist after *every* page. This is the whole resume story, and it is
        # one UPDATE per 50 videos.
        repo.save_enumeration_cursor(
            channel.channel_id,
            cursor=token,
            complete=token is None,
            newest_published_at=newest_seen,
        )

        log.info(
            "enumerated page",
            channel_id=channel.channel_id,
            page=result.pages,
            seen=result.seen,
            new=result.new,
            has_more=token is not None,
        )

        if token is None:
            result.complete = True
            break
        if limit is not None and result.seen >= limit:
            log.info("enumeration limit reached", channel_id=channel.channel_id,
                     limit=limit)
            break

    if result.complete:
        _warn_if_truncated(channel, result, total_results)


def _warn_if_truncated(
    channel: ChannelRow, result: DiscoverResult, total_results: int | None
) -> None:
    """Detect the uploads-playlist pagination ceiling instead of hiding it.

    Very large channels stop yielding a ``nextPageToken`` well before the end -
    around 20,000 items. Reporting "complete" at that point would silently lose
    half a channel, so say so loudly and let the operator decide.
    """
    expected = channel.reported_video_count or total_results
    if expected is None:
        return
    shortfall = expected - result.seen
    if result.seen >= PLAYLIST_PAGINATION_CEILING * 0.95 and shortfall > 100:
        message = (
            f"pagination stopped at {result.seen} items but the channel reports "
            f"{expected}: the uploads playlist has a ~{PLAYLIST_PAGINATION_CEILING} "
            "item ceiling. Enumerate the /videos, /shorts and /streams tabs with "
            "yt-dlp to reach the rest."
        )
        result.warnings.append(message)
        log.warning(
            "enumeration likely truncated",
            channel_id=channel.channel_id,
            seen=result.seen,
            reported=expected,
        )
    elif shortfall > max(50, expected * 0.2):
        message = (
            f"enumerated {result.seen} of a reported {expected} videos; the "
            "difference is usually private, deleted or members-only uploads."
        )
        result.warnings.append(message)


def _discover_tab(
    repo: Repo,
    channel: ChannelRow,
    tab: str,
    result: DiscoverResult,
    *,
    limit: int | None,
    cookies_file: str | None,
    proxy: str | None,
) -> None:
    """Union one channel tab into ``videos`` via yt-dlp."""
    url = TAB_URLS[tab].format(cid=channel.channel_id)
    entries = flat_playlist(
        url, limit=limit, cookies_file=cookies_file, proxy=proxy
    )
    if not entries:
        result.tabs[tab] = 0
        return

    ids = [e.video_id for e in entries]
    fresh = repo.count_new_videos(ids)
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id=e.video_id, channel_id=channel.channel_id, title=e.title
            )
            for e in entries
        ]
    )
    result.tabs[tab] = len(entries)
    result.new += fresh
    result.seen += len(entries)
    log.info(
        "enumerated tab",
        channel_id=channel.channel_id, tab=tab, found=len(entries), new=fresh,
    )


def discover_all(
    repo: Repo,
    *,
    api: YouTubeAPI | None,
    channel_id: str | None = None,
    incremental: bool = False,
    limit: int | None = None,
    include_shorts: bool = False,
    include_streams: bool = False,
    cookies_file: str | None = None,
    proxy: str | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[DiscoverResult]:
    """Enumerate every enabled channel, or just one."""
    if channel_id:
        row = repo.get_channel(channel_id)
        if row is None:
            raise ResolveError(f"unknown channel {channel_id}")
        channels: Sequence[ChannelRow] = [row]
    else:
        channels = repo.list_channels(enabled_only=True)

    results: list[DiscoverResult] = []
    for row in channels:
        if should_stop is not None and should_stop():
            break
        try:
            results.append(
                discover_channel(
                    repo, row,
                    api=api,
                    incremental=incremental,
                    limit=limit,
                    include_shorts=include_shorts,
                    include_streams=include_streams,
                    cookies_file=cookies_file,
                    proxy=proxy,
                    should_stop=should_stop,
                )
            )
        except QuotaExhausted:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad channel must not stop the rest
            log.error(
                "channel enumeration failed",
                channel_id=row.channel_id,
                error=str(exc)[:500],
                error_type=type(exc).__name__,
            )
            failed = DiscoverResult(channel_id=row.channel_id)
            failed.warnings.append(f"{type(exc).__name__}: {exc}"[:500])
            results.append(failed)
    return results
