"""Exception -> (outcome, status, retryable). The reason this thing survives.

Pure and total: no I/O, no clock, no globals, and no exception escapes
unclassified. Everything downstream - whether a video is retried, abandoned, or
left untouched while the pipeline backs off - is decided here.

The one invariant worth stating twice: **a block is a fetcher condition, not a
video condition.** When YouTube refuses our IP, every video in flight looks
broken. If that set a per-video status, one bad afternoon would mark thousands
of perfectly transcribable videos ``failed``, indistinguishable from genuinely
caption-less ones, and there would be no way to find them again. So
:data:`Outcome.BLOCKED` always carries ``status=None``, the video stays queued,
and the circuit breaker deals with it at the pipeline level.

Written against **youtube-transcript-api 1.2.4** as pinned in ``pyproject.toml``.
Facts read out of that version's source rather than its docs:

* ``IpBlocked`` subclasses ``RequestBlocked``, so the dispatch order below is
  significant.
* There is no ``TooManyRequests`` exception. A 429 arrives as
  ``YouTubeRequestFailed``, whose ``__init__`` keeps ``str(http_error)`` and
  throws the response away - so the status code has to be parsed back out of
  that string. :func:`http_status_from_text` does that, and
  ``tests/test_classify.py`` pins it. If it ever silently stops working, 429s
  degrade to ordinary retries and the circuit breaker never trips.
* ``AgeRestricted`` and ``PoTokenRequired`` descend straight from
  ``CouldNotRetrieveTranscript``, not from ``VideoUnplayable``.
* Members-only and region-locked videos both arrive as ``VideoUnplayable`` and
  are told apart only by the ``reason`` string.
"""

from __future__ import annotations

import json
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Final

