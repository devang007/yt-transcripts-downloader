"""Table-driven tests for the error classifier - one case per row of the spec.

Every exception here is a *real instance from the pinned library*, constructed
with the real constructor signature, not a stand-in. That is the whole point:
the classifier's contract is with youtube-transcript-api 1.2.4 as installed, and
a version bump that renames or re-parents an exception must fail here loudly
rather than silently reclassifying thousands of videos as ``failed``.

Facts about 1.2.4 that these tests pin down, and that differ from a naive
reading of the library's docs:

* ``IpBlocked`` is a *subclass* of ``RequestBlocked``, so ordering in the
  dispatch table matters.
* There is no ``TooManyRequests`` class. HTTP 429 arrives wrapped in
  ``YouTubeRequestFailed``, which keeps only ``str(http_error)`` and discards
  the response object - the status code has to be recovered from that string.
* ``AgeRestricted`` and ``PoTokenRequired`` descend directly from
  ``CouldNotRetrieveTranscript``, not from ``VideoUnplayable``.
* Members-only videos arrive as ``VideoUnplayable`` with a reason string.
"""

from __future__ import annotations

import json
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

import pytest
import requests
from sqlalchemy.exc import OperationalError
from youtube_transcript_api import (
    AgeRestricted,
    CookieInvalid,
    CookiePathInvalid,
    CouldNotRetrieveTranscript,
    FailedToCreateConsentCookie,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    NotTranslatable,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    TranslationLanguageNotAvailable,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
)
from youtube_transcript_api._transcripts import TranscriptList

from yt_tx.classify import HardBlock, classify, describe_exception
from yt_tx.states import Outcome, Status

VIDEO = "dQw4w9WgXcQ"


# --------------------------------------------------------------------------- #
# Builders for real exception instances
# --------------------------------------------------------------------------- #


def empty_transcript_list(video_id: str = VIDEO) -> TranscriptList:
    return TranscriptList(video_id, {}, {}, [])


def http_error(status: int, reason: str = "") -> requests.exceptions.HTTPError:
    """A genuine HTTPError as ``raise_for_status`` would produce it."""
    response = requests.Response()
    response.status_code = status
    response.reason = reason or {
        429: "Too Many Requests",
        500: "Internal Server Error",
        503: "Service Unavailable",
        403: "Forbidden",
    }.get(status, "Error")
    response.url = f"https://www.youtube.com/watch?v={VIDEO}"
    try:
        response.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        return exc
    raise AssertionError(f"status {status} is not an error")


def mysql_operational_error(errno: int) -> OperationalError:
    """SQLAlchemy-wrapped PyMySQL error, shaped exactly as the driver raises it."""
    import pymysql

    messages = {
        1213: "Deadlock found when trying to get lock; try restarting transaction",
        1205: "Lock wait timeout exceeded; try restarting transaction",
    }
    orig = pymysql.err.OperationalError(errno, messages.get(errno, "err"))
    return OperationalError("UPDATE videos SET status=%s", {}, orig)


def json_decode_error() -> json.JSONDecodeError:
    try:
        json.loads("{not json")
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError("unreachable")


def xml_parse_error() -> ET.ParseError:
    try:
        ET.fromstring("<transcript><text>unclosed")
    except ET.ParseError as exc:
        return exc
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Case:
    name: str
    build: Callable[[], BaseException]
    outcome: Outcome
    status: Status | None
    retryable: bool
    needs_audio: bool = False
    http_status: int | None = None
    max_attempts: int | None = None
    cookies_configured: bool = False


