"""Duration parsing and skip rules (pure), plus batch hydration against MySQL."""

from __future__ import annotations

import pytest

from tests.conftest import status_of
from tests.fake_api import FakeYouTubeAPI, as_any, make_details
from yt_tx.classify import HardBlock
from yt_tx.hydrate import SkipRules, hydrate, to_update
from yt_tx.repo import Repo, VideoUpsert
from yt_tx.states import Status
from yt_tx.youtube_api import parse_iso8601_duration, parse_rfc3339

# --------------------------------------------------------------------------- #
# ISO-8601 durations
# --------------------------------------------------------------------------- #

DURATIONS: tuple[tuple[str | None, int | None], ...] = (
    ("PT1H2M3S", 3723),
    ("PT10M", 600),
    ("PT45S", 45),
    ("PT1H", 3600),
    ("PT2H30M", 9000),
    ("P1DT2H", 93600),
    ("P1D", 86400),
    # A finished livestream often reports P0D, which genuinely means "unknown".
    ("P0D", 0),
    ("PT0S", 0),
    # Fractional seconds appear on some Shorts.
    ("PT1M30.5S", 90),
    ("PT11H59M59S", 43199),
    (None, None),
    ("", None),
    ("garbage", None),
    ("1H2M", None),
)


@pytest.mark.parametrize("raw,expected", DURATIONS, ids=[str(d[0]) for d in DURATIONS])
def test_parse_iso8601_duration(raw: str | None, expected: int | None) -> None:
    assert parse_iso8601_duration(raw) == expected


def test_parse_rfc3339() -> None:
    parsed = parse_rfc3339("2024-03-15T10:30:00Z")
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day, parsed.hour) == (2024, 3, 15, 10)
    assert parsed.tzinfo is None, "MySQL DATETIME columns take naive UTC"
    # An offset timestamp is normalised to UTC, not stored as-is.
    offset = parse_rfc3339("2024-03-15T12:30:00+02:00")
    assert offset is not None
    assert offset.hour == 10
    assert parse_rfc3339(None) is None
    assert parse_rfc3339("not a date") is None


# --------------------------------------------------------------------------- #
# Skip rules
# --------------------------------------------------------------------------- #


def test_upcoming_is_skipped_not_failed() -> None:
    """A premiere has not happened yet; it is not a broken video."""
    rules = SkipRules()
    status, reason = rules.evaluate(make_details("v1", live="upcoming"))
    assert status is Status.SKIPPED
    assert "not happened" in (reason or "")


def test_live_now_is_skipped() -> None:
    status, reason = SkipRules().evaluate(make_details("v1", live="live"))
    assert status is Status.SKIPPED
    assert "live" in (reason or "")


def test_over_length_is_skipped() -> None:
    rules = SkipRules(max_duration_seconds=3600)
    status, reason = rules.evaluate(make_details("v1", duration_seconds=7200))
    assert status is Status.SKIPPED
    assert "7200" in (reason or "")
    assert rules.evaluate(make_details("v1", duration_seconds=3600))[0] is Status.METADATA_OK


def test_private_and_deleted_are_unavailable_not_skipped() -> None:
    """Gone beats skipped: one is permanent, the other is a filter decision."""
    assert (
        SkipRules().evaluate(make_details("v1", privacy="private"))[0]
        is Status.UNAVAILABLE
    )
    assert (
        SkipRules().evaluate(make_details("v1", upload_status="deleted"))[0]
        is Status.UNAVAILABLE
    )
    assert (
        SkipRules().evaluate(make_details("v1", upload_status="rejected"))[0]
        is Status.UNAVAILABLE
    )


def test_shorts_inclusion_is_configurable() -> None:
    short = make_details("v1", duration_seconds=45)
    assert SkipRules(include_shorts=True).evaluate(short)[0] is Status.METADATA_OK
    assert SkipRules(include_shorts=False).evaluate(short)[0] is Status.SKIPPED


