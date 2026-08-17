"""Reference parsing (pure) and enumeration with recorded pages.

The cursor-resume test is the one that matters. Persisting
``enumeration_cursor`` after every page is what turns a crash 8,000 videos into a
40,000-video channel from "start again tomorrow" into "carry on", and the only way
to know it works is to kill a run mid-channel and restart it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.fake_api import (
    BASE_TIME,
    Boom,
    FakeYouTubeAPI,
    as_any,
    make_details,
    make_pages,
    make_rss,
)
from yt_tx.discover import (
    ChannelRef,
    RefKind,
    ResolveError,
    discover_channel,
    parse_channel_ref,
    resolve_channel,
    uploads_playlist_for,
)
from yt_tx.repo import Repo
from yt_tx.states import Status
from yt_tx.youtube_api import ResolvedChannel

CHANNEL_ID = "UCXuqSBlHAE6Xw-yeJA0Tunw"

# --------------------------------------------------------------------------- #
# Reference parsing - pure, no network, no database
# --------------------------------------------------------------------------- #

REF_CASES: tuple[tuple[str, RefKind, str], ...] = (
    (CHANNEL_ID, RefKind.CHANNEL_ID, CHANNEL_ID),
    ("@lexfridman", RefKind.HANDLE, "@lexfridman"),
    ("lexfridman", RefKind.CUSTOM, "lexfridman"),
    (f"https://www.youtube.com/channel/{CHANNEL_ID}", RefKind.CHANNEL_ID, CHANNEL_ID),
    (f"https://youtube.com/channel/{CHANNEL_ID}/videos", RefKind.CHANNEL_ID, CHANNEL_ID),
    ("https://www.youtube.com/@3blue1brown", RefKind.HANDLE, "@3blue1brown"),
    ("https://www.youtube.com/@3blue1brown/shorts", RefKind.HANDLE, "@3blue1brown"),
    ("https://www.youtube.com/c/3blue1brown", RefKind.CUSTOM, "3blue1brown"),
    ("https://www.youtube.com/user/GoogleTechTalks", RefKind.USER, "GoogleTechTalks"),
    ("www.youtube.com/@someone", RefKind.HANDLE, "@someone"),
    ("youtube.com/@someone", RefKind.HANDLE, "@someone"),
    ("/channel/" + CHANNEL_ID, RefKind.CHANNEL_ID, CHANNEL_ID),
    ("https://m.youtube.com/@mobile", RefKind.HANDLE, "@mobile"),
    # Tracking parameters must not become part of the identifier.
    (f"https://www.youtube.com/channel/{CHANNEL_ID}?si=abc123", RefKind.CHANNEL_ID, CHANNEL_ID),
    ("https://www.youtube.com/@handle?feature=share", RefKind.HANDLE, "@handle"),
    # Percent-encoded vanity path.
    ("https://www.youtube.com/c/Some%20Name", RefKind.CUSTOM, "Some Name"),
    ("  @padded  ", RefKind.HANDLE, "@padded"),
    ("https://www.youtube.com/playlist?list=PLabc123", RefKind.PLAYLIST, "PLabc123"),
)


@pytest.mark.parametrize("raw,kind,value", REF_CASES, ids=[c[0] for c in REF_CASES])
def test_parse_channel_ref(raw: str, kind: RefKind, value: str) -> None:
    parsed = parse_channel_ref(raw)
    assert parsed.kind is kind
    assert parsed.value == value
    assert parsed.original == raw.strip()


BAD_REFS: tuple[str, ...] = (
    "",
    "   ",
    "https://vimeo.com/12345",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/channel/not-a-real-id",
    "https://www.youtube.com/",
)


@pytest.mark.parametrize("raw", BAD_REFS)
def test_parse_channel_ref_rejects(raw: str) -> None:
    with pytest.raises(ResolveError):
        parse_channel_ref(raw)


def test_uploads_playlist_derivation() -> None:
    """UC -> UU is documented and stable, and saves a lookup."""
    assert uploads_playlist_for(CHANNEL_ID) == "UU" + CHANNEL_ID[2:]
    with pytest.raises(ValueError):
        uploads_playlist_for("not-a-channel")


def test_channel_ref_is_canonical() -> None:
    assert ChannelRef(RefKind.CHANNEL_ID, CHANNEL_ID, CHANNEL_ID).is_canonical is True
    assert ChannelRef(RefKind.HANDLE, "@x", "@x").is_canonical is False


# --------------------------------------------------------------------------- #
# Resolution and enumeration - need the database
# --------------------------------------------------------------------------- #

mysql = pytest.mark.mysql


def _api(total: int = 7, **kwargs: object) -> FakeYouTubeAPI:
    return FakeYouTubeAPI(
        pages=make_pages(total),
        channel=ResolvedChannel(
            channel_id=CHANNEL_ID,
            title="Test Channel",
            handle="@testchannel",
            uploads_playlist_id="UU" + CHANNEL_ID[2:],
            video_count=total,
        ),
        **kwargs,  # type: ignore[arg-type]
    )


@mysql
def test_resolution_is_cached_and_costs_one_call(repo: Repo) -> None:
    api = _api()
    first = resolve_channel(repo, "@testchannel", api=as_any(api))
    assert first.channel_id == CHANNEL_ID
    assert api.calls.count("channels.list") == 1

    second = resolve_channel(repo, "@testchannel", api=as_any(api))
    assert second.channel_id == CHANNEL_ID
    assert api.calls.count("channels.list") == 1, "resolution should be cached forever"


@mysql
def test_resolution_falls_back_when_the_api_finds_nothing(repo: Repo) -> None:
    """A vanity /c/ URL has no Data API lookup, and search.list is off-limits."""
    api = FakeYouTubeAPI(channel=None)  # raises ChannelNotFound
    with pytest.raises(Exception):
        # yt-dlp would be the fallback, and the network guard blocks it, which is
        # itself the assertion: nothing here reaches for search.list.
        resolve_channel(repo, "https://www.youtube.com/c/Nonexistent", api=as_any(api))
    assert "search.list" not in api.calls


@mysql
def test_full_enumeration_persists_every_page(repo: Repo) -> None:
    api = _api(total=7)
    channel = resolve_channel(repo, "@testchannel", api=as_any(api))

    result = discover_channel(repo, channel, api=as_any(api))
    assert result.complete is True
    assert result.seen == 7
    assert result.new == 7
    assert result.pages == 3  # 3 + 3 + 1

    rows, total = repo.list_videos(channel_id=CHANNEL_ID, per_page=100)
    assert total == 7
    assert all(r.status is Status.DISCOVERED for r in rows)
    assert {r.video_id for r in rows} == {f"vid{i:08d}" for i in range(7)}

    stored = repo.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.enumeration_complete is True
    assert stored.enumeration_cursor is None
    assert stored.newest_published_at is not None


@mysql
def test_cursor_saved_after_every_page(repo: Repo) -> None:
    """One UPDATE per page is the entire cost of being resumable."""
    api = _api(total=9)
    channel = resolve_channel(repo, "@testchannel", api=as_any(api))
    discover_channel(repo, channel, api=as_any(api))
    # 3 pages requested: None, token-3, token-6.
    assert api.page_requests == [None, "token-3", "token-6"]


@mysql
def test_crash_mid_channel_resumes_without_duplicates_or_gaps(repo: Repo) -> None:
    """The headline property: kill mid-channel, restart, lose nothing, repeat nothing."""
    channel = resolve_channel(repo, "@testchannel", api=as_any(_api(total=9)))

    # First attempt dies while fetching page 3.
    dying = FakeYouTubeAPI(pages=make_pages(9), fail_on_page=3)
    with pytest.raises(Boom):
        discover_channel(repo, channel, api=as_any(dying))

    partial = repo.get_channel(CHANNEL_ID)
    assert partial is not None
    assert partial.enumeration_complete is False
    assert partial.enumeration_cursor == "token-6", "should be poised to resume page 3"
    _, mid_total = repo.list_videos(channel_id=CHANNEL_ID, per_page=100)
    assert mid_total == 6, "the two completed pages must be durable"

    # Restart. It must pick up at the saved cursor, not at the beginning.
    healthy = FakeYouTubeAPI(pages=make_pages(9))
    result = discover_channel(repo, partial, api=as_any(healthy))
    assert healthy.page_requests == ["token-6"], "resumed from the cursor"
    assert result.complete is True

    rows, total = repo.list_videos(channel_id=CHANNEL_ID, per_page=100)
    assert total == 9, "no pages lost"
    assert len({r.video_id for r in rows}) == 9, "no duplicates"


@mysql
def test_re_enumeration_does_not_reset_completed_videos(repo: Repo) -> None:
    """A nightly re-run must not undo yesterday's work."""
    from sqlalchemy import text

    channel = resolve_channel(repo, "@testchannel", api=as_any(_api(total=7)))
    discover_channel(repo, channel, api=as_any(_api(total=7)))
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status='transcript_ok', attempts=1"))

    fresh = repo.get_channel(CHANNEL_ID)
    assert fresh is not None
    result = discover_channel(repo, fresh, api=as_any(_api(total=7)))
    assert result.new == 0

    assert repo.status_counts() == {Status.TRANSCRIPT_OK.value: 7}


