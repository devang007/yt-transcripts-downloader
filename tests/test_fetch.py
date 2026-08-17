"""Selection, normalisation, durable storage, and the per-video pipeline.

The storage tests are worth more than they look. Writing the gzip and fsyncing it
*before* committing the row is what makes a power cut leave an unreferenced file
rather than a row pointing at nothing, and determinism of the gzip is what keeps
the stored sha256 verifiable by ``doctor`` afterwards.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from youtube_transcript_api import IpBlocked, TranscriptsDisabled, VideoUnavailable

from tests.fake_api import (
    FakeFetcher,
    as_any,
    english_manual,
    japanese_only,
    sample_segments,
)
from yt_tx.classify import HardBlock
from yt_tx.fetch import (
    Available,
    FetchConfig,
    NoTranscriptInLanguages,
    Segment,
    _parse_json3,
    _parse_srv,
    _parse_vtt,
    covered_seconds,
    fetch_video,
    flatten,
    normalise_segments,
    read_transcript_file,
    select_transcripts,
    transcript_path,
    verify_transcript_file,
    word_count,
    write_transcript_file,
)
from yt_tx.repo import Repo
from yt_tx.states import Status, TranscriptKind

# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_configured_language_order_wins() -> None:
    available = [
        Available("hi", "Hindi", TranscriptKind.MANUAL),
        Available("en", "English", TranscriptKind.MANUAL),
    ]
    picked = select_transcripts(available, languages=["en", "hi"])
    assert picked[0].language_code == "en"
    picked = select_transcripts(available, languages=["hi", "en"])
    assert picked[0].language_code == "hi"


def test_manual_beats_asr_within_a_language() -> None:
    available = [
        Available("en", "English (auto)", TranscriptKind.ASR),
        Available("en", "English", TranscriptKind.MANUAL),
    ]
    assert select_transcripts(available, languages=["en"])[0].kind is TranscriptKind.MANUAL
    assert (
        select_transcripts(available, languages=["en"], prefer_manual=False)[0].kind
        is not None
    )


def test_base_language_match_is_accepted() -> None:
    """Configured ``en`` must accept ``en-GB``.

    Uploads are tagged inconsistently. An exact-only match would file thousands
    of perfectly good English videos as ``lang_missing``.
    """
    available = [Available("en-GB", "English (UK)", TranscriptKind.MANUAL)]
    assert select_transcripts(available, languages=["en"])[0].language_code == "en-GB"


def test_exact_match_preferred_over_base_match() -> None:
    available = [
        Available("en-GB", "English (UK)", TranscriptKind.MANUAL),
        Available("en", "English", TranscriptKind.MANUAL),
    ]
    assert select_transcripts(available, languages=["en"])[0].language_code == "en"


def test_no_match_raises_for_lang_missing() -> None:
    with pytest.raises(NoTranscriptInLanguages) as info:
        select_transcripts(japanese_only(), languages=["en", "hi"])
    assert "ja" in str(info.value)


def test_empty_track_list_raises() -> None:
    with pytest.raises(NoTranscriptInLanguages):
        select_transcripts([], languages=["en"])


def test_translation_only_when_enabled() -> None:
    with pytest.raises(NoTranscriptInLanguages):
        select_transcripts(japanese_only(), languages=["en"], accept_translated=False)

    picked = select_transcripts(
        japanese_only(), languages=["en"], accept_translated=True
    )
    assert picked[0].translate_to == "en"
    assert picked[0].stored_language == "en"
    assert picked[0].stored_kind is TranscriptKind.TRANSLATED


def test_translation_respects_available_targets() -> None:
    available = [
        Available(
            "ja", "Japanese", TranscriptKind.ASR,
            is_translatable=True, translation_targets=("fr", "de"),
        )
    ]
    with pytest.raises(NoTranscriptInLanguages):
        select_transcripts(available, languages=["en"], accept_translated=True)


def test_untranslatable_track_is_not_translated() -> None:
    available = [Available("ja", "Japanese", TranscriptKind.MANUAL, is_translatable=False)]
    with pytest.raises(NoTranscriptInLanguages):
        select_transcripts(available, languages=["en"], accept_translated=True)


def test_store_all_variants_marks_exactly_one_preferred() -> None:
    picked = select_transcripts(
        english_manual(), languages=["en"], store_all_variants=True
    )
    assert len(picked) == 2
    assert sum(1 for p in picked if p.is_preferred) == 1
    assert picked[0].is_preferred is True


def test_single_variant_by_default() -> None:
    assert len(select_transcripts(english_manual(), languages=["en"])) == 1


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def test_normalise_collapses_whitespace_and_sorts() -> None:
    segments = normalise_segments(
        [
            {"text": "second", "start": 5.0, "duration": 1.0},
            {"text": "  first\nline  ", "start": 0.0, "duration": 2.0},
            {"text": "   ", "start": 3.0, "duration": 1.0},  # empty: dropped
            {"text": "bad start", "start": "nonsense", "duration": 1.0},  # dropped
        ]
    )
    assert [s.text for s in segments] == ["first line", "second"]
    assert segments[0].start == 0.0


def test_normalise_clamps_negatives() -> None:
    segments = normalise_segments([{"text": "x", "start": -3.0, "duration": -1.0}])
    assert segments[0].start == 0.0
    assert segments[0].duration == 0.0


def test_flatten_drops_sound_effect_cues() -> None:
    text = flatten(sample_segments())
    assert "[Music]" not in text
    assert text == "Hello and welcome to the show Today we discuss mitochondria"


def test_word_count() -> None:
    assert word_count("one two three") == 3
    assert word_count("") == 0


def test_covered_seconds_merges_overlaps() -> None:
    """ASR cues overlap constantly; summing durations can exceed the video length."""
    overlapping = [
        Segment(0.0, 5.0, "a"),
        Segment(2.0, 5.0, "b"),  # overlaps a
        Segment(20.0, 3.0, "c"),  # separate island
    ]
    # Naive sum would be 13; the true union is 7 + 3.
    assert covered_seconds(overlapping) == pytest.approx(10.0)
    assert covered_seconds([]) == 0.0


def test_covered_seconds_contiguous() -> None:
    assert covered_seconds(sample_segments()) == pytest.approx(11.5)


# --------------------------------------------------------------------------- #
# Caption format parsers
# --------------------------------------------------------------------------- #


def test_parse_json3() -> None:
    body = json.dumps(
        {
            "events": [
                {"tStartMs": 0, "dDurationMs": 2500,
                 "segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
                {"tStartMs": 2500, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
                {"tStartMs": 3500, "dDurationMs": 1500, "segs": [{"utf8": "again"}]},
                {"tStartMs": 5000},  # no segs at all
            ]
        }
    )
    segments = normalise_segments(_parse_json3(body))
    assert [s.text for s in segments] == ["Hello world", "again"]
    assert segments[0].duration == pytest.approx(2.5)


def test_parse_vtt() -> None:
    body = """WEBVTT

