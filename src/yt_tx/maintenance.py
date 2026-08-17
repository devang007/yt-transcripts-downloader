"""``doctor`` and ``prune``: reconcile state, and stop tables growing forever.

``doctor`` checks both directions of the DB/disk relationship, because they are
not equally serious:

* **row without a file** - corruption. Something committed a transcript row while
  its gzip never landed, and every consumer of that row will now fail. Reported
  as an error, and ``--fix`` deletes the row so the video can be re-fetched.
* **file without a row** - harmless. Writes deliberately fsync the file *before*
  committing the row, so a crash in between leaves exactly this. Reported as
  reclaimable space, never as an error.

Everything else it looks at is something that makes the UI lie: stale leases that
hide work, and ``runs`` rows whose process died without closing them.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from . import db as ytdb
from .fetch import verify_transcript_file
from .logs import get_logger
from .repo import Repo
from .settings import Bootstrap
from .states import ExitReason

log = get_logger(__name__)

DURATION_RE: Final = re.compile(r"^(\d+)\s*([smhdw])$", re.I)
_UNIT_DAYS: Final[dict[str, float]] = {
    "s": 1 / 86400, "m": 1 / 1440, "h": 1 / 24, "d": 1.0, "w": 7.0
}


def parse_age(value: str) -> int:
    """``30d`` -> 30, ``6h`` -> 1 (rounded up). Days is the unit MySQL gets."""
    match = DURATION_RE.match(value.strip())
    if not match:
        raise ValueError(f"unparsable duration {value!r}; use e.g. 30d, 12h, 2w")
    amount = int(match.group(1))
    days = amount * _UNIT_DAYS[match.group(2).lower()]
    return max(1, int(days + 0.999))


@dataclass
class DoctorReport:
    ok: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    fixes: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.errors

    def as_text(self) -> str:
        lines: list[str] = ["yt-tx doctor", "=" * 60]
        for key, value in self.facts.items():
            lines.append(f"  {key:<28} {value}")
        lines.append("")
        for message in self.ok:
            lines.append(f"  [ ok ] {message}")
        for message in self.warnings:
            lines.append(f"  [warn] {message}")
        for message in self.errors:
            lines.append(f"  [FAIL] {message}")
        for message in self.fixes:
            lines.append(f"  [fix ] {message}")
        lines.append("")
        lines.append(
            "healthy" if self.healthy else f"{len(self.errors)} problem(s) found"
        )
        return "\n".join(lines)


def doctor(
    repo: Repo,
    bootstrap: Bootstrap,
    *,
    fix: bool = False,
    deep: bool = False,
) -> DoctorReport:
    """Reconcile database and disk, and report anything that makes state lie.

    Args:
        fix: Delete transcript rows whose file is missing, close orphan runs, and
            reset stale leases. Never deletes files.
        deep: Also verify every stored sha256, which reads every transcript on
            disk. Slow, and worth it after a disk scare.
    """
    report = DoctorReport()

    # -- server ------------------------------------------------------------ #
    try:
        with repo.connect() as conn:
            diag = ytdb.check_server(conn)
            missing = ytdb.missing_tables(conn)
            has_ft = ytdb.fulltext_index_exists(conn)
            min_token = ytdb.ft_min_token_size(conn)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"cannot reach MySQL: {exc}")
        return report

    report.facts["mysql version"] = diag["version"]
    report.facts["connection charset"] = diag["charset_connection"]
    report.facts["session time_zone"] = diag["time_zone"]
    report.facts["max_allowed_packet"] = f"{int(diag['max_allowed_packet']) // 1024**2} MB"
    report.facts["transcript_dir"] = str(bootstrap.transcript_dir)
    report.facts["log_dir"] = str(bootstrap.log_dir)

    for warning in diag["warnings"]:
        report.warnings.append(warning)
    if missing:
        report.errors.append(
            f"missing tables: {', '.join(missing)} - run `yt-tx init`"
        )
        return report
    report.ok.append("schema is complete")

    if has_ft:
        report.ok.append("transcript FULLTEXT index is present")
    else:
        report.warnings.append(
            "transcript FULLTEXT index is missing; search falls back to LIKE. "
            "Rebuild with `yt-tx fulltext --build`."
        )
    if min_token > 2:
        report.warnings.append(
            f"innodb_ft_min_token_size is {min_token}, so words shorter than "
            f"{min_token} characters are unsearchable"
        )

    # -- counts ------------------------------------------------------------ #
    stats = repo.stats()
    report.facts["videos"] = stats.total
    report.facts["transcripts"] = stats.transcripts
    report.facts["coverage"] = f"{stats.coverage_pct}%"
    report.facts["needs_audio"] = stats.needs_audio
    report.facts["remaining"] = stats.remaining

    # -- leases ------------------------------------------------------------ #
    stale = repo.count_stale_leases()
    if stale:
        if fix:
            reaped = repo.reap_expired_leases()
            report.fixes.append(f"returned {reaped} expired lease(s) to the queue")
        else:
            report.warnings.append(
                f"{stale} row(s) hold expired leases; they are hidden from the "
                "claim query's point of view until reaped. Run with --fix."
            )
    else:
        report.ok.append("no stale leases")

    # -- runs -------------------------------------------------------------- #
    from .api import process_is_alive

    active = repo.active_runs()
    dead = [r for r in active if r.pid is None or not process_is_alive(r.pid)]
    if dead:
        if fix:
            for run in dead:
                repo.finish_run(run.id, exit_reason=ExitReason.CRASHED)
            report.fixes.append(f"closed {len(dead)} orphaned run row(s)")
        else:
            report.errors.append(
                f"run(s) {[r.id for r in dead]} are marked active but their "
                "process is gone; the UI will show a phantom RUNNING. Run with --fix."
            )
    elif active:
        report.ok.append(f"run {active[0].id} is genuinely active (pid {active[0].pid})")
    else:
        report.ok.append("no active runs")

    # -- disk: rows pointing at nothing ------------------------------------ #
    missing_files = repo.transcript_rows_with_missing_files()
    if missing_files:
        detail = ", ".join(f"{v}({p})" for _, v, p in missing_files[:5])
        if fix:
            deleted = repo.delete_transcript_rows([row_id for row_id, _, _ in missing_files])
            report.fixes.append(
                f"deleted {deleted} transcript row(s) whose file was missing; "
                "those videos can now be re-fetched"
            )
        else:
            report.errors.append(
                f"{len(missing_files)} transcript row(s) point at files that do "
                f"not exist (e.g. {detail}). This is the corruption direction. "
                "Run with --fix to delete the rows and re-fetch."
            )
    else:
        report.ok.append("every transcript row has its file on disk")

    # -- disk: files nobody references ------------------------------------- #
    known = {Path(path).resolve() for _, path, _ in repo.all_transcript_paths()}
    orphans: list[Path] = []
    orphan_bytes = 0
    root = bootstrap.transcript_dir
    if root.exists():
        for path in root.rglob("*.json.gz"):
            resolved = path.resolve()
            if resolved not in known:
                orphans.append(path)
                try:
                    orphan_bytes += path.stat().st_size
                except OSError:
                    pass
    if orphans:
        report.warnings.append(
            f"{len(orphans)} transcript file(s) on disk have no database row "
            f"({orphan_bytes / 1024:.0f} KB reclaimable). Harmless: files are "
            "fsynced before the row is committed, so a crash leaves exactly this."
        )
        report.facts["orphan files"] = len(orphans)
    else:
        report.ok.append("no orphaned transcript files")

    # -- leftover temp files ----------------------------------------------- #
    if root.exists():
        temps = list(root.rglob(".*.tmp"))
        if temps:
            if fix:
                removed = 0
                for path in temps:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
                report.fixes.append(f"removed {removed} interrupted temp file(s)")
            else:
                report.warnings.append(
                    f"{len(temps)} interrupted temp file(s) from a killed write; "
                    "remove with --fix"
                )

    # -- deep hash verification -------------------------------------------- #
    if deep:
        started = time.monotonic()
        checked = 0
        corrupt: list[str] = []
        for video_id, raw_path, sha in repo.all_transcript_paths():
            path = Path(raw_path)
            if not path.is_absolute():
                path = Path.cwd() / path
            if not path.exists():
                continue
            checked += 1
            if not verify_transcript_file(path, sha):
                corrupt.append(f"{video_id} ({raw_path})")
        report.facts["hashes verified"] = f"{checked} in {time.monotonic() - started:.1f}s"
        if corrupt:
            report.errors.append(
                f"{len(corrupt)} transcript file(s) do not match their stored "
                f"sha256: {', '.join(corrupt[:5])}"
            )
        else:
            report.ok.append(f"all {checked} transcript file(s) match their sha256")

    # -- configuration ----------------------------------------------------- #
    stored = repo.get_settings()
    if not stored.get("youtube_api_key"):
        report.warnings.append(
            "no Data API key configured; enumeration and metadata fall back to "
            "yt-dlp, which works but is far slower per video"
        )
    cookies = stored.get("cookies_file")
    if cookies and not Path(str(cookies)).exists():
        report.errors.append(f"cookies_file is set to {cookies!r} but does not exist")
    if not bootstrap.transcript_dir.exists():
        report.warnings.append(
            f"transcript_dir {bootstrap.transcript_dir} does not exist yet"
        )
    if not bootstrap.web.is_loopback and not bootstrap.web.auth_token:
        report.errors.append(
            "web.host is not loopback and no auth_token is set; the settings "
            "endpoint would expose an API key"
        )

    return report


# --------------------------------------------------------------------------- #
# prune
# --------------------------------------------------------------------------- #


@dataclass
class PruneReport:
    attempts_deleted: int = 0
    logs_deleted: int = 0
    log_bytes_freed: int = 0

    def as_text(self) -> str:
        return (
            f"pruned {self.attempts_deleted} fetch_attempts row(s), "
            f"{self.logs_deleted} log file(s) "
            f"({self.log_bytes_freed / 1024 / 1024:.1f} MB freed)"
        )


def prune(
    repo: Repo, bootstrap: Bootstrap, *, older_than: str = "30d", logs: bool = True
) -> PruneReport:
    """Delete old attempt rows and run logs.

    ``fetch_attempts`` gains a row per attempt per video, so on a large corpus it
    outgrows every other table combined. Worth running from day one rather than
    discovering it at 40 million rows.
    """
    days = parse_age(older_than)
    report = PruneReport(attempts_deleted=repo.prune_attempts(days))

    if logs:
        cutoff = time.time() - days * 86400
        active_logs = {
            r.log_path for r in repo.list_runs(limit=500) if r.is_active and r.log_path
        }
        for path in sorted(bootstrap.log_dir.glob("run-*.jsonl")):
            if str(path) in active_logs:
                continue
            try:
                stat = path.stat()
                if stat.st_mtime >= cutoff:
                    continue
                size = stat.st_size
                path.unlink()
            except OSError as exc:
                log.warning("could not remove log", path=str(path), error=str(exc))
                continue
            report.logs_deleted += 1
            report.log_bytes_freed += size

    log.info(
        "prune complete",
        older_than_days=days,
        attempts=report.attempts_deleted,
        logs=report.logs_deleted,
    )
    return report


def iter_orphan_files(repo: Repo, bootstrap: Bootstrap) -> Iterator[Path]:
    known = {Path(path).resolve() for _, path, _ in repo.all_transcript_paths()}
    root = bootstrap.transcript_dir
    if not root.exists():
        return
    for path in root.rglob("*.json.gz"):
        if path.resolve() not in known:
            yield path


def reclaim_orphans(repo: Repo, bootstrap: Bootstrap) -> tuple[int, int]:
    """Delete transcript files with no database row. Returns (count, bytes)."""
    count = 0
    freed = 0
    for path in iter_orphan_files(repo, bootstrap):
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        count += 1
        freed += size
    if count:
        log.info("reclaimed orphan transcript files", count=count, bytes=freed)
    return count, freed


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total