import requests
from youtube_transcript_api import (
    AgeRestricted,
    CookieError,
    CouldNotRetrieveTranscript,
    FailedToCreateConsentCookie,
    InvalidVideoId,
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

from .db import is_retryable_db_error
from .states import Outcome, Status

MAX_REASON_LEN: Final = 255  # videos.status_reason
MAX_ERROR_TYPE_LEN: Final = 128  # fetch_attempts.error_type
MAX_ERROR_MESSAGE_LEN: Final = 1024  # fetch_attempts.error_message

# Statuses YouTube returns when it is refusing *us*, not the video. 403 belongs
# here: video-level permission problems surface as typed exceptions, so a raw
# 403 on the caption endpoint is an IP or quota refusal in practice.
BLOCKING_HTTP_STATUSES: Final[frozenset[int]] = frozenset({403, 429})

_STATUS_IN_TEXT: Final = re.compile(r"\b([1-5]\d{2})\s+(?:Client|Server)\s+Error\b")
_LEADING_STATUS: Final = re.compile(r"^\s*([1-5]\d{2})\b")

_AGE_HINTS: Final[tuple[str, ...]] = (
    "confirm your age",
    "age-restricted",
    "age restricted",
    "inappropriate for some users",
)

# yt-dlp reports everything as DownloadError/ExtractorError with the real cause
# only in the message, so these strings are the only discriminator available.
# Matched against a message normalised to straight quotes and lower case -
# yt-dlp uses a curly apostrophe in "you're", and matching the ASCII form
# against the raw text silently fails.
#
# The bot-check case is the one that matters: it is a *block*, and letting it
# fall through to "unrecognised exception" would mark videos failed during an IP
# ban - exactly the mass-poisoning this module exists to prevent.
_YTDLP_BLOCK_HINTS: Final[tuple[str, ...]] = (
    "sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "http error 429",
    "too many requests",
    "precondition check failed",
    "this content isn't available",
    "please sign in",
    "requested format is not available",  # usually a stripped-down bot response
)
_YTDLP_UNAVAILABLE_HINTS: Final[tuple[str, ...]] = (
    "video unavailable",
    "private video",
    "this video has been removed",
    "account associated with this video has been terminated",
    "video is not available",
    # Covers both "not available in your country" and YouTube's actual wording,
    # "has not made this video available in your country".
    "available in your country",
    "blocked it in your country",
    "members-only",
    "join this channel",
    "unable to extract",  # persistent extractor breakage, not a transient fault
)
_YTDLP_UPCOMING_HINTS: Final[tuple[str, ...]] = (
    "premieres in",
    "this live event will begin",
    "live event will begin in",
)


def _normalise_message(text: str) -> str:
    """Lower-case with curly quotes folded to ASCII, for hint matching."""
    return (
        text.replace("’", "'")
        .replace("‘", "'")
        .replace("“", '"')
        .replace("”", '"')
        .lower()
    )


def _is_ytdlp_error(exc: BaseException) -> bool:
    """Recognise yt-dlp's exceptions without importing yt_dlp.

    Duck-typed on the class name deliberately: importing yt_dlp costs the better
    part of a second, and the classifier is on the hot path of every video.
    """
    for klass in type(exc).__mro__:
        if klass.__name__ in {"DownloadError", "ExtractorError", "GeoRestrictedError"}:
            return True
    return False


class HardBlock(Exception):
    """Raised when YouTube is refusing the fetcher rather than the video.

    Caught by the worker, which trips the circuit breaker and leaves every
    in-flight video queued exactly as it was.
    """

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        self.http_status = http_status
        super().__init__(message)


class QuotaExhausted(Exception):
    """Data API budget for the day is spent. The run stops cleanly."""


@dataclass(frozen=True, slots=True)
class Classification:
    """What to do about one exception.

    Attributes:
        outcome: Recorded verbatim in ``fetch_attempts.outcome``.
        status: New ``videos.status``, or ``None`` to leave it alone. Always
            ``None`` for :data:`Outcome.BLOCKED`.
        retryable: Whether another attempt could plausibly succeed.
        needs_audio: Set ``videos.needs_audio=1``, queueing it for phase 2.
        reason: Short human-readable cause, sized for ``status_reason``.
        error_type: Exception class name, for ``fetch_attempts.error_type``.
        http_status: Recovered HTTP status, when there was one.
        max_attempts: Attempt ceiling specific to *this* failure mode, which
            overrides the configured global when lower. A malformed response
            deserves two tries; a wholly unrecognised exception deserves one.
    """

    outcome: Outcome
    status: Status | None
    retryable: bool
    reason: str
    error_type: str
    needs_audio: bool = False
    http_status: int | None = None
    max_attempts: int | None = None

    @property
    def is_block(self) -> bool:
        return self.outcome is Outcome.BLOCKED

    @property
    def is_terminal(self) -> bool:
        return self.outcome is Outcome.TERMINAL

    def as_tuple(self) -> tuple[Outcome, Status | None, bool]:
        """The spec's ``classify(exc) -> (outcome, status, retryable)`` shape."""
        return (self.outcome, self.status, self.retryable)


def describe_exception(exc: BaseException) -> tuple[str, str]:
    """Class name and message, both truncated to their column widths."""
    error_type = type(exc).__name__[:MAX_ERROR_TYPE_LEN]
    try:
        message = str(exc)
    except Exception:  # pragma: no cover - a __str__ that raises
        message = "<unprintable exception>"
    message = " ".join(message.split())
    if len(message) > MAX_ERROR_MESSAGE_LEN:
        message = message[: MAX_ERROR_MESSAGE_LEN - 3] + "..."
    return error_type, message


def http_status_from_text(text: str) -> int | None:
    """Recover an HTTP status code from a stringified ``HTTPError``.

    ``str(HTTPError)`` reads ``"429 Client Error: Too Many Requests for url:
    https://..."``. The URL is full of digits, so anchor on the
    ``Client Error``/``Server Error`` phrase first and only then fall back to a
    leading code.
    """
    match = _STATUS_IN_TEXT.search(text)
    if match:
        return int(match.group(1))
    match = _LEADING_STATUS.match(text)
    if match:
        return int(match.group(1))
    return None


def http_status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status for any exception shape we might see."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "http_status", None)
    if isinstance(code, int):
        return code
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str):
        found = http_status_from_text(reason)
        if found is not None:
            return found
    return http_status_from_text(str(exc))


