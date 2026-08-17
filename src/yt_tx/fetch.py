"""Transcript fetching: two backends behind one protocol, plus durable storage.

:class:`TranscriptFetcher` is the only abstraction in this codebase with more
than one implementation, which is why it exists at all. The two differ in what
they can reach - ``youtube-transcript-api`` is faster and gives typed errors,
yt-dlp copes with formats and videos the former cannot parse - so both are
useful and the pipeline should not care which is in play.

The stage does three things per video, in this order:

1. **List** what captions exist, and persist that inventory *whatever happens
   next*. This is the column that later tells "captions are switched off" apart
   from "captions exist, just not in a language you asked for". Without it both
   look identical and you cannot tell which are worth revisiting.
2. **Select** by preference chain: configured languages in order, manual over
   ASR, optionally a machine translation, else ``lang_missing``.
3. **Store**, in an order chosen for crash safety: write the gzip, fsync it,
   fsync its directory, and only then commit the database row. An orphan file is
   harmless and reclaimable; a row pointing at a file that does not exist is
   corruption. ``doctor`` reconciles both directions.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import time
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast, runtime_checkable

import requests

from .classify import Classification, HardBlock, classify, describe_exception
from .limiter import full_jitter_backoff
from .logs import context, get_logger
from .repo import Repo, TranscriptWrite, VideoRow
from .states import Status, TranscriptKind

log = get_logger(__name__)

SOURCE_YTA: Final = "youtube-transcript-api"
SOURCE_YTDLP: Final = "yt-dlp"

_WHITESPACE: Final = re.compile(r"\s+")
# Caption artefacts that are noise in a searchable corpus.
_ARTEFACTS: Final = re.compile(r"^\s*\[(?:music|applause|laughter|inaudible)\]\s*$", re.I)


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Segment:
    """One caption cue, normalised across both backends."""

    start: float
    duration: float
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class Available:
    """One caption track that exists for a video."""

    language_code: str
    language: str
    kind: TranscriptKind
    is_translatable: bool = False
    translation_targets: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "language_code": self.language_code,
            "language": self.language,
            "kind": self.kind.value,
            "is_translatable": self.is_translatable,
        }


@dataclass(frozen=True, slots=True)
class Selection:
    """A decision to download one specific variant."""

    language_code: str
    kind: TranscriptKind
    source: Available
    translate_to: str | None = None
    is_preferred: bool = False

    @property
    def stored_language(self) -> str:
        return self.translate_to or self.language_code

    @property
    def stored_kind(self) -> TranscriptKind:
        return TranscriptKind.TRANSLATED if self.translate_to else self.kind


class NoTranscriptInLanguages(Exception):
    """Tracks exist, but none in a configured language. Maps to ``lang_missing``."""


@runtime_checkable
class TranscriptFetcher(Protocol):
    """The one genuine two-implementation abstraction here."""

    name: str

    def list_available(self, video_id: str) -> list[Available]:
        """Every caption track that exists. Raises on captions-disabled."""
        ...

    def download(self, video_id: str, selection: Selection) -> list[Segment]:
        """Fetch and normalise one variant."""
        ...

    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def _rank_kind(kind: TranscriptKind, prefer_manual: bool) -> int:
    if prefer_manual:
        return 0 if kind is TranscriptKind.MANUAL else 1
    return 0


def _matches(available: Sequence[Available], wanted: str) -> list[Available]:
    """Exact language match, then base-language match.

    ``en`` matching ``en-GB`` is deliberate: uploads are tagged inconsistently
    and a configured ``en`` that refuses ``en-GB`` captions would quietly file
    thousands of perfectly good English videos as ``lang_missing``.
    """
    lowered = wanted.lower()
    exact = [a for a in available if a.language_code.lower() == lowered]
    if exact:
        return exact
    base = lowered.split("-")[0]
    return [a for a in available if a.language_code.lower().split("-")[0] == base]


def select_transcripts(
    available: Sequence[Available],
    *,
    languages: Sequence[str],
    prefer_manual: bool = True,
    store_all_variants: bool = False,
    accept_translated: bool = False,
) -> list[Selection]:
    """Choose what to download.

    Returns:
        Selections in storage order, the first flagged ``is_preferred``.

    Raises:
        NoTranscriptInLanguages: nothing matched and translation is off or
            impossible. The caller turns this into ``lang_missing``.
    """
    if not available:
        raise NoTranscriptInLanguages("no caption tracks exist")

    best: Available | None = None
    for wanted in languages:
        candidates = _matches(available, wanted)
        if candidates:
            best = sorted(
                candidates, key=lambda a: (_rank_kind(a.kind, prefer_manual),
                                           a.language_code)
            )[0]
            break

    if best is None and accept_translated:
        translatable = [a for a in available if a.is_translatable]
        for wanted in languages:
            for track in translatable:
                if not track.translation_targets or wanted in track.translation_targets:
                    return [
                        Selection(
                            language_code=track.language_code,
                            kind=track.kind,
                            source=track,
                            translate_to=wanted,
                            is_preferred=True,
                        )
                    ]

    if best is None:
        raise NoTranscriptInLanguages(
            f"none of {', '.join(languages)} among "
            f"{', '.join(a.language_code for a in available)}"
        )

    selections = [
        Selection(
            language_code=best.language_code,
            kind=best.kind,
            source=best,
            is_preferred=True,
        )
    ]
    if store_all_variants:
        # Every real track, not the translation matrix - that would be unbounded.
        for track in available:
            if track is best:
                continue
            selections.append(
                Selection(
                    language_code=track.language_code,
                    kind=track.kind,
                    source=track,
                    is_preferred=False,
                )
            )
    return selections


# --------------------------------------------------------------------------- #
# Normalisation and storage
# --------------------------------------------------------------------------- #


def normalise_segments(raw: Iterable[dict[str, Any]]) -> list[Segment]:
    """Coerce either backend's output into ``{start, duration, text}``."""
    out: list[Segment] = []
    for item in raw:
        text = str(item.get("text") or "")
        text = _WHITESPACE.sub(" ", text.replace("\n", " ")).strip()
        if not text:
            continue
        try:
            start = float(item.get("start") or 0.0)
            duration = float(item.get("duration") or 0.0)
        except (TypeError, ValueError):
            continue
        out.append(Segment(start=max(0.0, start), duration=max(0.0, duration), text=text))
    out.sort(key=lambda s: s.start)
    return out