00:00:00.000 --> 00:00:02.500
Hello <c>world</c>

00:00:02.500 --> 00:00:05.000
second line
continued
"""
    segments = normalise_segments(_parse_vtt(body))
    assert [s.text for s in segments] == ["Hello world", "second line continued"]
    assert segments[1].start == pytest.approx(2.5)


def test_parse_srv() -> None:
    body = '<transcript><text start="0" dur="2.5">Hello</text>' \
           '<text start="2.5" dur="1.5">world</text></transcript>'
    segments = normalise_segments(_parse_srv(body))
    assert [s.text for s in segments] == ["Hello", "world"]
    assert segments[1].start == pytest.approx(2.5)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def test_write_transcript_file_round_trip(tmp_path: Path) -> None:
    path = transcript_path(tmp_path, "UCchan", "vid123", "en", TranscriptKind.MANUAL)
    assert path.name == "vid123.en.manual.json.gz"

    stored = write_transcript_file(
        path,
        video_id="vid123",
        language="en",
        kind=TranscriptKind.MANUAL,
        source="test",
        segments=sample_segments(),
    )
    assert path.exists()
    assert stored.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert verify_transcript_file(path, stored.sha256) is True

    payload = read_transcript_file(path)
    assert payload["video_id"] == "vid123"
    assert payload["segment_count"] == 4
    assert payload["segments"][0]["text"] == "Hello and welcome"


def test_gzip_is_deterministic(tmp_path: Path) -> None:
    """``mtime=0`` keeps the hash stable, so ``doctor`` can verify it later.

    With the default gzip header the stored sha256 would change on every rewrite
    purely because the clock moved, making integrity checks useless.
    """
    first = write_transcript_file(
        tmp_path / "a.json.gz", video_id="v", language="en",
        kind=TranscriptKind.MANUAL, source="t", segments=sample_segments(),
    )
    second = write_transcript_file(
        tmp_path / "b.json.gz", video_id="v", language="en",
        kind=TranscriptKind.MANUAL, source="t", segments=sample_segments(),
    )
    assert first.sha256 == second.sha256


def test_write_leaves_no_temp_files_behind(tmp_path: Path) -> None:
    write_transcript_file(
        tmp_path / "x.json.gz", video_id="v", language="en",
        kind=TranscriptKind.MANUAL, source="t", segments=sample_segments(),
    )
    assert list(tmp_path.glob(".*.tmp")) == []
    assert len(list(tmp_path.iterdir())) == 1


def test_write_is_atomic_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed write must not replace a good file with a truncated one."""
    path = tmp_path / "atomic.json.gz"
    write_transcript_file(
        path, video_id="v", language="en", kind=TranscriptKind.MANUAL,
        source="t", segments=[Segment(0.0, 1.0, "original")],
    )
    original = path.read_bytes()

    import os as os_module

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os_module, "replace", boom)
    with pytest.raises(OSError):
        write_transcript_file(
            path, video_id="v", language="en", kind=TranscriptKind.MANUAL,
            source="t", segments=[Segment(0.0, 1.0, "replacement")],
        )
    assert path.read_bytes() == original, "the good file must survive"
    assert list(tmp_path.glob(".*.tmp")) == [], "the temp file must be cleaned up"


