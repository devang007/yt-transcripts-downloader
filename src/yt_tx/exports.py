"""Export transcripts as JSONL, plain text, or CSV.

Streaming throughout. A 100k-video corpus with plaintext is several gigabytes,
and materialising that in memory to hand to a browser would take the API process
down. Rows are read in keyset-paginated chunks so MySQL never holds a giant
result set open either.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy import text

from .logs import get_logger
from .repo import Repo

log = get_logger(__name__)

Format = Literal["jsonl", "txt", "csv"]

CHUNK: Final = 200

CSV_COLUMNS: Final[tuple[str, ...]] = (
    "video_id", "channel_id", "channel_handle", "title", "published_at",
    "duration_seconds", "view_count", "status", "language_code", "kind",
    "segment_count", "word_count", "char_count", "covered_seconds", "source",
    "url", "raw_path",
)

_EXPORT_COLUMNS: Final = """
SELECT t.id AS transcript_id,
       v.video_id, v.channel_id, c.handle AS channel_handle, v.title,
       v.published_at, v.duration_seconds, v.view_count, v.status,
       t.language_code, t.kind, t.segment_count, t.word_count, t.char_count,
       t.covered_seconds, t.source, t.raw_path, t.plaintext
  FROM transcripts t
  JOIN videos v   ON v.video_id = t.video_id
  LEFT JOIN channels c ON c.channel_id = v.channel_id
 WHERE t.is_preferred = 1
   {channel_filter}
"""

# Oldest upload first. The cursor is (published_at, transcript_id) rather than
# transcript_id alone: several videos can share a publish timestamp, and a
# cursor that cannot break that tie either loses rows or repeats them.
_EXPORT_DATED_SQL: Final = _EXPORT_COLUMNS + """
   AND v.published_at IS NOT NULL
   AND (v.published_at > :cursor_published
        OR (v.published_at = :cursor_published AND t.id > :cursor))
 ORDER BY v.published_at, t.id
 LIMIT :chunk
"""

# Undated videos go last rather than first (MySQL's own NULLS FIRST), so the
# chronological run is not preceded by an arbitrary block. Without a Data API
# key, enumeration records no publish date at all, so this is most of a corpus.
_EXPORT_UNDATED_SQL: Final = _EXPORT_COLUMNS + """
   AND v.published_at IS NULL
   AND t.id > :cursor
 ORDER BY t.id
 LIMIT :chunk