CASES: tuple[Case, ...] = (
    # -- captions disabled ------------------------------------------------- #
    Case(
        "captions_disabled",
        lambda: TranscriptsDisabled(VIDEO),
        Outcome.TERMINAL, Status.NO_TRANSCRIPT, False, needs_audio=True,
    ),
    # -- wrong language ---------------------------------------------------- #
    # needs_audio stays 0 here: the video *has* text, just not in a configured
    # language. The fix is translation, not a GPU. Only genuinely text-less
    # videos belong in the phase-2 audio queue.
    Case(
        "no_transcript_in_requested_languages",
        lambda: NoTranscriptFound(VIDEO, ["en", "hi"], empty_transcript_list()),
        Outcome.TERMINAL, Status.LANG_MISSING, False,
    ),
    Case(
        "not_translatable",
        lambda: NotTranslatable(VIDEO),
        Outcome.TERMINAL, Status.LANG_MISSING, False,
    ),
    Case(
        "translation_language_unavailable",
        lambda: TranslationLanguageNotAvailable(VIDEO),
        Outcome.TERMINAL, Status.LANG_MISSING, False,
    ),
    # -- gone -------------------------------------------------------------- #
    Case(
        "video_unavailable",
        lambda: VideoUnavailable(VIDEO),
        Outcome.TERMINAL, Status.UNAVAILABLE, False,
    ),
    Case(
        "invalid_video_id",
        lambda: InvalidVideoId(VIDEO),
        Outcome.TERMINAL, Status.UNAVAILABLE, False,
    ),
    Case(
        "members_only",
        lambda: VideoUnplayable(
            VIDEO,
            "This video is available to this channel's members on level: "
            "Supporter (or any higher level).",
            [],
        ),
        Outcome.TERMINAL, Status.UNAVAILABLE, False,
    ),
    Case(
        "unplayable_no_reason",
        lambda: VideoUnplayable(VIDEO, None, []),
        Outcome.TERMINAL, Status.UNAVAILABLE, False,
    ),
    Case(
        "unplayable_region_locked",
        lambda: VideoUnplayable(
            VIDEO, "The uploader has not made this video available in your country", []
        ),
        Outcome.TERMINAL, Status.UNAVAILABLE, False,
    ),
    # -- age restriction --------------------------------------------------- #
    Case(
        "age_restricted_without_cookies",
        lambda: AgeRestricted(VIDEO),
        Outcome.TERMINAL, Status.AGE_RESTRICTED, False,
    ),
    Case(
        # With cookies configured the failure is no longer explained by missing
        # authentication, so it is worth another attempt rather than terminal.
        "age_restricted_with_cookies",
        lambda: AgeRestricted(VIDEO),
        Outcome.RETRYABLE, Status.RETRY, True,
        max_attempts=2, cookies_configured=True,
    ),
    Case(
        "unplayable_age_reason_routes_to_age_restricted",
        lambda: VideoUnplayable(VIDEO, "Sign in to confirm your age", []),
        Outcome.TERMINAL, Status.AGE_RESTRICTED, False,
    ),
    # -- blocked: a fetcher condition, never a video condition ------------- #
    Case(
        "request_blocked",
        lambda: RequestBlocked(VIDEO),
        Outcome.BLOCKED, None, False,
    ),
    Case(
        # IpBlocked subclasses RequestBlocked; both must land on BLOCKED.
        "ip_blocked",
        lambda: IpBlocked(VIDEO),
        Outcome.BLOCKED, None, False,
    ),
    Case(
        "po_token_required",
        lambda: PoTokenRequired(VIDEO),
        Outcome.BLOCKED, None, False,
    ),
    Case(
        "http_429_wrapped_by_library",
        lambda: YouTubeRequestFailed(VIDEO, http_error(429)),
        Outcome.BLOCKED, None, False, http_status=429,
    ),
    Case(
        "http_429_raw",
        lambda: http_error(429),
        Outcome.BLOCKED, None, False, http_status=429,
    ),
    Case(
        "hard_block_raised_by_us",
        lambda: HardBlock("canary request refused"),
        Outcome.BLOCKED, None, False,
    ),
    Case(
        "cookie_file_missing",
        lambda: CookiePathInvalid("/nope/cookies.txt"),
        Outcome.BLOCKED, None, False,
    ),
    Case(
        "cookie_expired",
        lambda: CookieInvalid("/tmp/cookies.txt"),
        Outcome.BLOCKED, None, False,
    ),
    # -- transient --------------------------------------------------------- #
    Case(
        "http_500_wrapped",
        lambda: YouTubeRequestFailed(VIDEO, http_error(500)),
        Outcome.RETRYABLE, Status.RETRY, True, http_status=500,
    ),
    Case(
        "http_503_wrapped",
        lambda: YouTubeRequestFailed(VIDEO, http_error(503)),
        Outcome.RETRYABLE, Status.RETRY, True, http_status=503,
    ),
    Case(
        "http_503_raw",
        lambda: http_error(503),
        Outcome.RETRYABLE, Status.RETRY, True, http_status=503,
    ),
    Case(
        "timeout",
        lambda: requests.exceptions.ReadTimeout("timed out"),
        Outcome.RETRYABLE, Status.RETRY, True,
    ),
    Case(
        "connect_timeout",
        lambda: requests.exceptions.ConnectTimeout("timed out"),
        Outcome.RETRYABLE, Status.RETRY, True,
    ),
    Case(
        "connection_reset",
        lambda: requests.exceptions.ConnectionError(
            ConnectionResetError(104, "Connection reset by peer")
        ),
        Outcome.RETRYABLE, Status.RETRY, True,
    ),
    Case(
        "chunked_encoding_error",
        lambda: requests.exceptions.ChunkedEncodingError("incomplete read"),
        Outcome.RETRYABLE, Status.RETRY, True,
    ),
    Case(
        "socket_timeout",
        lambda: socket.timeout("timed out"),
        Outcome.RETRYABLE, Status.RETRY, True,
    ),
    Case(
        "consent_cookie_failed",
        lambda: FailedToCreateConsentCookie(VIDEO),
        Outcome.RETRYABLE, Status.RETRY, True,
    ),
    # -- malformed response: retryable, but only twice --------------------- #
    Case(
        "youtube_data_unparsable",
        lambda: YouTubeDataUnparsable(VIDEO),
        Outcome.RETRYABLE, Status.RETRY, True, max_attempts=2,
    ),
    Case(
        "json_decode_error",
        json_decode_error,
        Outcome.RETRYABLE, Status.RETRY, True, max_attempts=2,
    ),
    Case(
        "xml_parse_error",
        xml_parse_error,
        Outcome.RETRYABLE, Status.RETRY, True, max_attempts=2,
    ),
    # -- database ---------------------------------------------------------- #
    Case(
        "mysql_deadlock",
        lambda: mysql_operational_error(1213),
        Outcome.RETRYABLE, None, True,
    ),
    Case(
        "mysql_lock_wait_timeout",
        lambda: mysql_operational_error(1205),
        Outcome.RETRYABLE, None, True,
    ),
    # -- unknown ----------------------------------------------------------- #
    Case(
        "unknown_exception",
        lambda: RuntimeError("something nobody predicted"),
        Outcome.RETRYABLE, Status.RETRY, True, max_attempts=1,
    ),
    Case(
        "unknown_library_exception",
        lambda: CouldNotRetrieveTranscript(VIDEO),
        Outcome.RETRYABLE, Status.RETRY, True, max_attempts=1,
    ),
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_classification(case: Case) -> None:
    result = classify(case.build(), cookies_configured=case.cookies_configured)
    assert result.outcome is case.outcome
    assert result.status == case.status
    assert result.retryable is case.retryable
    assert result.needs_audio is case.needs_audio
    if case.http_status is not None:
        assert result.http_status == case.http_status
    if case.max_attempts is not None:
        assert result.max_attempts == case.max_attempts


def test_every_case_name_is_unique() -> None:
    names = [c.name for c in CASES]
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# Invariants that matter more than any individual row
# --------------------------------------------------------------------------- #


def test_blocked_never_changes_video_status() -> None:
    """The single most important property in this module.

    A block means the fetcher is being refused, not that the video lacks
    captions. If a block could set a status, one bad afternoon on a datacenter
    IP would mark thousands of perfectly good videos ``failed`` with no way to
    tell them apart from genuinely caption-less ones.
    """
    blocked = [
        RequestBlocked(VIDEO),
        IpBlocked(VIDEO),
        PoTokenRequired(VIDEO),
        YouTubeRequestFailed(VIDEO, http_error(429)),
        http_error(429),
        HardBlock("refused"),
        CookieInvalid("/tmp/c.txt"),
    ]
    for exc in blocked:
        result = classify(exc)
        assert result.outcome is Outcome.BLOCKED, exc
        assert result.status is None, f"{type(exc).__name__} would poison video rows"
        assert result.is_block is True
        assert result.needs_audio is False


def test_classification_is_total() -> None:
    """No exception may escape unclassified.

    Every exception class the library exports, plus a few builtins, must map to
    exactly one outcome. Unmapped ones fall through to retryable-once, never to
    an exception from the classifier itself.
    """
    import youtube_transcript_api as api

    exotic: list[BaseException] = [
        ValueError("x"), KeyError("x"), TypeError("x"), OSError("x"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        ZeroDivisionError("x"), MemoryError(),
    ]
    for name in dir(api):
        obj = getattr(api, name)
        if isinstance(obj, type) and issubclass(obj, Exception):
            try:
                exotic.append(obj(VIDEO))
            except TypeError:
                continue  # constructor needs more args; covered explicitly above
    for exc in exotic:
        result = classify(exc)
        assert result.outcome in set(Outcome)
        assert result.error_type


def test_classify_is_pure() -> None:
    """Same input, same answer - no hidden clock, counter, or global."""
    exc = TranscriptsDisabled(VIDEO)
    first = classify(exc)
    for _ in range(5):
        assert classify(exc) == first


def test_needs_audio_only_on_missing_text() -> None:
    """``needs_audio`` is the phase-2 queue; only text-less videos belong in it."""
    assert classify(TranscriptsDisabled(VIDEO)).needs_audio is True
    for exc in (
        # Captions exist, just not in a configured language: translate, don't ASR.
        NoTranscriptFound(VIDEO, ["en"], empty_transcript_list()),
        VideoUnavailable(VIDEO),
        AgeRestricted(VIDEO),
        InvalidVideoId(VIDEO),
        RequestBlocked(VIDEO),
        RuntimeError("x"),
    ):
        assert classify(exc).needs_audio is False, exc


def test_terminal_statuses_are_not_retryable() -> None:
    for case in CASES:
        result = classify(case.build(), cookies_configured=case.cookies_configured)
        if result.outcome is Outcome.TERMINAL:
            assert result.retryable is False
            assert result.status is not None


def test_http_status_extracted_from_stringified_error() -> None:
    """YouTubeRequestFailed drops the response object; parse its message.

    ``YouTubeRequestFailed.__init__`` stores ``str(http_error)`` and nothing
    else, so ``.response.status_code`` is simply not available. If this
    extraction breaks, every 429 silently degrades from BLOCKED to a generic
    retry and the circuit breaker never trips.
    """
    for status in (400, 403, 429, 500, 502, 503):
        exc = YouTubeRequestFailed(VIDEO, http_error(status))
        assert classify(exc).http_status == status, str(exc)


def test_status_reason_is_short_enough_for_the_column() -> None:
    """``videos.status_reason`` is VARCHAR(255) under STRICT_TRANS_TABLES."""
    for case in CASES:
        result = classify(case.build(), cookies_configured=case.cookies_configured)
        assert len(result.reason) <= 255, case.name
        assert len(result.error_type) <= 128, case.name


def test_describe_exception_truncates_for_the_column() -> None:
    """``fetch_attempts.error_message`` is VARCHAR(1024)."""
    long = RuntimeError("x" * 5000)
    error_type, message = describe_exception(long)
    assert error_type == "RuntimeError"
    assert len(message) <= 1024


# --------------------------------------------------------------------------- #
# yt-dlp: real exception instances from the pinned version
# --------------------------------------------------------------------------- #


def ytdlp_error(message: str) -> BaseException:
    """A genuine ``yt_dlp.utils.DownloadError``, as the fetcher would see it."""
    from yt_dlp.utils import DownloadError

    return DownloadError(message)


YTDLP_CASES: tuple[tuple[str, str, Outcome, Status | None], ...] = (
    # The one that matters most. Note the curly apostrophe: yt-dlp emits U+2019,
    # and matching a straight ASCII quote against it silently fails, which would
    # send this down the "unrecognised exception" path and mark videos failed
    # during an IP ban.
    (
        "bot_check_curly_apostrophe",
        "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication.",
        Outcome.BLOCKED, None,
    ),
    (
        "bot_check_straight_apostrophe",
        "ERROR: Sign in to confirm you're not a bot.",
        Outcome.BLOCKED, None,
    ),
    (
        "precondition_check_failed",
        "ERROR: [youtube] Unable to download API page: HTTP Error 400: Bad Request; "
        "YouTube said: ERROR - Precondition check failed.",
        Outcome.BLOCKED, None,
    ),
    (
        "http_429",
        "ERROR: [youtube] Unable to download webpage: HTTP Error 429: Too Many Requests",
        Outcome.BLOCKED, None,
    ),
    (
        "video_unavailable",
        "ERROR: [youtube] abcdefghijk: Video unavailable",
        Outcome.TERMINAL, Status.UNAVAILABLE,
    ),
    (
        "private_video",
        "ERROR: [youtube] abcdefghijk: Private video. Sign in if you've been granted "
        "access to this video",
        Outcome.TERMINAL, Status.UNAVAILABLE,
    ),
    (
        "region_locked",
        "ERROR: [youtube] abcdefghijk: The uploader has not made this video "
        "available in your country",
        Outcome.TERMINAL, Status.UNAVAILABLE,
    ),
    (
        "members_only",
        "ERROR: [youtube] abcdefghijk: Join this channel to get access to members-only "
        "content",
        Outcome.TERMINAL, Status.UNAVAILABLE,
    ),
    (
        "age_restricted",
        "ERROR: [youtube] abcdefghijk: Sign in to confirm your age. "
        "This video may be inappropriate for some users.",
        Outcome.TERMINAL, Status.AGE_RESTRICTED,
    ),
    (
        "upcoming_premiere",
        "ERROR: [youtube] abcdefghijk: This live event will begin in 3 hours.",
        Outcome.TERMINAL, Status.SKIPPED,
    ),
    (
        "unknown_ytdlp_message",
        "ERROR: [youtube] something entirely new happened",
        Outcome.RETRYABLE, Status.RETRY,
    ),
)


@pytest.mark.parametrize(
    "name,message,outcome,status", YTDLP_CASES, ids=[c[0] for c in YTDLP_CASES]
)
def test_ytdlp_classification(
    name: str, message: str, outcome: Outcome, status: Status | None
) -> None:
    result = classify(ytdlp_error(message))
    assert result.outcome is outcome, f"{name}: {result.reason}"
    assert result.status == status, f"{name}: {result.reason}"


def test_ytdlp_bot_check_never_touches_video_status() -> None:
    """Regression guard for a bug found by running the live integration suite.

    yt-dlp's bot-check refusal used to fall through to "unrecognised exception",
    which meant a single blocked afternoon would burn an attempt on every video
    in the queue and eventually mark them all failed.
    """
    exc = ytdlp_error(
        "ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you’re not a bot."
    )
    result = classify(exc)
    assert result.outcome is Outcome.BLOCKED
    assert result.status is None
    assert result.retryable is False
    assert result.needs_audio is False


def test_ytdlp_unknown_errors_get_two_attempts_not_one() -> None:
    """yt-dlp rewords its messages between releases; be slightly generous."""
    result = classify(ytdlp_error("ERROR: mystery"))
    assert result.max_attempts == 2


def test_ytdlp_age_restriction_respects_cookies() -> None:
    message = "ERROR: [youtube] x: Sign in to confirm your age"
    assert classify(ytdlp_error(message)).status is Status.AGE_RESTRICTED
    with_cookies = classify(ytdlp_error(message), cookies_configured=True)
    assert with_cookies.outcome is Outcome.RETRYABLE


def test_extractor_error_is_recognised_too() -> None:
    from yt_dlp.utils import ExtractorError

    result = classify(ExtractorError("Video unavailable"))
    assert result.outcome is Outcome.TERMINAL
    assert result.status is Status.UNAVAILABLE


def test_ip_blocked_subclass_ordering() -> None:
    """Guard against a dispatch table that checks the parent first by accident."""
    assert issubclass(IpBlocked, RequestBlocked)
    assert classify(IpBlocked(VIDEO)).error_type == "IpBlocked"
    assert classify(RequestBlocked(VIDEO)).error_type == "RequestBlocked"


def test_unknown_then_exhausted_becomes_failed() -> None:
    """Retryable-with-cap is what turns a mystery into ``failed``, not ``retry``."""
    result = classify(RuntimeError("mystery"))
    assert result.max_attempts == 1
    assert result.retryable is True
    assert result.status is Status.RETRY