def test_verify_detects_tampering(tmp_path: Path) -> None:
    path = tmp_path / "t.json.gz"
    stored = write_transcript_file(
        path, video_id="v", language="en", kind=TranscriptKind.MANUAL,
        source="t", segments=sample_segments(),
    )
    path.write_bytes(gzip.compress(b'{"segments": []}'))
    assert verify_transcript_file(path, stored.sha256) is False
    assert verify_transcript_file(tmp_path / "gone.gz", stored.sha256) is False


def test_language_code_is_sanitised_for_the_filesystem(tmp_path: Path) -> None:
    path = transcript_path(tmp_path, "UCc", "v", "../../etc/passwd", TranscriptKind.ASR)
    assert ".." not in path.name
    assert path.parent == tmp_path / "UCc"


# --------------------------------------------------------------------------- #
# The per-video pipeline
# --------------------------------------------------------------------------- #

mysql = pytest.mark.mysql


def config(tmp_path: Path, **kwargs: object) -> FetchConfig:
    defaults: dict[str, object] = {
        "transcript_dir": tmp_path,
        "languages": ["en", "hi"],
        "max_attempts": 3,
        "backoff_base_seconds": 0.001,
        "backoff_cap_seconds": 0.01,
    }
    defaults.update(kwargs)
    return FetchConfig(**defaults)  # type: ignore[arg-type]