def _truncate_reason(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= MAX_REASON_LEN:
        return flat
    return flat[: MAX_REASON_LEN - 3] + "..."


def _blocked(exc: BaseException, reason: str, status: int | None = None) -> Classification:
    error_type, _ = describe_exception(exc)
    return Classification(
        outcome=Outcome.BLOCKED,
        status=None,  # never, under any circumstances, touch the video
        retryable=False,
        reason=_truncate_reason(reason),
        error_type=error_type,
        http_status=status,
    )


def _terminal(
    exc: BaseException,
    status: Status,
    reason: str,
    *,
    needs_audio: bool = False,
    http_status: int | None = None,
) -> Classification:
    error_type, _ = describe_exception(exc)
    return Classification(
        outcome=Outcome.TERMINAL,
        status=status,
        retryable=False,
        reason=_truncate_reason(reason),
        error_type=error_type,
        needs_audio=needs_audio,
        http_status=http_status,
    )


def _retry(
    exc: BaseException,
    reason: str,
    *,
    max_attempts: int | None = None,
    http_status: int | None = None,
    status: Status | None = Status.RETRY,
) -> Classification:
    error_type, _ = describe_exception(exc)
    return Classification(
        outcome=Outcome.RETRYABLE,
        status=status,
        retryable=True,
        reason=_truncate_reason(reason),
        error_type=error_type,
        http_status=http_status,
        max_attempts=max_attempts,
    )


def classify(exc: BaseException, *, cookies_configured: bool = False) -> Classification:
    """Map an exception onto exactly one outcome.

    Args:
        exc: The exception raised by a fetcher, an HTTP call, or the database.
        cookies_configured: Whether a cookie jar is in use. Changes the verdict
            on age restriction only: without cookies it is terminal (we cannot
            fix it), with cookies it is worth one more attempt (the cookies may
            simply be stale).

    Returns:
        A :class:`Classification`. Never raises.
    """
    # -- our own signals --------------------------------------------------- #
    if isinstance(exc, HardBlock):
        return _blocked(exc, str(exc) or "fetcher blocked", exc.http_status)

    # -- database ---------------------------------------------------------- #
    # Handled by the repo layer's retry decorator; the video is untouched.
    if is_retryable_db_error(exc):
        from .db import mysql_errno

        return _retry(
            exc,
            f"mysql errno {mysql_errno(exc)}: lock contention",
            status=None,
        )

    # -- configuration / auth: a fetcher problem, so leave videos alone ---- #
    if isinstance(exc, CookieError):
        return _blocked(exc, f"cookie configuration unusable: {exc}")

    # -- YouTube refusing us ----------------------------------------------- #
    # IpBlocked subclasses RequestBlocked, so this single check covers both;
    # error_type comes off the concrete class and stays specific.
    if isinstance(exc, RequestBlocked):
        return _blocked(exc, "YouTube is blocking requests from this IP")
    if isinstance(exc, PoTokenRequired):
        return _blocked(exc, "YouTube demanded a PO token for this request")

    # -- captions genuinely absent ----------------------------------------- #
    if isinstance(exc, TranscriptsDisabled):
        return _terminal(
            exc,
            Status.NO_TRANSCRIPT,
            "captions are disabled for this video",
            needs_audio=True,
        )

    # -- captions exist, wrong language ------------------------------------ #
    # needs_audio deliberately stays 0: there is text here, it is simply not in
    # a configured language. That is a translation problem, not a GPU problem.
    if isinstance(exc, NoTranscriptFound):
        requested = getattr(exc, "_requested_language_codes", None)
        detail = f" (wanted {', '.join(requested)})" if requested else ""
        return _terminal(
            exc, Status.LANG_MISSING, f"no transcript in configured languages{detail}"
        )
    if isinstance(exc, (NotTranslatable, TranslationLanguageNotAvailable)):
        return _terminal(
            exc, Status.LANG_MISSING, "requested translation is not available"
        )

    # -- age restriction --------------------------------------------------- #
    if isinstance(exc, AgeRestricted):
        if cookies_configured:
            return _retry(
                exc,
                "age-restricted and the configured cookies were rejected",
                max_attempts=2,
            )
        return _terminal(
            exc, Status.AGE_RESTRICTED, "age-restricted; requires authenticated cookies"
        )

    # -- unplayable: reason string is the only discriminator ---------------- #
    if isinstance(exc, VideoUnplayable):
        reason = (getattr(exc, "reason", None) or "").strip()
        lowered = reason.lower()
        if any(hint in lowered for hint in _AGE_HINTS):
            if cookies_configured:
                return _retry(
                    exc,
                    "age-restricted and the configured cookies were rejected",
                    max_attempts=2,
                )
            return _terminal(
                exc,
                Status.AGE_RESTRICTED,
                "age-restricted; requires authenticated cookies",
            )
        return _terminal(
            exc, Status.UNAVAILABLE, f"unplayable: {reason or 'no reason given'}"
        )

    # -- gone for good ----------------------------------------------------- #
    if isinstance(exc, VideoUnavailable):
        return _terminal(exc, Status.UNAVAILABLE, "video is no longer available")
    if isinstance(exc, InvalidVideoId):
        return _terminal(exc, Status.UNAVAILABLE, "video id is not valid")

    # -- HTTP failures the library wrapped --------------------------------- #
    if isinstance(exc, YouTubeRequestFailed):
        status = http_status_of(exc)
        if status is not None and status in BLOCKING_HTTP_STATUSES:
            return _blocked(exc, f"YouTube returned HTTP {status}", status)
        if status is not None and 500 <= status < 600:
            return _retry(exc, f"YouTube returned HTTP {status}", http_status=status)
        if status is not None and 400 <= status < 500:
            # An unexpected 4xx is more likely a changed endpoint than a
            # transient fault, so cap it low instead of burning four attempts.
            return _retry(
                exc,
                f"YouTube returned HTTP {status}",
                http_status=status,
                max_attempts=2,
            )
        return _retry(exc, f"request to YouTube failed: {exc}", http_status=status)

    # -- yt-dlp, whose exceptions carry their meaning only in the message --- #
    if _is_ytdlp_error(exc):
        return _classify_ytdlp(exc, cookies_configured=cookies_configured)

    # -- malformed or partial payloads ------------------------------------- #
    if isinstance(exc, YouTubeDataUnparsable):
        return _retry(
            exc, "YouTube returned data this version cannot parse", max_attempts=2
        )
    if isinstance(exc, (json.JSONDecodeError, ET.ParseError)):
        return _retry(exc, f"malformed response: {exc}", max_attempts=2)
    if isinstance(exc, FailedToCreateConsentCookie):
        return _retry(exc, "failed to auto-accept the cookie consent page")

    # -- raw transport ----------------------------------------------------- #
    if isinstance(exc, requests.exceptions.HTTPError):
        status = http_status_of(exc)
        if status is not None and status in BLOCKING_HTTP_STATUSES:
            return _blocked(exc, f"HTTP {status} from YouTube", status)
        return _retry(exc, f"HTTP error {status if status else ''}".strip(),
                      http_status=status)
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ContentDecodingError,
            requests.exceptions.RequestException,
            socket.timeout,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return _retry(exc, f"network error: {type(exc).__name__}")

    # -- everything else: one more try, then give up and keep the traceback - #
    if isinstance(exc, CouldNotRetrieveTranscript):
        return _retry(
            exc,
            f"unhandled transcript error: {type(exc).__name__}",
            max_attempts=1,
        )
    return _retry(exc, f"unexpected {type(exc).__name__}: {exc}", max_attempts=1)


def _classify_ytdlp(
    exc: BaseException, *, cookies_configured: bool
) -> Classification:
    """Classify a yt-dlp failure from its message.

    yt-dlp collapses every cause into ``DownloadError``/``ExtractorError``, so
    unlike youtube-transcript-api there is no type to dispatch on. Ordering
    matters: block hints are checked first, because a bot-check refusal
    superficially resembles half a dozen other messages and getting it wrong
    marks good videos ``failed`` en masse.
    """
    message = _normalise_message(str(exc))
    status = http_status_of(exc)

    if any(hint in message for hint in _YTDLP_BLOCK_HINTS):
        return _blocked(exc, f"yt-dlp refused: {str(exc)[:150]}", status)

    if any(hint in message for hint in _AGE_HINTS):
        if cookies_configured:
            return _retry(
                exc, "age-restricted and the configured cookies were rejected",
                max_attempts=2,
            )
        return _terminal(
            exc, Status.AGE_RESTRICTED,
            "age-restricted; requires authenticated cookies",
        )

    if any(hint in message for hint in _YTDLP_UPCOMING_HINTS):
        return _terminal(
            exc, Status.SKIPPED, "premiere or live event has not happened yet"
        )

    if any(hint in message for hint in _YTDLP_UNAVAILABLE_HINTS):
        return _terminal(exc, Status.UNAVAILABLE, f"unavailable: {str(exc)[:150]}")

    if "no such file" in message or "captions" in message and "no" in message:
        return _terminal(
            exc, Status.NO_TRANSCRIPT, "yt-dlp found no captions", needs_audio=True
        )

    if status is not None and status in BLOCKING_HTTP_STATUSES:
        return _blocked(exc, f"yt-dlp saw HTTP {status}", status)
    if status is not None and 500 <= status < 600:
        return _retry(exc, f"yt-dlp saw HTTP {status}", http_status=status)

    # Unrecognised yt-dlp failure. Two attempts, not one: yt-dlp's messages
    # change between releases, and being slightly generous here is much cheaper
    # than misfiling a whole channel.
    return _retry(exc, f"yt-dlp error: {str(exc)[:180]}", max_attempts=2,
                  http_status=status)


def effective_max_attempts(result: Classification, configured: int) -> int:
    """Attempt ceiling for this failure: the lower of specific and configured."""
    if result.max_attempts is None:
        return configured
    return min(result.max_attempts, configured)