def test_streams_inclusion_is_configurable() -> None:
    stream = make_details("v1", duration_seconds=7200, was_livestream=True)
    assert SkipRules(include_streams=True).evaluate(stream)[0] is Status.METADATA_OK
    assert SkipRules(include_streams=False).evaluate(stream)[0] is Status.SKIPPED


def test_short_detection_is_a_duration_heuristic() -> None:
    """Duration alone cannot prove a Short; the code treats it as a guess."""
    assert make_details("v1", duration_seconds=45).is_short_guess is True
    assert make_details("v1", duration_seconds=180).is_short_guess is True
    assert make_details("v1", duration_seconds=181).is_short_guess is False
    # A three-minute livestream is not a Short.
    assert (
        make_details("v1", duration_seconds=60, was_livestream=True).is_short_guess
        is False
    )


def test_zero_duration_livestream_is_skipped() -> None:
    status, reason = SkipRules().evaluate(
        make_details("v1", duration_seconds=0, was_livestream=True, live="none")
    )
    assert status is Status.SKIPPED
    assert "no recorded duration" in (reason or "")


def test_to_update_carries_derived_fields() -> None:
    update = to_update(
        make_details("v1", duration_seconds=45, was_livestream=False), SkipRules()
    )
    assert update.is_short is True
    assert update.was_livestream is False
    assert update.status is Status.METADATA_OK
    assert update.tags == ["testing"]


# --------------------------------------------------------------------------- #
# Batch hydration
# --------------------------------------------------------------------------- #

mysql = pytest.mark.mysql


@mysql
def test_hydration_batches_fifty_per_call(repo: Repo, channel: str) -> None:
    """One quota unit per fifty videos is the best deal in the Data API."""
    ids = [f"batch{i:07d}" for i in range(120)]
    repo.upsert_videos([VideoUpsert(video_id=v, channel_id=channel) for v in ids])
    api = FakeYouTubeAPI(details={v: make_details(v) for v in ids})

    result = hydrate(repo, api=as_any(api), rules=SkipRules())

    assert result.requested == 120
    assert result.hydrated == 120
    assert result.calls == 3, "120 videos should cost 3 units, not 120"
    assert all(len(batch) <= 50 for batch in api.detail_requests)
    assert repo.status_counts() == {Status.METADATA_OK.value: 120}


@mysql
def test_hydration_applies_skip_rules(repo: Repo, channel: str) -> None:
    repo.upsert_videos(
        [
            VideoUpsert(video_id="hydlong00001", channel_id=channel),
            VideoUpsert(video_id="hydshort0001", channel_id=channel),
            VideoUpsert(video_id="hydlive00001", channel_id=channel),
            VideoUpsert(video_id="hydok0000001", channel_id=channel),
        ]
    )
    api = FakeYouTubeAPI(
        details={
            "hydlong00001": make_details("hydlong00001", duration_seconds=50_000),
            "hydshort0001": make_details("hydshort0001", duration_seconds=30),
            "hydlive00001": make_details("hydlive00001", live="live"),
            "hydok0000001": make_details("hydok0000001", duration_seconds=600),
        }
    )
    result = hydrate(
        repo,
        api=as_any(api),
        rules=SkipRules(max_duration_seconds=43200, include_shorts=False),
    )
    assert result.hydrated == 1
    assert result.skipped == 3
    counts = repo.status_counts()
    assert counts[Status.METADATA_OK.value] == 1
    assert counts[Status.SKIPPED.value] == 3
    # Reasons are aggregated so an operator can see *why* things were skipped.
    assert len(result.reasons) == 3


@mysql
def test_ids_the_api_declines_become_unavailable(repo: Repo, channel: str) -> None:
    """A deleted video is simply absent from the response; diffing is the only clue."""
    repo.upsert_videos(
        [
            VideoUpsert(video_id="hydgone00001", channel_id=channel),
            VideoUpsert(video_id="hydhere00001", channel_id=channel),
        ]
    )
    api = FakeYouTubeAPI(details={"hydhere00001": make_details("hydhere00001")})

    result = hydrate(repo, api=as_any(api), rules=SkipRules())
    assert result.unavailable == 1
    assert result.hydrated == 1

    gone = repo.get_video("hydgone00001")
    assert gone is not None
    assert gone.status is Status.UNAVAILABLE
    assert "not returned" in (gone.status_reason or "")