@mysql
def test_successful_fetch_commits_everything(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(available=english_manual(), segments=sample_segments())

    outcome = fetch_video(
        repo, as_any(fetcher), video, config(tmp_path), run_id=None, worker="w1"
    )
    assert outcome.status is Status.TRANSCRIPT_OK
    assert outcome.transcripts == 1

    row = repo.get_video(video.video_id)
    assert row is not None
    assert row.status is Status.TRANSCRIPT_OK
    assert row.claimed_by is None
    # The inventory is stored even though only one variant was downloaded.
    assert {a["language_code"] for a in row.available_transcripts} == {"en", "es"}

    transcripts = repo.list_transcripts(video.video_id)
    assert len(transcripts) == 1
    assert transcripts[0].language_code == "en"
    assert transcripts[0].kind is TranscriptKind.MANUAL
    assert transcripts[0].word_count == 10
    assert Path(transcripts[0].raw_path).exists()
    assert verify_transcript_file(
        Path(transcripts[0].raw_path), transcripts[0].raw_sha256
    )


@mysql
def test_idempotent_rerun_downloads_nothing(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    """The whole point of the project: a second run must not redo the first."""
    fetcher = FakeFetcher(available=english_manual(), segments=sample_segments())
    conf = config(tmp_path)

    first_batch = repo.claim_batch("w1", limit=3)
    for video in first_batch:
        fetch_video(repo, as_any(fetcher), video, conf, run_id=None, worker="w1")

    assert len(fetcher.download_calls) == 3
    before = repo.status_counts()
    transcripts_before = repo.stats().transcripts

    # Second pass: nothing is claimable, so nothing is fetched.
    assert repo.claim_batch("w1", limit=10) == []
    assert len(fetcher.download_calls) == 3, "a re-run must not re-download"
    assert repo.status_counts() == before
    assert repo.stats().transcripts == transcripts_before


@mysql
def test_captions_disabled_becomes_no_transcript_and_queues_audio(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(list_error=TranscriptsDisabled(video.video_id))

    outcome = fetch_video(
        repo, as_any(fetcher), video, config(tmp_path), run_id=None, worker="w1"
    )
    assert outcome.status is Status.NO_TRANSCRIPT

    row = repo.get_video(video.video_id)
    assert row is not None
    assert row.status is Status.NO_TRANSCRIPT
    assert row.needs_audio is True, "this is the phase-2 queue"


@mysql
def test_wrong_language_becomes_lang_missing_and_records_what_exists(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    """The distinction that makes the corpus diagnosable months later."""
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(available=japanese_only())

    outcome = fetch_video(
        repo, as_any(fetcher), video, config(tmp_path), run_id=None, worker="w1"
    )
    assert outcome.status is Status.LANG_MISSING

    row = repo.get_video(video.video_id)
    assert row is not None
    assert row.status is Status.LANG_MISSING
    assert row.needs_audio is False, "text exists; this is a translation problem"
    assert row.available_transcripts[0]["language_code"] == "ja"
    assert fetcher.download_calls == [], "nothing should have been downloaded"


@mysql
def test_translation_rescues_a_lang_missing_video(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(available=japanese_only(), segments=sample_segments())

    outcome = fetch_video(
        repo, as_any(fetcher), video,
        config(tmp_path, accept_translated=True),
        run_id=None, worker="w1",
    )
    assert outcome.status is Status.TRANSCRIPT_OK
    transcripts = repo.list_transcripts(video.video_id)
    assert transcripts[0].kind is TranscriptKind.TRANSLATED
    assert transcripts[0].language_code == "en"


@mysql
def test_block_raises_and_leaves_the_video_untouched(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    """The invariant the design turns on, exercised end to end."""
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(list_error=IpBlocked(video.video_id))

    with pytest.raises(HardBlock):
        fetch_video(
            repo, as_any(fetcher), video, config(tmp_path), run_id=None, worker="w1"
        )

    row = repo.get_video(video.video_id)
    assert row is not None
    assert row.status is Status.METADATA_OK, "a block must not change status"
    assert row.attempts == 0, "a block must not consume an attempt"
    assert row.claimed_by is None, "but it must go back on the queue"
    assert repo.recent_attempts(video.video_id)[0]["outcome"] == "blocked"
    assert len(repo.claim_batch("w2", limit=1)) == 1


@mysql
def test_unavailable_is_terminal(repo: Repo, seeded: str, tmp_path: Path) -> None:
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(list_error=VideoUnavailable(video.video_id))
    outcome = fetch_video(
        repo, as_any(fetcher), video, config(tmp_path), run_id=None, worker="w1"
    )
    assert outcome.status is Status.UNAVAILABLE
    # Terminal means never claimed again. The other seeded videos still are.
    assert video.video_id not in {v.video_id for v in repo.claim_batch("w2", limit=5)}


@mysql
def test_transient_failure_retries_then_fails(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    import requests

    conf = config(tmp_path, max_attempts=2)
    fetcher = FakeFetcher(list_error=requests.exceptions.ReadTimeout("timed out"))

    video = repo.claim_batch("w1", limit=1)[0]
    first = fetch_video(repo, as_any(fetcher), video, conf, run_id=None, worker="w1")
    assert first.status is Status.RETRY

    with repo.begin() as conn:
        from sqlalchemy import text

        conn.execute(
            text("UPDATE videos SET next_attempt_at = NULL WHERE video_id = :v"),
            {"v": video.video_id},
        )
    again = repo.claim_batch("w1", limit=1)[0]
    second = fetch_video(repo, as_any(fetcher), again, conf, run_id=None, worker="w1")
    assert second.status is Status.FAILED


@mysql
def test_empty_transcript_is_treated_as_no_transcript(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    """A track that lists but downloads empty is not a success."""
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(available=english_manual(), empty_download=True)
    outcome = fetch_video(
        repo, as_any(fetcher), video, config(tmp_path), run_id=None, worker="w1"
    )
    assert outcome.status is Status.NO_TRANSCRIPT
    assert repo.list_transcripts(video.video_id) == []


@mysql
def test_store_all_variants_writes_every_track(
    repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(available=english_manual(), segments=sample_segments())
    fetch_video(
        repo, as_any(fetcher), video,
        config(tmp_path, store_all_variants=True),
        run_id=None, worker="w1",
    )
    transcripts = repo.list_transcripts(video.video_id)
    assert len(transcripts) == 2
    assert sum(1 for t in transcripts if t.is_preferred) == 1
    for transcript in transcripts:
        assert Path(transcript.raw_path).exists()


@mysql
def test_file_is_written_before_the_row_is_committed(
    repo: Repo, seeded: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering check: a DB failure must leave a file, never a row without one.

    An orphan file is reclaimable by ``doctor``; a row pointing at a missing file
    is corruption that every downstream consumer trips over.
    """
    video = repo.claim_batch("w1", limit=1)[0]
    fetcher = FakeFetcher(available=english_manual(), segments=sample_segments())

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database went away mid-commit")

    monkeypatch.setattr(repo, "record_transcript_success", explode)
    fetch_video(
        repo, as_any(fetcher), video, config(tmp_path), run_id=None, worker="w1"
    )

    files = list(tmp_path.rglob("*.json.gz"))
    assert len(files) == 1, "the file should already be on disk"
    assert repo.list_transcripts(video.video_id) == [], "but no row should exist"