"""

# Below any real publish date; MySQL DATETIME bottoms out at 1000-01-01.
_EPOCH_FLOOR: Final = datetime(1000, 1, 1)


def watch_url(video_id: str, seconds: float | None = None) -> str:
    base = f"https://www.youtube.com/watch?v={video_id}"
    return base if not seconds else f"{base}&t={int(seconds)}s"


def iter_export_rows(
    repo: Repo, *, channel_id: str | None = None
) -> Iterator[dict[str, Any]]:
    """Stream preferred transcripts oldest upload first, then undated ones.

    Keyset rather than OFFSET: at a few hundred thousand rows, ``LIMIT n OFFSET m``
    re-scans everything before the offset on every page, and the export gets
    quadratically slower the further it gets.

    The two passes exist because ``published_at`` is nullable and most of a
    yt-dlp-enumerated corpus has no date. Sorting them together would bury the
    chronological run behind thousands of undated rows.
    """
    clause = "AND v.channel_id = :channel_id" if channel_id else ""
    base: dict[str, Any] = {"channel_id": channel_id} if channel_id else {}

    dated = text(_EXPORT_DATED_SQL.format(channel_filter=clause))
    cursor, cursor_published = 0, _EPOCH_FLOOR
    while True:
        params = {**base, "cursor": cursor, "cursor_published": cursor_published,
                  "chunk": CHUNK}
        with repo.connect() as conn:
            rows = conn.execute(dated, params).mappings().all()
        if not rows:
            break
        for row in rows:
            yield dict(row)
        cursor = int(rows[-1]["transcript_id"])
        cursor_published = rows[-1]["published_at"]

    undated = text(_EXPORT_UNDATED_SQL.format(channel_filter=clause))
    cursor = 0
    while True:
        params = {**base, "cursor": cursor, "chunk": CHUNK}
        with repo.connect() as conn:
            rows = conn.execute(undated, params).mappings().all()
        if not rows:
            return
        for row in rows:
            yield dict(row)
        cursor = int(rows[-1]["transcript_id"])


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None if value is None else str(value)


def _jsonl_record(row: dict[str, Any]) -> str:
    payload = {
        "video_id": row["video_id"],
        "channel_id": row["channel_id"],
        "channel_handle": row.get("channel_handle"),
        "title": row.get("title"),
        "published_at": _iso(row.get("published_at")),
        "duration_seconds": row.get("duration_seconds"),
        "view_count": row.get("view_count"),
        "url": watch_url(str(row["video_id"])),
        "language_code": row.get("language_code"),
        "kind": row.get("kind"),
        "segment_count": row.get("segment_count"),
        "word_count": row.get("word_count"),
        "covered_seconds": (
            float(row["covered_seconds"]) if row.get("covered_seconds") is not None
            else None
        ),
        "source": row.get("source"),
        "text": row.get("plaintext") or "",
    }
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _txt_record(row: dict[str, Any]) -> str:
    header = (
        f"# {row.get('title') or row['video_id']}\n"
        f"# {watch_url(str(row['video_id']))}\n"
        f"# channel: {row.get('channel_handle') or row['channel_id']}\n"
        f"# published: {_iso(row.get('published_at')) or 'unknown'}\n"
        f"# language: {row.get('language_code')} ({row.get('kind')})\n"
    )
    return f"{header}\n{row.get('plaintext') or ''}\n\n{'-' * 72}\n\n"


def _csv_row(row: dict[str, Any]) -> list[Any]:
    return [
        row["video_id"],
        row["channel_id"],
        row.get("channel_handle") or "",
        row.get("title") or "",
        _iso(row.get("published_at")) or "",
        row.get("duration_seconds") or "",
        row.get("view_count") or "",
        row.get("status") or "",
        row.get("language_code") or "",
        row.get("kind") or "",
        row.get("segment_count") or "",
        row.get("word_count") or "",
        row.get("char_count") or "",
        row.get("covered_seconds") or "",
        row.get("source") or "",
        watch_url(str(row["video_id"])),
        row.get("raw_path") or "",
    ]


def export_stream(
    repo: Repo, *, fmt: Format = "jsonl", channel_id: str | None = None
) -> Iterator[str]:
    """Yield an export a chunk at a time, suitable for a streaming response.

    CSV deliberately omits transcript text: a multi-megabyte cell per row makes
    the file unusable in every spreadsheet tool, and JSONL exists for the text.
    """
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_COLUMNS)
        yield buffer.getvalue()
        for row in iter_export_rows(repo, channel_id=channel_id):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(_csv_row(row))
            yield buffer.getvalue()
        return

    render = _jsonl_record if fmt == "jsonl" else _txt_record
    for row in iter_export_rows(repo, channel_id=channel_id):
        yield render(row)


def export_to_dir(
    repo: Repo,
    out: Path,
    *,
    fmt: Format = "jsonl",
    channel_id: str | None = None,
    per_video: bool = False,
) -> tuple[int, Path]:
    """Write an export to disk. Returns (records, path or directory).

    ``per_video`` with ``txt`` writes one file per video, which is what most
    downstream text tooling wants; otherwise everything goes to a single file.
    """
    out.mkdir(parents=True, exist_ok=True)

    if per_video and fmt == "txt":
        count = 0
        for row in iter_export_rows(repo, channel_id=channel_id):
            directory = out / str(row["channel_id"])
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{row['video_id']}.{row['language_code']}.txt"
            path.write_text(_txt_record(row), encoding="utf-8")
            count += 1
        log.info("export complete", records=count, out=str(out), format=fmt)
        return count, out

    suffix = channel_id or "all"
    target = out / f"yt-tx-{suffix}.{fmt}"
    count = 0
    with target.open("w", encoding="utf-8", newline="") as handle:
        for chunk in export_stream(repo, fmt=fmt, channel_id=channel_id):
            handle.write(chunk)
            count += 1
    if fmt == "csv":
        count = max(0, count - 1)  # the header row is not a record
    log.info("export complete", records=count, out=str(target), format=fmt)
    return count, target


def audio_queue_jsonl(repo: Repo, out: Path, *, limit: int | None = None) -> int:
    """Write the phase-2 work queue: every video with ``needs_audio=1``.

    Includes ``duration_seconds`` so the consumer can estimate GPU time and
    hard-skip anything over its threshold *before* downloading audio. At roughly
    30 MB per hour for 64 kbps mono opus, that check is the difference between a
    few gigabytes and a few hundred.
    """
    rows = repo.audio_queue(limit=limit)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    {
                        "video_id": row.video_id,
                        "channel_id": row.channel_id,
                        "title": row.title,
                        "url": watch_url(row.video_id),
                        "duration_seconds": row.duration_seconds,
                        "published_at": _iso(row.published_at),
                        "status": row.status.value,
                        "status_reason": row.status_reason,
                        "available_transcripts": row.available_transcripts,
                        "estimated_audio_mb": (
                            round((row.duration_seconds or 0) / 3600 * 30, 1)
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    total_seconds = sum(r.duration_seconds or 0 for r in rows)
    log.info(
        "audio queue exported",
        count=len(rows),
        out=str(out),
        total_hours=round(total_seconds / 3600, 1),
        estimated_audio_gb=round(total_seconds / 3600 * 30 / 1024, 1),
    )
    return len(rows)


def format_columns() -> Sequence[str]:
    return CSV_COLUMNS
