"""Opt-in live-network tests. ``pytest -m integration``.

Deliberately excluded from the default run: these hit YouTube, so they are slow,
they consume the same IP reputation the harvester depends on, and they fail for
reasons unrelated to this code (a video gets deleted, captions get switched off,
the datacenter IP gets blocked). Everything in the default suite is
fixture-driven for exactly that reason.

Run them when you have changed a fetcher or bumped a pinned dependency, which is
when the shape of YouTube's responses is the thing actually under test.

The three ids below cover the three outcomes that matter:

* manual captions          -> ``transcript_ok``
* auto-generated captions  -> ``transcript_ok`` with ``kind='asr'``
* captions disabled        -> ``no_transcript`` with ``needs_audio=1``

If one of them changes character, fix the id rather than the assertion.
"""

from __future__ import annotations

import os

import pytest

from yt_tx.classify import HardBlock, classify
from yt_tx.fetch import (
    SOURCE_YTA,
    SOURCE_YTDLP,
    NoTranscriptInLanguages,
    flatten,
    make_fetcher,
    select_transcripts,
)
from yt_tx.states import TranscriptKind

pytestmark = pytest.mark.integration

# TED talks carry human-written captions and are unusually stable.
MANUAL_CAPTIONS = "8S0FDjFBj8o"
# Rick Astley: auto-captioned, and about as unlikely to disappear as anything.
ASR_CAPTIONS = "dQw4w9WgXcQ"
# Google's own "Me at the zoo"-era upload with captions off.
NO_CAPTIONS = "jNQXAC9IVRw"

BACKENDS = [SOURCE_YTA, SOURCE_YTDLP]


def _skip_if_blocked(exc: BaseException) -> None:
    """A datacenter IP block is an environment problem, not a test failure."""
    result = classify(exc)
    if result.is_block or isinstance(exc, HardBlock):
        pytest.skip(
            f"YouTube is blocking this IP ({result.error_type}); "
            "integration tests need a residential IP or a proxy"
        )


@pytest.fixture(params=BACKENDS)
def fetcher(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    proxy = os.environ.get("YT_TX_TEST_PROXY")
    backend = make_fetcher(request.param, proxy=proxy)
    yield backend
    backend.close()


def test_lists_manual_captions(fetcher) -> None:  # type: ignore[no-untyped-def]
    try:
        available = fetcher.list_available(MANUAL_CAPTIONS)
    except Exception as exc:
        _skip_if_blocked(exc)
        raise
    assert available, "expected at least one caption track"
    assert any(a.kind is TranscriptKind.MANUAL for a in available), (
        f"{MANUAL_CAPTIONS} was chosen for its human-written captions; "
        "if that changed, pick a different id"
    )


def test_downloads_and_normalises_manual_captions(fetcher) -> None:  # type: ignore[no-untyped-def]
    try:
        available = fetcher.list_available(MANUAL_CAPTIONS)
        selection = select_transcripts(available, languages=["en"])[0]
        segments = fetcher.download(MANUAL_CAPTIONS, selection)
    except Exception as exc:
        _skip_if_blocked(exc)
        raise

    assert len(segments) > 10
    assert all(s.duration >= 0 for s in segments)
    assert segments == sorted(segments, key=lambda s: s.start)
    text = flatten(segments)
    assert len(text.split()) > 100


def test_asr_only_video(fetcher) -> None:  # type: ignore[no-untyped-def]
    try:
        available = fetcher.list_available(ASR_CAPTIONS)
    except Exception as exc:
        _skip_if_blocked(exc)
        raise
    assert available
    selection = select_transcripts(available, languages=["en"])[0]
    segments = fetcher.download(ASR_CAPTIONS, selection)
    assert segments
    assert flatten(segments)


def test_captions_disabled_classifies_as_no_transcript(fetcher) -> None:  # type: ignore[no-untyped-def]
    """The distinction the whole classifier exists to draw."""
    from yt_tx.states import Outcome, Status

    try:
        available = fetcher.list_available(NO_CAPTIONS)
    except Exception as exc:
        _skip_if_blocked(exc)
        result = classify(exc)
        assert result.outcome is Outcome.TERMINAL, (
            f"{type(exc).__name__} should be terminal, not {result.outcome}"
        )
        assert result.status is Status.NO_TRANSCRIPT
        assert result.needs_audio is True
        return

    # Some backends list nothing rather than raising; that is equally valid.
    with pytest.raises(NoTranscriptInLanguages):
        select_transcripts(available, languages=["en"])


def test_both_backends_agree_on_what_exists() -> None:
    """If the two backends disagree about a video, one of them has drifted."""
    proxy = os.environ.get("YT_TX_TEST_PROXY")
    inventories: dict[str, set[str]] = {}
    for backend_name in BACKENDS:
        backend = make_fetcher(backend_name, proxy=proxy)
        try:
            available = backend.list_available(MANUAL_CAPTIONS)
            inventories[backend_name] = {
                a.language_code.split("-")[0] for a in available
            }
        except Exception as exc:
            _skip_if_blocked(exc)
            pytest.skip(f"{backend_name} failed: {type(exc).__name__}: {exc}")
        finally:
            backend.close()

    yta, ytdlp = inventories[SOURCE_YTA], inventories[SOURCE_YTDLP]
    assert yta & ytdlp, (
        f"backends share no languages at all: {sorted(yta)} vs {sorted(ytdlp)}"
    )


def test_rss_feed_is_readable() -> None:
    """The free enumeration path an incremental cron depends on."""
    from yt_tx.youtube_api import fetch_rss_latest

    # Google Developers: large, public, and not going anywhere.
    entries = fetch_rss_latest("UC_x5XG1OV2P6uZZ5FSM9Ttw")
    assert entries
    assert all(len(e.video_id) == 11 for e in entries)
    assert any(e.published_at is not None for e in entries)
