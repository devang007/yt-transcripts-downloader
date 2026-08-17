"""YouTube Data API v3 client, quota ledger, and the free RSS shortcut.

Quota is the binding constraint on enumeration, not rate limits. A default
project gets 10,000 units/day, and the costs are wildly uneven:

===================== =====
call                  units
===================== =====
``channels.list``         1
``playlistItems.list``    1
``videos.list``           1
``search.list``         100
===================== =====

``search.list`` is therefore never used - it would burn the entire daily budget
on 100 calls, and everything it offers is reachable through the uploads playlist
for one unit a page. :class:`YouTubeAPI` has no method that calls it.

The cheapest enumeration path is not the API at all: a channel's RSS feed
(``feeds/videos.xml``) returns roughly the 15 newest uploads for zero units and
no key. :func:`fetch_rss_latest` is what a daily cron should lean on, falling
through to the paginated API only when something genuinely new shows up.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, cast
from xml.etree import ElementTree

import requests

from .classify import QuotaExhausted
from .logs import get_logger

log = get_logger(__name__)

API_ROOT: Final = "https://www.googleapis.com/youtube/v3"
RSS_URL: Final = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

COST_CHANNELS_LIST: Final = 1
COST_PLAYLIST_ITEMS_LIST: Final = 1
COST_VIDEOS_LIST: Final = 1

MAX_IDS_PER_VIDEOS_CALL: Final = 50
MAX_PLAYLIST_PAGE: Final = 50

# The uploads playlist stops paginating somewhere around 20k items regardless of
# how many videos the channel actually has. Detected and warned about rather than
# silently truncating a 40k-video channel to 20k.
PLAYLIST_PAGINATION_CEILING: Final = 20_000

_DURATION_RE: Final = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)

# Shorts were historically <= 60s; the limit is now 3 minutes. Duration alone
# cannot prove a video is a Short, which is why discovery also unions the
# /shorts tab when enabled - this is a heuristic and is treated as one.
SHORTS_MAX_SECONDS: Final = 180


class YouTubeAPIError(RuntimeError):
    """A Data API call failed in a way worth reporting to the operator."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class ChannelNotFound(YouTubeAPIError):
    """The reference does not resolve to a channel."""


def parse_iso8601_duration(value: str | None) -> int | None:
    """``PT1H2M3S`` -> 3723 seconds. Returns ``None`` for absent or unparsable.

    Livestreams sometimes report ``P0D``, which is a real value meaning "no
    duration known", so it maps to 0 rather than to an error.
    """
    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        log.warning("unparsable duration", duration=value)
        return None
    parts = match.groupdict()
    total = 0.0
    total += int(parts["days"] or 0) * 86400
    total += int(parts["hours"] or 0) * 3600
    total += int(parts["minutes"] or 0) * 60
    total += float(parts["seconds"] or 0)
    return int(total)