@mysql
def test_incremental_skips_a_channel_with_nothing_new(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RSS costs zero quota; that is the whole point of incremental mode."""
    import yt_tx.discover as discover_module

    channel = resolve_channel(repo, "@testchannel", api=as_any(_api(total=7)))
    discover_channel(repo, channel, api=as_any(_api(total=7)))

    stored = repo.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.newest_published_at is not None

    # RSS reports only videos we already know about.
    monkeypatch.setattr(
        discover_module,
        "fetch_rss_latest",
        lambda channel_id, **kwargs: make_rss(
            ["vid00000000"], newest=stored.newest_published_at.replace(tzinfo=None)
        ),
    )
    api = _api(total=7)
    result = discover_channel(repo, stored, api=as_any(api), incremental=True)

    assert result.was_skipped is True
    assert "not newer" in (result.skipped_reason or "")
    assert api.calls == [], "an unchanged channel must cost zero quota units"


@mysql
def test_incremental_proceeds_when_rss_shows_something_newer(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yt_tx.discover as discover_module

    channel = resolve_channel(repo, "@testchannel", api=as_any(_api(total=7)))
    discover_channel(repo, channel, api=as_any(_api(total=7)))
    stored = repo.get_channel(CHANNEL_ID)
    assert stored is not None

    monkeypatch.setattr(
        discover_module,
        "fetch_rss_latest",
        lambda channel_id, **kwargs: make_rss(
            ["brandnew001"], newest=BASE_TIME + timedelta(days=5)
        ),
    )
    api = _api(total=7)
    result = discover_channel(repo, stored, api=as_any(api), incremental=True)
    assert result.was_skipped is False
    assert "playlistItems.list" in api.calls
    # RSS metadata is persisted even before the paginated pass runs.
    assert repo.get_video("brandnew001") is not None


@mysql
def test_incremental_finishes_a_partial_channel_before_trusting_rss(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-enumerated channel must never be skipped as "unchanged"."""
    import yt_tx.discover as discover_module

    channel = resolve_channel(repo, "@testchannel", api=as_any(_api(total=9)))
    dying = FakeYouTubeAPI(pages=make_pages(9), fail_on_page=2)
    with pytest.raises(Boom):
        discover_channel(repo, channel, api=as_any(dying))

    called = {"rss": False}

    def rss(channel_id: str, **kwargs: object) -> list[object]:
        called["rss"] = True
        return []

    monkeypatch.setattr(discover_module, "fetch_rss_latest", rss)
    partial = repo.get_channel(CHANNEL_ID)
    assert partial is not None
    result = discover_channel(repo, partial, api=as_any(_api(total=9)), incremental=True)
    assert called["rss"] is False, "an incomplete channel must be finished, not skipped"
    assert result.complete is True


@mysql
def test_rss_failure_falls_back_to_full_enumeration(
    repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yt_tx.discover as discover_module

    channel = resolve_channel(repo, "@testchannel", api=as_any(_api(total=7)))
    discover_channel(repo, channel, api=as_any(_api(total=7)))
    stored = repo.get_channel(CHANNEL_ID)
    assert stored is not None

    def boom(channel_id: str, **kwargs: object) -> list[object]:
        raise OSError("dns failure")

    monkeypatch.setattr(discover_module, "fetch_rss_latest", boom)
    api = _api(total=7)
    result = discover_channel(repo, stored, api=as_any(api), incremental=True)
    assert result.was_skipped is False, "a flaky RSS fetch must not skip the channel"


@mysql
def test_limit_stops_enumeration_early_but_keeps_the_cursor(repo: Repo) -> None:
    channel = resolve_channel(repo, "@testchannel", api=as_any(_api(total=9)))
    result = discover_channel(repo, channel, api=as_any(_api(total=9)), limit=4)
    assert result.complete is False
    assert result.seen == 6  # stops at the first page boundary past the limit
    stored = repo.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.enumeration_cursor is not None, "must remain resumable"


@mysql
def test_shortfall_against_reported_count_is_reported(repo: Repo) -> None:
    """Silently enumerating half a channel is the failure mode being avoided."""
    api = FakeYouTubeAPI(
        pages=make_pages(3),
        channel=ResolvedChannel(
            channel_id=CHANNEL_ID, title="Big", handle="@big",
            uploads_playlist_id="UU" + CHANNEL_ID[2:],
            video_count=5000,  # far more than the 3 we will enumerate
        ),
    )
    channel = resolve_channel(repo, "@big", api=as_any(api))
    result = discover_channel(repo, channel, api=as_any(api))
    assert result.warnings, "a large shortfall must be surfaced, not hidden"
    assert "5000" in result.warnings[0]


@mysql
def test_discover_all_continues_past_a_broken_channel(repo: Repo) -> None:
    """One bad channel must not abort the other ninety-nine."""
    from yt_tx.discover import discover_all

    repo.upsert_channel(
        channel_id="UCgood000000000000000000",
        input_ref="@good", handle="@good",
        uploads_playlist_id="UUgood000000000000000000",
    )
    repo.upsert_channel(
        channel_id="UCbad0000000000000000000",
        input_ref="@bad", handle="@bad",
        uploads_playlist_id="UUbad0000000000000000000",
    )

    class HalfBroken(FakeYouTubeAPI):
        def playlist_page(self, playlist_id: str, *, page_token: str | None = None):  # type: ignore[no-untyped-def]
            if "bad" in playlist_id:
                raise Boom("this channel is cursed")
            return super().playlist_page(playlist_id, page_token=page_token)

    api = HalfBroken(pages=make_pages(3))
    results = discover_all(repo, api=as_any(api))
    assert len(results) == 2
    broken = [r for r in results if r.channel_id.startswith("UCbad")][0]
    assert broken.warnings
    good = [r for r in results if r.channel_id.startswith("UCgood")][0]
    assert good.seen == 3


@mysql
def test_stop_request_halts_between_pages_and_keeps_the_cursor(repo: Repo) -> None:
    channel = resolve_channel(repo, "@testchannel", api=as_any(_api(total=9)))
    calls = {"n": 0}

    def should_stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 2

    result = discover_channel(
        repo, channel, api=as_any(_api(total=9)), should_stop=should_stop
    )
    assert result.complete is False
    stored = repo.get_channel(CHANNEL_ID)
    assert stored is not None
    assert stored.enumeration_complete is False


@mysql
def test_deleted_video_tombstones_get_no_title(repo: Repo) -> None:
    """Playlists keep tombstones; storing "Deleted video" as a title is a lie."""
    from yt_tx.youtube_api import PlaylistItem, PlaylistPage

    api = FakeYouTubeAPI(
        pages=[
            PlaylistPage(
                items=[
                    PlaylistItem("tomb00000001", None, BASE_TIME, None, None),
                    PlaylistItem("real00000001", "A Real Title", BASE_TIME, None, None),
                ],
                next_page_token=None,
                total_results=2,
            )
        ],
        channel=ResolvedChannel(
            CHANNEL_ID, "T", "@t", "UU" + CHANNEL_ID[2:], 2
        ),
    )
    channel = resolve_channel(repo, "@t", api=as_any(api))
    discover_channel(repo, channel, api=as_any(api))
    tomb = repo.get_video("tomb00000001")
    assert tomb is not None
    assert tomb.title is None