@mysql
def test_a_block_never_marks_videos_unavailable(
    repo: Repo, channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for a bug found by running hydration for real.

    The yt-dlp path used to swallow every ``DownloadError`` and ``continue``,
    which left the id absent from the batch and got it marked ``unavailable`` by
    the caller. One bot check therefore permanently retired every video it
    touched - a terminal status that is never revisited - with no way to tell
    them apart from genuinely deleted videos.
    """
    import yt_tx.hydrate as hydrate_module
    from yt_dlp.utils import DownloadError

    ids = [f"blocked{i:05d}" for i in range(4)]
    repo.upsert_videos([VideoUpsert(video_id=v, channel_id=channel) for v in ids])

    def blocked_extract(self: object, url: str, download: bool = False) -> None:
        raise DownloadError(
            "ERROR: [youtube] x: Sign in to confirm you’re not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication."
        )

    from yt_dlp import YoutubeDL

    monkeypatch.setattr(YoutubeDL, "extract_info", blocked_extract)

    with pytest.raises(HardBlock):
        hydrate(repo, api=None, rules=SkipRules())

    # Not one row concluded anything. Everything is still queued.
    assert repo.status_counts() == {Status.DISCOVERED.value: 4}
    for video_id in ids:
        row = repo.get_video(video_id)
        assert row is not None
        assert row.status is Status.DISCOVERED, "a block poisoned a video row"


@mysql
def test_a_transient_ytdlp_failure_defers_rather_than_retiring(
    repo: Repo, channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retryable failure must leave the video queued, not mark it gone."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    repo.upsert_videos([VideoUpsert(video_id="deferme00001", channel_id=channel)])

    def flaky(self: object, url: str, download: bool = False) -> None:
        raise DownloadError("ERROR: [youtube] x: Unable to download webpage: timed out")

    monkeypatch.setattr(YoutubeDL, "extract_info", flaky)
    result = hydrate(repo, api=None, rules=SkipRules())

    assert result.deferred == 1
    assert result.unavailable == 0
    assert status_of(repo, "deferme00001") is Status.DISCOVERED


@mysql
def test_a_genuinely_gone_video_is_still_marked_unavailable(
    repo: Repo, channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferral fix must not stop real deletions being recorded."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    repo.upsert_videos([VideoUpsert(video_id="deleted00001", channel_id=channel)])

    def gone(self: object, url: str, download: bool = False) -> None:
        raise DownloadError("ERROR: [youtube] deleted00001: Video unavailable")

    monkeypatch.setattr(YoutubeDL, "extract_info", gone)
    result = hydrate(repo, api=None, rules=SkipRules())

    assert result.unavailable == 1
    assert result.deferred == 0
    assert status_of(repo, "deleted00001") is Status.UNAVAILABLE


@mysql
def test_an_entirely_empty_api_batch_is_treated_as_systemic(
    repo: Repo, channel: str
) -> None:
    """Fifty ids missing at once is not fifty deletions.

    It is an auth, quota or network problem, and marking them all unavailable
    would retire a whole channel over a transient fault.
    """
    ids = [f"empty{i:07d}" for i in range(10)]
    repo.upsert_videos([VideoUpsert(video_id=v, channel_id=channel) for v in ids])
    api = FakeYouTubeAPI(details={})  # returns nothing for anything

    with pytest.raises(HardBlock) as info:
        hydrate(repo, api=as_any(api), rules=SkipRules())
    assert "systemic" in str(info.value)
    assert repo.status_counts() == {Status.DISCOVERED.value: 10}


@mysql
def test_a_single_missing_id_is_still_unavailable(repo: Repo, channel: str) -> None:
    """The systemic guard must not mask an ordinary deleted video."""
    repo.upsert_videos([VideoUpsert(video_id="onegone00001", channel_id=channel)])
    api = FakeYouTubeAPI(details={})
    result = hydrate(repo, api=as_any(api), rules=SkipRules())
    assert result.unavailable == 1
    assert status_of(repo, "onegone00001") is Status.UNAVAILABLE


@mysql
def test_deferred_videos_do_not_make_hydration_spin(
    repo: Repo, channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deferred ids stay `discovered`, so the query must exclude them.

    Without the exclusion the same rows come straight back at the top of the next
    query and hydrate loops forever.
    """
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    ids = [f"spin{i:08d}" for i in range(5)]
    repo.upsert_videos([VideoUpsert(video_id=v, channel_id=channel) for v in ids])

    attempts = {"n": 0}

    def flaky(self: object, url: str, download: bool = False) -> None:
        attempts["n"] += 1
        if attempts["n"] > 50:
            raise AssertionError("hydrate is spinning on deferred videos")
        raise DownloadError("ERROR: [youtube] x: Unable to download webpage: timed out")

    monkeypatch.setattr(YoutubeDL, "extract_info", flaky)
    result = hydrate(repo, api=None, rules=SkipRules())

    assert result.deferred == 5
    assert attempts["n"] == 5, "each video should be attempted exactly once"


@mysql
def test_hydration_is_idempotent_and_does_not_loop(repo: Repo, channel: str) -> None:
    """The claim query must actually shrink, or hydrate spins forever."""
    repo.upsert_videos([VideoUpsert(video_id="hydonce00001", channel_id=channel)])
    api = FakeYouTubeAPI(details={"hydonce00001": make_details("hydonce00001")})

    first = hydrate(repo, api=as_any(api), rules=SkipRules())
    second = hydrate(repo, api=as_any(api), rules=SkipRules())
    assert first.requested == 1
    assert second.requested == 0, "nothing left in discovered"


@mysql
def test_hydration_respects_limit(repo: Repo, channel: str) -> None:
    ids = [f"hydlim{i:06d}" for i in range(10)]
    repo.upsert_videos([VideoUpsert(video_id=v, channel_id=channel) for v in ids])
    api = FakeYouTubeAPI(details={v: make_details(v) for v in ids})
    result = hydrate(repo, api=as_any(api), rules=SkipRules(), limit=4)
    assert result.requested == 4
    assert len(repo.videos_needing_metadata(limit=100)) == 6


@mysql
def test_hydration_scoped_to_one_channel(repo: Repo, channel: str) -> None:
    repo.upsert_channel(channel_id="UCother2", input_ref="@other2", handle="@other2")
    repo.upsert_videos(
        [
            VideoUpsert(video_id="hydmine00001", channel_id=channel),
            VideoUpsert(video_id="hydtheir0001", channel_id="UCother2"),
        ]
    )
    api = FakeYouTubeAPI(
        details={
            "hydmine00001": make_details("hydmine00001"),
            "hydtheir0001": make_details("hydtheir0001"),
        }
    )
    hydrate(repo, api=as_any(api), rules=SkipRules(), channel_id=channel)
    assert repo.get_video("hydmine00001").status is Status.METADATA_OK  # type: ignore[union-attr]
    assert repo.get_video("hydtheir0001").status is Status.DISCOVERED  # type: ignore[union-attr]


@mysql
def test_stop_request_halts_hydration(repo: Repo, channel: str) -> None:
    ids = [f"hydstop{i:05d}" for i in range(60)]
    repo.upsert_videos([VideoUpsert(video_id=v, channel_id=channel) for v in ids])
    api = FakeYouTubeAPI(details={v: make_details(v) for v in ids})
    result = hydrate(
        repo, api=as_any(api), rules=SkipRules(), should_stop=lambda: True
    )
    assert result.requested == 0
    assert api.calls == []