def parse_rfc3339(value: str | None) -> datetime | None:
    """API timestamps are RFC-3339 UTC; return a naive UTC datetime for MySQL."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        log.warning("unparsable timestamp", value=value)
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def best_thumbnail(thumbnails: object) -> str | None:
    """Highest-resolution thumbnail URL available."""
    if not isinstance(thumbnails, dict):
        return None
    order = ("maxres", "standard", "high", "medium", "default")
    entries = cast("dict[str, Any]", thumbnails)
    for key in order:
        entry = entries.get(key)
        if isinstance(entry, dict):
            url = entry.get("url")
            if isinstance(url, str):
                return url[:512]
    return None


# --------------------------------------------------------------------------- #
# Quota
# --------------------------------------------------------------------------- #


class QuotaGuard:
    """Charges the ledger before each call and stops the run before the wall.

    Stopping at 90% by default leaves headroom for a graceful shutdown and for
    whatever else shares the project's key. Google resets at midnight *Pacific*,
    which is why the ledger is keyed on a Pacific date rather than a UTC one.
    """

    def __init__(
        self,
        *,
        charge: Callable[[int], int],
        budget: int,
        stop_at_pct: int = 90,
    ) -> None:
        self._charge = charge
        self._budget = max(1, budget)
        self._limit = int(self._budget * min(100, max(1, stop_at_pct)) / 100)
        self.used = 0

    @property
    def limit(self) -> int:
        return self._limit

    def spend(self, units: int) -> None:
        """Record spend and raise once the soft limit is crossed.

        Raises:
            QuotaExhausted: the run should end with ``exit_reason``
                ``quota_exhausted``, leaving everything queued for tomorrow.
        """
        self.used = self._charge(units)
        if self.used >= self._limit:
            raise QuotaExhausted(
                f"Data API quota at {self.used}/{self._budget} units "
                f"(soft limit {self._limit}); stopping until the Pacific-midnight reset"
            )

    def pct(self) -> float:
        return round(100.0 * self.used / self._budget, 1)


# --------------------------------------------------------------------------- #
# Payload types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ResolvedChannel:
    channel_id: str
    title: str | None
    handle: str | None
    uploads_playlist_id: str | None
    video_count: int | None


@dataclass(frozen=True, slots=True)
class PlaylistItem:
    video_id: str
    title: str | None
    published_at: datetime | None
    description: str | None
    thumbnail_url: str | None


@dataclass(frozen=True, slots=True)
class PlaylistPage:
    items: list[PlaylistItem]
    next_page_token: str | None
    total_results: int | None


@dataclass(frozen=True, slots=True)
class VideoDetails:
    video_id: str
    title: str | None
    description: str | None
    published_at: datetime | None
    duration_seconds: int | None
    view_count: int | None
    like_count: int | None
    comment_count: int | None
    tags: list[str] | None
    category_id: str | None
    default_language: str | None
    default_audio_language: str | None
    live_broadcast_content: str | None
    was_livestream: bool
    thumbnail_url: str | None
    privacy_status: str | None
    upload_status: str | None
    embeddable: bool | None = None
    made_for_kids: bool | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def is_short_guess(self) -> bool:
        """Duration heuristic only. Discovery's /shorts tab is authoritative."""
        return (
            self.duration_seconds is not None
            and 0 < self.duration_seconds <= SHORTS_MAX_SECONDS
            and not self.was_livestream
        )


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class YouTubeAPI:
    """Thin Data API v3 client. Only the three cheap endpoints are reachable."""

    def __init__(
        self,
        api_key: str,
        *,
        quota: QuotaGuard | None = None,
        session: requests.Session | None = None,
        before_request: Callable[[], None] | None = None,
        timeout: float = 30.0,
        proxy: str | None = None,
    ) -> None:
        if not api_key:
            raise YouTubeAPIError("a Data API key is required")
        self._key = api_key
        self._quota = quota
        self._timeout = timeout
        self._before = before_request
        self._session = session or requests.Session()
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})

    def close(self) -> None:
        self._session.close()

    # -- transport --------------------------------------------------------- #

    def _get(self, path: str, params: dict[str, Any], cost: int) -> dict[str, Any]:
        if self._quota is not None:
            self._quota.spend(cost)
        if self._before is not None:
            self._before()

        query = {k: v for k, v in params.items() if v is not None}
        query["key"] = self._key
        response = self._session.get(
            f"{API_ROOT}/{path}", params=query, timeout=self._timeout
        )

        if response.status_code == 403:
            reason = _error_reason(response)
            if reason in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
                raise QuotaExhausted(f"Data API refused the call: {reason}")
            raise YouTubeAPIError(
                f"Data API forbidden ({reason or 'no reason given'}); "
                "check that the key is valid and the API is enabled",
                status=403,
            )
        if response.status_code == 400:
            raise YouTubeAPIError(
                f"Data API rejected the request: {_error_reason(response) or response.text[:200]}",
                status=400,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise YouTubeAPIError("Data API returned a non-object payload")
        return cast("dict[str, Any]", payload)

    # -- channels ---------------------------------------------------------- #

    def resolve_channel(
        self,
        *,
        channel_id: str | None = None,
        handle: str | None = None,
        username: str | None = None,
    ) -> ResolvedChannel:
        """Resolve exactly one of id / handle / legacy username.

        Raises:
            ChannelNotFound: nothing matched.
        """
        params: dict[str, Any] = {
            "part": "snippet,contentDetails,statistics",
            "maxResults": 1,
        }
        if channel_id:
            params["id"] = channel_id
        elif handle:
            params["forHandle"] = handle if handle.startswith("@") else f"@{handle}"
        elif username:
            params["forUsername"] = username
        else:
            raise ValueError("one of channel_id, handle or username is required")

        payload = self._get("channels", params, COST_CHANNELS_LIST)
        items = payload.get("items") or []
        if not items:
            target = channel_id or handle or username
            raise ChannelNotFound(f"no channel matched {target!r}")

        item = cast("dict[str, Any]", items[0])
        snippet = cast("dict[str, Any]", item.get("snippet") or {})
        content = cast("dict[str, Any]", item.get("contentDetails") or {})
        stats = cast("dict[str, Any]", item.get("statistics") or {})
        related = cast("dict[str, Any]", content.get("relatedPlaylists") or {})
        custom = snippet.get("customUrl")

        return ResolvedChannel(
            channel_id=str(item["id"]),
            title=_clip(snippet.get("title"), 512),
            handle=_normalise_handle(custom) or _normalise_handle(handle),
            uploads_playlist_id=related.get("uploads"),
            video_count=_as_int(stats.get("videoCount")),
        )

    # -- enumeration ------------------------------------------------------- #

    def playlist_page(
        self, playlist_id: str, *, page_token: str | None = None
    ) -> PlaylistPage:
        """One page of up to 50 uploads. One quota unit."""
        payload = self._get(
            "playlistItems",
            {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": MAX_PLAYLIST_PAGE,
                "pageToken": page_token,
            },
            COST_PLAYLIST_ITEMS_LIST,
        )
        info = cast("dict[str, Any]", payload.get("pageInfo") or {})
        items: list[PlaylistItem] = []
        for raw in payload.get("items") or []:
            entry = cast("dict[str, Any]", raw)
            snippet = cast("dict[str, Any]", entry.get("snippet") or {})
            content = cast("dict[str, Any]", entry.get("contentDetails") or {})
            video_id = content.get("videoId") or (
                cast("dict[str, Any]", snippet.get("resourceId") or {})
            ).get("videoId")
            if not video_id:
                continue
            # contentDetails.videoPublishedAt is when the *video* went live;
            # snippet.publishedAt is when it was added to the playlist. For an
            # uploads playlist they usually agree, but the former is correct.
            published = parse_rfc3339(
                content.get("videoPublishedAt") or snippet.get("publishedAt")
            )
            title = snippet.get("title")
            # Deleted and private videos remain in the playlist as tombstones.
            if title in {"Deleted video", "Private video"}:
                title = None
            items.append(
                PlaylistItem(
                    video_id=str(video_id),
                    title=_clip(title, 1024),
                    published_at=published,
                    description=snippet.get("description") or None,
                    thumbnail_url=best_thumbnail(snippet.get("thumbnails")),
                )
            )
        return PlaylistPage(
            items=items,
            next_page_token=payload.get("nextPageToken"),
            total_results=_as_int(info.get("totalResults")),
        )

    # -- metadata ---------------------------------------------------------- #

    def video_details(self, video_ids: Sequence[str]) -> list[VideoDetails]:
        """Hydrate up to 50 ids in one call, for one quota unit.

        Ids the API declines to return (deleted, private) are simply absent from
        the response; the caller diffs against what it asked for.
        """
        if not video_ids:
            return []
        if len(video_ids) > MAX_IDS_PER_VIDEOS_CALL:
            raise ValueError(
                f"videos.list accepts at most {MAX_IDS_PER_VIDEOS_CALL} ids per call"
            )
        payload = self._get(
            "videos",
            {
                "part": "snippet,contentDetails,statistics,status,liveStreamingDetails",
                "id": ",".join(video_ids),
                "maxResults": MAX_IDS_PER_VIDEOS_CALL,
            },
            COST_VIDEOS_LIST,
        )
        return [
            _video_details(cast("dict[str, Any]", raw))
            for raw in payload.get("items") or []
        ]


def _video_details(item: dict[str, Any]) -> VideoDetails:
    snippet = cast("dict[str, Any]", item.get("snippet") or {})
    content = cast("dict[str, Any]", item.get("contentDetails") or {})
    stats = cast("dict[str, Any]", item.get("statistics") or {})
    status = cast("dict[str, Any]", item.get("status") or {})
    live = cast("dict[str, Any]", item.get("liveStreamingDetails") or {})
    tags = snippet.get("tags")

    broadcast = snippet.get("liveBroadcastContent")
    if broadcast not in {"none", "live", "upcoming"}:
        broadcast = "none" if broadcast is None else None

    return VideoDetails(
        video_id=str(item["id"]),
        title=_clip(snippet.get("title"), 1024),
        description=snippet.get("description") or None,
        published_at=parse_rfc3339(snippet.get("publishedAt")),
        duration_seconds=parse_iso8601_duration(content.get("duration")),
        view_count=_as_int(stats.get("viewCount")),
        like_count=_as_int(stats.get("likeCount")),
        comment_count=_as_int(stats.get("commentCount")),
        tags=[str(t) for t in tags] if isinstance(tags, list) else None,
        category_id=_clip(snippet.get("categoryId"), 8),
        default_language=_clip(snippet.get("defaultLanguage"), 16),
        default_audio_language=_clip(snippet.get("defaultAudioLanguage"), 16),
        live_broadcast_content=broadcast,
        # liveStreamingDetails is present for anything that was ever a stream,
        # including a finished one, which is the durable signal.
        was_livestream=bool(live),
        thumbnail_url=best_thumbnail(snippet.get("thumbnails")),
        privacy_status=status.get("privacyStatus"),
        upload_status=status.get("uploadStatus"),
        embeddable=status.get("embeddable"),
        made_for_kids=status.get("madeForKids"),
    )


# --------------------------------------------------------------------------- #
# RSS: free, unauthenticated, ~15 newest videos
# --------------------------------------------------------------------------- #

_ATOM: Final = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


@dataclass(frozen=True, slots=True)
class RssEntry:
    video_id: str
    title: str | None
    published_at: datetime | None


def fetch_rss_latest(
    channel_id: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 20.0,
    proxy: str | None = None,
) -> list[RssEntry]:
    """The newest ~15 uploads, for zero quota units and no API key.

    This is what an incremental daily run should start with: if nothing here is
    newer than ``channels.newest_published_at``, the channel can be skipped
    without spending a single unit.
    """
    http = session or requests.Session()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    response = http.get(
        RSS_URL.format(channel_id=channel_id), timeout=timeout, proxies=proxies
    )
    if response.status_code == 404:
        raise ChannelNotFound(f"RSS feed not found for {channel_id}")
    response.raise_for_status()

    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        raise YouTubeAPIError(f"malformed RSS for {channel_id}: {exc}") from exc

    entries: list[RssEntry] = []
    for entry in root.findall("atom:entry", _ATOM):
        video_id = entry.findtext("yt:videoId", namespaces=_ATOM)
        if not video_id:
            continue
        entries.append(
            RssEntry(
                video_id=video_id.strip(),
                title=_clip(entry.findtext("atom:title", namespaces=_ATOM), 1024),
                published_at=parse_rfc3339(
                    entry.findtext("atom:published", namespaces=_ATOM)
                ),
            )
        )
    return entries


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _error_reason(response: requests.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = cast("dict[str, Any]", payload).get("error")
    if not isinstance(error, dict):
        return None
    errors = cast("dict[str, Any]", error).get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            reason = cast("dict[str, Any]", first).get("reason")
            if isinstance(reason, str):
                return reason
    message = cast("dict[str, Any]", error).get("message")
    return message if isinstance(message, str) else None


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _clip(value: object, length: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:length] if text else None


def _normalise_handle(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not text.startswith("@"):
        text = f"@{text}"
    return text[:128]


def chunked(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])