def flatten(segments: Sequence[Segment]) -> str:
    """Segments to searchable plaintext, dropping pure sound-effect cues."""
    parts = [s.text for s in segments if not _ARTEFACTS.match(s.text)]
    return _WHITESPACE.sub(" ", " ".join(parts)).strip()


def covered_seconds(segments: Sequence[Segment]) -> float:
    """Union of cue intervals, in seconds.

    Merged rather than summed: ASR cues routinely overlap, and a naive sum can
    report more covered seconds than the video is long, which makes the number
    useless for spotting a truncated transcript.
    """
    if not segments:
        return 0.0
    intervals = sorted((s.start, s.start + s.duration) for s in segments)
    total = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    total += current_end - current_start
    return round(total, 2)


def word_count(plaintext: str) -> int:
    return len(plaintext.split()) if plaintext else 0


def transcript_path(
    root: Path, channel_id: str, video_id: str, language: str, kind: TranscriptKind
) -> Path:
    safe_lang = re.sub(r"[^A-Za-z0-9_-]", "_", language)[:16]
    return root / channel_id / f"{video_id}.{safe_lang}.{kind.value}.json.gz"


@dataclass(frozen=True, slots=True)
class StoredFile:
    path: Path
    sha256: str
    bytes_written: int


def write_transcript_file(
    path: Path,
    *,
    video_id: str,
    language: str,
    kind: TranscriptKind,
    source: str,
    segments: Sequence[Segment],
) -> StoredFile:
    """Write the gzip atomically and durably, returning its hash.

    Ordering is the point. Temp file, fsync, atomic rename, fsync the directory -
    and only after all of that does the caller commit the database row. A power
    cut anywhere in here leaves at worst an unreferenced file.

    ``mtime=0`` keeps the gzip byte-identical for identical input, so the stored
    sha256 stays verifiable by ``doctor`` across rewrites instead of changing
    every time purely because the clock moved.
    """
    payload = {
        "video_id": video_id,
        "language_code": language,
        "kind": kind.value,
        "source": source,
        "segment_count": len(segments),
        "segments": [s.as_dict() for s in segments],
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, compresslevel=6) as gz:
        gz.write(body)
    blob = buffer.getvalue()
    digest = hashlib.sha256(blob).hexdigest()

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    # Without this the rename itself can be lost on power failure, leaving the
    # committed row pointing at nothing.
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return StoredFile(path=path, sha256=digest, bytes_written=len(blob))


