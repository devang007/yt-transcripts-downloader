"""Recorded-cassette stand-ins for the Data API and the transcript fetchers.

Recorded rather than live: a unit suite that depends on YouTube being reachable
fails at 2am for reasons that have nothing to do with the code. These replay
fixture payloads through the exact same call signatures as the real classes, so
they exercise the production code paths rather than a parallel universe.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from yt_tx.fetch import Available, Segment, Selection
from yt_tx.states import TranscriptKind
from yt_tx.youtube_api import (
    ChannelNotFound,
    PlaylistItem,
    PlaylistPage,
    ResolvedChannel,
    RssEntry,
    VideoDetails,
)

BASE_TIME = datetime(2024, 1, 1, 12, 0, 0)


class Boom(Exception):
    """Injected failure, standing in for a process being killed mid-page."""


@dataclass
class FakeYouTubeAPI:
    """Replays fixture pages with the real :class:`YouTubeAPI` call signatures."""

    pages: list[PlaylistPage] = field(default_factory=list)
    details: dict[str, VideoDetails] = field(default_factory=dict)
    channel: ResolvedChannel | None = None

    fail_on_page: int | None = None
    """Raise :class:`Boom` when this 1-based page is requested."""

    calls: list[str] = field(default_factory=list)
    page_requests: list[str | None] = field(default_factory=list)
    detail_requests: list[list[str]] = field(default_factory=list)
    closed: bool = False

    # -- channels ---------------------------------------------------------- #

    def resolve_channel(
        self,
        *,
        channel_id: str | None = None,
        handle: str | None = None,
        username: str | None = None,
    ) -> ResolvedChannel:
        self.calls.append("channels.list")
        if self.channel is None:
            raise ChannelNotFound(f"no channel matched {channel_id or handle or username!r}")
        return self.channel

    # -- enumeration ------------------------------------------------------- #

    def playlist_page(
        self, playlist_id: str, *, page_token: str | None = None
    ) -> PlaylistPage:
        self.calls.append("playlistItems.list")
        self.page_requests.append(page_token)

        index = 0
        if page_token is not None:
            index = next(
                (
                    i + 1
                    for i, page in enumerate(self.pages)
                    if page.next_page_token == page_token
                ),
                0,
            )
        if self.fail_on_page is not None and index + 1 == self.fail_on_page:
            raise Boom(f"killed while fetching page {index + 1}")
        if index >= len(self.pages):
            return PlaylistPage(items=[], next_page_token=None, total_results=0)
        return self.pages[index]

    # -- metadata ---------------------------------------------------------- #

    def video_details(self, video_ids: Sequence[str]) -> list[VideoDetails]:
        self.calls.append("videos.list")
        self.detail_requests.append(list(video_ids))
        return [self.details[v] for v in video_ids if v in self.details]

    def close(self) -> None:
        self.closed = True


def make_pages(
    total: int, *, per_page: int = 3, prefix: str = "vid"
) -> list[PlaylistPage]:
    """``total`` videos split into pages, each linking to the next by token."""
    pages: list[PlaylistPage] = []
    ids = [f"{prefix}{i:08d}" for i in range(total)]
    for start in range(0, total, per_page):
        chunk = ids[start : start + per_page]
        is_last = start + per_page >= total
        pages.append(
            PlaylistPage(
                items=[
                    PlaylistItem(
                        video_id=video_id,
                        title=f"Video {video_id}",
                        published_at=BASE_TIME - timedelta(days=ids.index(video_id)),
                        description=f"Description for {video_id}",
                        thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hq.jpg",
                    )
                    for video_id in chunk
                ],
                next_page_token=None if is_last else f"token-{start + per_page}",
                total_results=total,
            )
        )
    return pages


def make_details(
    video_id: str,
    *,
    duration_seconds: int | None = 600,
    live: str | None = "none",
    was_livestream: bool = False,
    privacy: str | None = "public",
    upload_status: str | None = "processed",
    title: str | None = None,
) -> VideoDetails:
    return VideoDetails(
        video_id=video_id,
        title=title or f"Video {video_id}",
        description="A description.",
        published_at=BASE_TIME,
        duration_seconds=duration_seconds,
        view_count=1234,
        like_count=56,
        comment_count=7,
        tags=["testing"],
        category_id="27",
        default_language="en",
        default_audio_language="en",
        live_broadcast_content=live,
        was_livestream=was_livestream,
        thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hq.jpg",
        privacy_status=privacy,
        upload_status=upload_status,
    )


def make_rss(ids: Sequence[str], *, newest: datetime | None = None) -> list[RssEntry]:
    base = newest or BASE_TIME
    return [
        RssEntry(
            video_id=video_id,
            title=f"Video {video_id}",
            published_at=base - timedelta(hours=index),
        )
        for index, video_id in enumerate(ids)
    ]


# --------------------------------------------------------------------------- #
# Fetchers
# --------------------------------------------------------------------------- #


@dataclass
class FakeFetcher:
    """A :class:`~yt_tx.fetch.TranscriptFetcher` driven entirely by fixtures."""

    name: str = "fake"
    available: list[Available] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)

    list_error: BaseException | None = None
    download_error: BaseException | None = None
    empty_download: bool = False

    list_calls: list[str] = field(default_factory=list)
    download_calls: list[tuple[str, str, str]] = field(default_factory=list)
    closed: bool = False

    def list_available(self, video_id: str) -> list[Available]:
        self.list_calls.append(video_id)
        if self.list_error is not None:
            raise self.list_error
        return list(self.available)

    def download(self, video_id: str, selection: Selection) -> list[Segment]:
        self.download_calls.append(
            (video_id, selection.stored_language, selection.stored_kind.value)
        )
        if self.download_error is not None:
            raise self.download_error
        if self.empty_download:
            return []
        return list(self.segments)

    def close(self) -> None:
        self.closed = True


def english_manual() -> list[Available]:
    return [
        Available("en", "English", TranscriptKind.MANUAL),
        Available("es", "Spanish", TranscriptKind.ASR, is_translatable=True),
    ]


def japanese_only() -> list[Available]:
    return [
        Available(
            "ja", "Japanese", TranscriptKind.ASR,
            is_translatable=True, translation_targets=("en", "hi", "fr"),
        )
    ]


def sample_segments() -> list[Segment]:
    return [
        Segment(start=0.0, duration=2.5, text="Hello and welcome"),
        Segment(start=2.5, duration=3.0, text="to the show"),
        Segment(start=5.5, duration=2.0, text="[Music]"),
        Segment(start=7.5, duration=4.0, text="Today we discuss mitochondria"),
    ]


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_any(value: object) -> Any:
    """Cast helper for handing fakes to code typed against the real classes."""
    return value