def read_transcript_file(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        loaded = json.load(handle)
    return cast("dict[str, Any]", loaded)


def verify_transcript_file(path: Path, expected_sha256: str) -> bool:
    if not path.exists():
        return False
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest == expected_sha256


# --------------------------------------------------------------------------- #
# Backend: youtube-transcript-api
# --------------------------------------------------------------------------- #


def build_session(
    *, cookies_file: str | None = None, proxy: str | None = None
) -> requests.Session:
    """A requests session carrying cookies and proxy, for either backend.

    In youtube-transcript-api 1.x there is no ``cookie_path`` argument any more -
    authentication is done by handing the client an ``http_client`` session with
    the cookies already loaded, which is what this builds.
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    if cookies_file:
        from http.cookiejar import MozillaCookieJar

        jar = MozillaCookieJar(cookies_file)
        # Let the caller's classifier decide what a bad cookie file means; both
        # OSError and LoadError surface as a configuration-level block.
        jar.load(ignore_discard=True, ignore_expires=True)
        session.cookies = jar  # type: ignore[assignment]
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


class YouTubeTranscriptApiFetcher:
    """Backend built on ``youtube-transcript-api`` 1.2.4.

    Deliberately does not catch anything: every exception the library raises is
    already meaningful and :func:`yt_tx.classify.classify` knows all of them.
    Swallowing them here would be how an IP block gets misfiled as a video
    problem.
    """

    name = SOURCE_YTA

    def __init__(
        self, *, cookies_file: str | None = None, proxy: str | None = None
    ) -> None:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import GenericProxyConfig

        self._session = build_session(cookies_file=cookies_file, proxy=proxy)
        proxy_config = (
            GenericProxyConfig(http_url=proxy, https_url=proxy) if proxy else None
        )
        self._api = YouTubeTranscriptApi(
            proxy_config=proxy_config, http_client=self._session
        )
        self._lists: dict[str, Any] = {}

    def list_available(self, video_id: str) -> list[Available]:
        transcript_list = self._api.list(video_id)
        # Kept so download() can reuse the already-fetched track objects instead
        # of paying for a second .list() call per video.
        self._lists[video_id] = transcript_list

        out: list[Available] = []
        for track in transcript_list:
            targets = tuple(
                str(t["language_code"])
                for t in getattr(track, "translation_languages", []) or []
                if isinstance(t, dict) and "language_code" in t
            )
            out.append(
                Available(
                    language_code=str(track.language_code),
                    language=str(track.language),
                    kind=(
                        TranscriptKind.ASR if track.is_generated else TranscriptKind.MANUAL
                    ),
                    is_translatable=bool(track.is_translatable),
                    translation_targets=targets,
                )
            )
        return out

    def download(self, video_id: str, selection: Selection) -> list[Segment]:
        transcript_list = self._lists.get(video_id) or self._api.list(video_id)
        track = None
        for candidate in transcript_list:
            if (
                str(candidate.language_code) == selection.language_code
                and (candidate.is_generated) == (selection.kind is TranscriptKind.ASR)
            ):
                track = candidate
                break
        if track is None:
            # Fall back to the library's own resolution rather than guessing.
            track = transcript_list.find_transcript([selection.language_code])

        if selection.translate_to:
            track = track.translate(selection.translate_to)

        fetched = track.fetch()
        return normalise_segments(fetched.to_raw_data())

    def close(self) -> None:
        self._lists.clear()
        self._session.close()


# --------------------------------------------------------------------------- #
# Backend: yt-dlp
# --------------------------------------------------------------------------- #


class YtDlpFetcher:
    """Backend built on yt-dlp's subtitle metadata.

    yt-dlp reports caption tracks as URLs; this reads the ``json3`` variant
    directly rather than letting yt-dlp write files to disk, which keeps storage
    in one place and avoids a temp-directory dance per video.
    """

    name = SOURCE_YTDLP

    JSON3_PRIORITY: Final[tuple[str, ...]] = ("json3", "srv3", "srv2", "srv1", "vtt")

    def __init__(
        self, *, cookies_file: str | None = None, proxy: str | None = None
    ) -> None:
        self._cookies_file = cookies_file
        self._proxy = proxy
        self._session = build_session(cookies_file=cookies_file, proxy=proxy)
        self._info: dict[str, dict[str, Any]] = {}

    def _extract(self, video_id: str) -> dict[str, Any]:
        cached = self._info.get(video_id)
        if cached is not None:
            return cached

        from yt_dlp import YoutubeDL

        from .discover import ytdlp_options

        options = ytdlp_options(cookies_file=self._cookies_file, proxy=self._proxy)
        options["extract_flat"] = False
        options["writesubtitles"] = False
        # ignoreerrors would turn an IP block into a silent empty result, which
        # is precisely the misclassification this project is built to avoid.
        options["ignoreerrors"] = False

        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
        if not isinstance(info, dict):
            raise HardBlock(f"yt-dlp returned no info for {video_id}")
        data = cast("dict[str, Any]", info)
        self._info[video_id] = data
        return data

    def list_available(self, video_id: str) -> list[Available]:
        from youtube_transcript_api import TranscriptsDisabled

        info = self._extract(video_id)
        manual = cast("dict[str, Any]", info.get("subtitles") or {})
        auto = cast("dict[str, Any]", info.get("automatic_captions") or {})

        out: list[Available] = []
        for language_code, formats in manual.items():
            if self._pick_format(formats) is None:
                continue
            out.append(
                Available(
                    language_code=str(language_code),
                    language=self._label(formats, str(language_code)),
                    kind=TranscriptKind.MANUAL,
                )
            )
        known = {a.language_code for a in out}
        for language_code, formats in auto.items():
            code = str(language_code)
            if code in known or code.endswith("-orig"):
                continue
            chosen = self._pick_format(formats)
            if chosen is None:
                continue
            # yt-dlp lists the *entire* auto-translation matrix under
            # automatic_captions - often 200 languages for a video with one ASR
            # track. Only the genuine ASR track has no `tlang` in its URL, so
            # that is the discriminator. Filtering by language-code shape does
            # not work: "af" and "en" look identical either way.
            if "tlang=" in str(chosen.get("url") or ""):
                continue
            out.append(
                Available(
                    language_code=code,
                    language=self._label(formats, code),
                    kind=TranscriptKind.ASR,
                    is_translatable=True,
                )
            )

        if not out:
            # Same signal the other backend gives, so the classifier needs no
            # special case: no tracks at all means captions are unavailable.
            raise TranscriptsDisabled(video_id)
        return out

    def download(self, video_id: str, selection: Selection) -> list[Segment]:
        info = self._extract(video_id)
        bucket = (
            "subtitles" if selection.kind is TranscriptKind.MANUAL
            else "automatic_captions"
        )
        tracks = cast("dict[str, Any]", info.get(bucket) or {})
        formats = tracks.get(selection.language_code)
        if not formats:
            raise NoTranscriptInLanguages(
                f"{selection.language_code} ({selection.kind}) vanished between "
                "listing and download"
            )

        chosen = self._pick_format(formats)
        if chosen is None:
            raise NoTranscriptInLanguages(
                f"no readable subtitle format for {selection.language_code}"
            )
        url = str(chosen["url"])
        if selection.translate_to:
            url = f"{url}&tlang={selection.translate_to}"

        response = self._session.get(url, timeout=30)
        response.raise_for_status()

        extension = str(chosen.get("ext") or "")
        if extension == "json3":
            return normalise_segments(_parse_json3(response.text))
        if extension == "vtt":
            return normalise_segments(_parse_vtt(response.text))
        return normalise_segments(_parse_srv(response.text))

    def _pick_format(self, formats: object) -> dict[str, Any] | None:
        if not isinstance(formats, list):
            return None
        entries = [f for f in formats if isinstance(f, dict)]
        for extension in self.JSON3_PRIORITY:
            for entry in entries:
                candidate = cast("dict[str, Any]", entry)
                if candidate.get("ext") == extension and candidate.get("url"):
                    return candidate
        return None

    @staticmethod
    def _label(formats: object, fallback: str) -> str:
        if isinstance(formats, list):
            for entry in formats:
                if isinstance(entry, dict):
                    name = cast("dict[str, Any]", entry).get("name")
                    if isinstance(name, str) and name:
                        return name[:64]
        return fallback

    def close(self) -> None:
        self._info.clear()
        self._session.close()


def _parse_json3(body: str) -> list[dict[str, Any]]:
    """YouTube's ``json3`` caption format into raw segment dicts."""
    payload = json.loads(body)
    if not isinstance(payload, dict):
        return []
    events = cast("dict[str, Any]", payload).get("events") or []
    out: list[dict[str, Any]] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        event = cast("dict[str, Any]", raw)
        segs = event.get("segs")
        if not isinstance(segs, list):
            continue
        text = "".join(
            str(cast("dict[str, Any]", s).get("utf8") or "")
            for s in segs
            if isinstance(s, dict)
        )
        if not text.strip():
            continue
        start_ms = event.get("tStartMs") or 0
        duration_ms = event.get("dDurationMs") or 0
        out.append(
            {
                "start": float(cast(float, start_ms)) / 1000.0,
                "duration": float(cast(float, duration_ms)) / 1000.0,
                "text": text,
            }
        )
    return out


_VTT_TIME: Final = re.compile(
    r"(\d{2,}):(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(\d{2,}):(\d{2}):(\d{2})[.,](\d{3})"
)


def _parse_vtt(body: str) -> list[dict[str, Any]]:
    """Minimal WebVTT reader: cue timings plus text, tags stripped."""
    out: list[dict[str, Any]] = []
    lines = body.splitlines()
    index = 0
    while index < len(lines):
        match = _VTT_TIME.search(lines[index])
        if not match:
            index += 1
            continue
        groups = [int(g) for g in match.groups()]
        start = groups[0] * 3600 + groups[1] * 60 + groups[2] + groups[3] / 1000
        end = groups[4] * 3600 + groups[5] * 60 + groups[6] + groups[7] / 1000
        index += 1
        chunk: list[str] = []
        while index < len(lines) and lines[index].strip():
            chunk.append(re.sub(r"<[^>]+>", "", lines[index]))
            index += 1
        text = " ".join(chunk).strip()
        if text:
            out.append({"start": start, "duration": max(0.0, end - start), "text": text})
    return out


def _parse_srv(body: str) -> list[dict[str, Any]]:
    """YouTube's legacy XML caption formats (srv1/2/3)."""
    from xml.etree import ElementTree

    root = ElementTree.fromstring(body)
    out: list[dict[str, Any]] = []
    for node in root.iter():
        if node.tag not in {"text", "p"}:
            continue
        start = node.get("start") or node.get("t") or "0"
        duration = node.get("dur") or node.get("d") or "0"
        text = "".join(node.itertext())
        if not text.strip():
            continue
        scale = 1000.0 if node.get("t") is not None else 1.0
        out.append(
            {
                "start": float(start) / scale,
                "duration": float(duration) / scale,
                "text": text,
            }
        )
    return out


def make_fetcher(
    backend: str, *, cookies_file: str | None = None, proxy: str | None = None
) -> TranscriptFetcher:
    if backend == SOURCE_YTDLP:
        return YtDlpFetcher(cookies_file=cookies_file, proxy=proxy)
    if backend == SOURCE_YTA:
        return YouTubeTranscriptApiFetcher(cookies_file=cookies_file, proxy=proxy)
    raise ValueError(f"unknown fetcher backend {backend!r}")


# --------------------------------------------------------------------------- #
# The per-video pipeline
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FetchConfig:
    transcript_dir: Path
    languages: Sequence[str]
    prefer_manual: bool = True
    store_all_variants: bool = False
    accept_translated: bool = False
    max_attempts: int = 4
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 300.0
    cookies_configured: bool = False


@dataclass
class FetchOutcome:
    video_id: str
    status: Status | None
    blocked: bool = False
    transcripts: int = 0
    reason: str | None = None
    classification: Classification | None = None
    languages: list[str] = field(default_factory=list)


def fetch_video(
    repo: Repo,
    fetcher: TranscriptFetcher,
    video: VideoRow,
    config: FetchConfig,
    *,
    run_id: int | None,
    worker: str,
    before_request: Callable[[], None] | None = None,
) -> FetchOutcome:
    """Fetch, store and commit one video. Never raises for expected failures.

    A :class:`~yt_tx.classify.HardBlock` is the one thing that propagates: the
    caller has to trip the circuit breaker, and the video is left exactly as it
    was, queued and untouched.

    Raises:
        HardBlock: YouTube is refusing the fetcher.
    """
    started = time.monotonic()
    with context(video_id=video.video_id):
        try:
            return _fetch_video_inner(
                repo, fetcher, video, config,
                run_id=run_id, worker=worker, before_request=before_request,
                started=started,
            )
        except (HardBlock, KeyboardInterrupt, SystemExit):
            # Ctrl-C must reach the worker's drain path, not be filed as a
            # per-video failure.
            raise
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            result = classify(exc, cookies_configured=config.cookies_configured)
            if result.is_block:
                repo.record_block(
                    video.video_id,
                    run_id=run_id,
                    worker=worker,
                    error_type=result.error_type,
                    error_message=describe_exception(exc)[1],
                    http_status=result.http_status,
                )
                log.warning(
                    "blocked",
                    error_type=result.error_type,
                    reason=result.reason,
                    http_status=result.http_status,
                )
                raise HardBlock(result.reason, http_status=result.http_status) from exc
            return _apply_failure(
                repo, video, config, exc, result,
                run_id=run_id, worker=worker,
            )


def _fetch_video_inner(
    repo: Repo,
    fetcher: TranscriptFetcher,
    video: VideoRow,
    config: FetchConfig,
    *,
    run_id: int | None,
    worker: str,
    before_request: Callable[[], None] | None,
    started: float,
) -> FetchOutcome:
    # 1. List. Persisted regardless of what happens next.
    if before_request is not None:
        before_request()
    available = fetcher.list_available(video.video_id)
    inventory = [a.as_dict() for a in available]
    repo.set_available_transcripts(video.video_id, inventory)

    # 2. Select.
    try:
        selections = select_transcripts(
            available,
            languages=list(config.languages),
            prefer_manual=config.prefer_manual,
            store_all_variants=config.store_all_variants,
            accept_translated=config.accept_translated,
        )
    except NoTranscriptInLanguages as exc:
        reason = f"no transcript in configured languages: {exc}"
        repo.record_terminal(
            video.video_id,
            status=Status.LANG_MISSING,
            reason=reason,
            # Captions exist, just not in a configured language: that is a
            # translation problem, not an ASR job.
            needs_audio=False,
            available=inventory,
            run_id=run_id,
            worker=worker,
            error_type="NoTranscriptInLanguages",
            error_message=str(exc),
            schedule_recheck=True,
        )
        log.info("lang_missing", available=[a.language_code for a in available])
        return FetchOutcome(
            video.video_id, Status.LANG_MISSING, reason=reason,
            languages=[a.language_code for a in available],
        )

    # 3. Download, then store on disk before touching the database.
    writes: list[TranscriptWrite] = []
    for selection in selections:
        if before_request is not None:
            before_request()
        segments = fetcher.download(video.video_id, selection)
        if not segments:
            log.warning(
                "empty transcript returned",
                language=selection.stored_language, kind=selection.stored_kind.value,
            )
            continue

        plaintext = flatten(segments)
        path = transcript_path(
            config.transcript_dir,
            video.channel_id,
            video.video_id,
            selection.stored_language,
            selection.stored_kind,
        )
        stored = write_transcript_file(
            path,
            video_id=video.video_id,
            language=selection.stored_language,
            kind=selection.stored_kind,
            source=fetcher.name,
            segments=segments,
        )
        writes.append(
            TranscriptWrite(
                video_id=video.video_id,
                language_code=selection.stored_language,
                kind=selection.stored_kind,
                is_preferred=selection.is_preferred,
                segment_count=len(segments),
                char_count=len(plaintext),
                word_count=word_count(plaintext),
                covered_seconds=covered_seconds(segments),
                raw_path=str(path),
                raw_sha256=stored.sha256,
                plaintext=plaintext,
                source=fetcher.name,
            )
        )

    if not writes:
        reason = "every candidate transcript came back empty"
        repo.record_terminal(
            video.video_id,
            status=Status.NO_TRANSCRIPT,
            reason=reason,
            needs_audio=True,
            available=inventory,
            run_id=run_id,
            worker=worker,
            schedule_recheck=True,
        )
        return FetchOutcome(video.video_id, Status.NO_TRANSCRIPT, reason=reason)

    # 4. One transaction for transcripts, status and the attempt row.
    repo.record_transcript_success(
        video.video_id,
        writes,
        available=inventory,
        run_id=run_id,
        worker=worker,
        duration_seconds=time.monotonic() - started,
    )
    log.info(
        "fetch ok",
        languages=[w.language_code for w in writes],
        kind=writes[0].kind.value,
        segments=writes[0].segment_count,
        words=writes[0].word_count,
    )
    return FetchOutcome(
        video.video_id,
        Status.TRANSCRIPT_OK,
        transcripts=len(writes),
        languages=[w.language_code for w in writes],
    )


def _apply_failure(
    repo: Repo,
    video: VideoRow,
    config: FetchConfig,
    exc: BaseException,
    result: Classification,
    *,
    run_id: int | None,
    worker: str,
) -> FetchOutcome:
    from .classify import effective_max_attempts

    error_type, error_message = describe_exception(exc)

    if result.is_terminal:
        assert result.status is not None
        repo.record_terminal(
            video.video_id,
            status=result.status,
            reason=result.reason,
            needs_audio=result.needs_audio,
            run_id=run_id,
            worker=worker,
            error_type=error_type,
            error_message=error_message,
            http_status=result.http_status,
            schedule_recheck=result.status in {Status.NO_TRANSCRIPT, Status.LANG_MISSING},
        )
        log.info(
            "terminal", status=result.status.value, reason=result.reason,
            error_type=error_type,
        )
        return FetchOutcome(
            video.video_id, result.status, reason=result.reason, classification=result
        )

    cap = effective_max_attempts(result, config.max_attempts)
    delay = full_jitter_backoff(
        video.attempts,
        base=config.backoff_base_seconds,
        cap=config.backoff_cap_seconds,
        retry_after=_retry_after(exc),
    )
    # Keep the traceback only for genuinely unexplained failures; storing one for
    # every transient timeout would bloat fetch_attempts for no benefit.
    tb = (
        "".join(traceback.format_exception(exc))
        if result.max_attempts == 1
        else None
    )
    final = repo.record_retry(
        video.video_id,
        reason=result.reason,
        delay_seconds=delay,
        max_attempts=cap,
        run_id=run_id,
        worker=worker,
        error_type=error_type,
        error_message=error_message,
        http_status=result.http_status,
        traceback=tb,
    )
    log.warning(
        "retryable failure",
        status=final.value, attempts=video.attempts + 1, cap=cap,
        retry_in=round(delay, 1), error_type=error_type, reason=result.reason,
    )
    return FetchOutcome(
        video.video_id, final, reason=result.reason, classification=result
    )


def _retry_after(exc: BaseException) -> float | None:
    """Honour a ``Retry-After`` header when the server sent one."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
