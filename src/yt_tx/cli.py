"""``yt-tx`` command line.

Every command that touches the network is Ctrl-C safe by construction: the
:class:`~yt_tx.worker.Pipeline` installs signal handlers that stop claiming and
drain, and :func:`_run_stage` always closes the ``runs`` row with an accurate
``exit_reason`` and exits with a code that says what happened
(0 completed, 130 interrupted, 4 circuit open).

``--run-id`` exists so the web API can create the ``runs`` row itself, hand the
id to the subprocess, and start tailing the log before the worker is even up.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from . import db as ytdb
from .logs import close as close_logs
from .logs import configure as configure_logs
from .logs import get_logger, run_log_path
from .repo import Repo
from .settings import Bootstrap, ConfigError, Knobs, load_bootstrap
from .states import DesiredState, ExitReason, Status
from .worker import Pipeline, exit_code_for

log = get_logger(__name__)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="YouTube transcript harvester: enumerate, persist, fetch, resume.",
    pretty_exceptions_show_locals=False,
)

ConfigOption = Annotated[
    Optional[Path],
    typer.Option("--config", "-c", help="Path to config.yaml.", show_default=False),
]
ChannelOption = Annotated[
    Optional[str],
    typer.Option("--channel", help="Restrict to one channel id.", show_default=False),
]
LimitOption = Annotated[
    Optional[int],
    typer.Option("--limit", min=1, help="Stop after this many videos.", show_default=False),
]
RunIdOption = Annotated[
    Optional[int],
    typer.Option("--run-id", help="Use an existing runs row (set by the API)."),
]
VerboseOption = Annotated[
    bool, typer.Option("--verbose", "-v", help="Debug-level logging.")
]
SkipHydrateOption = Annotated[
    bool,
    typer.Option(
        "--skip-hydrate/--hydrate",
        help="Claim discovered videos directly instead of fetching metadata "
             "first. The default: transcripts need no metadata, and without an "
             "API key the metadata pass costs one request per video. "
             "--hydrate restores it, filling duration and view counts and "
             "applying the shorts/streams/duration skip rules.",
    ),
]


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def _bootstrap(path: Path | None) -> Bootstrap:
    try:
        return load_bootstrap(path)
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


def _repo(boot: Bootstrap) -> Repo:
    return Repo(ytdb.make_engine(boot.mysql))


def _knobs(repo: Repo, boot: Bootstrap) -> Knobs:
    """DB settings win over YAML seeds, which win over dataclass defaults."""
    merged: dict[str, Any] = dict(boot.seeds)
    merged.update(repo.get_settings())
    return Knobs.from_mapping(merged)


def _require_schema(repo: Repo) -> None:
    with repo.connect() as conn:
        missing = ytdb.missing_tables(conn)
    if missing:
        typer.secho(
            f"schema is incomplete (missing: {', '.join(missing)}). "
            "Run `yt-tx init` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)


def _run_stage(
    *,
    command: str,
    config: Path | None,
    verbose: bool,
    run_id: int | None,
    args: dict[str, Any],
    body: Any,
) -> None:
    """Shared lifecycle for every networked command.

    Creates or adopts the ``runs`` row, points logging at that run's JSONL file,
    installs signal handlers, runs ``body(pipeline)``, and closes the row with the
    right exit reason no matter how the body ended.
    """
    boot = _bootstrap(config)
    boot.ensure_dirs()
    repo = _repo(boot)
    _require_schema(repo)

    owns_run = run_id is None
    if owns_run:
        run_id = repo.create_run(command, args=args)
    assert run_id is not None

    log_path = run_log_path(boot.log_dir, run_id)
    configure_logs(
        level="debug" if verbose else "info", jsonl_path=log_path, stderr=True
    )
    if owns_run:
        repo.set_run_pid(run_id, pid=_pid(), log_path=str(log_path))

    knobs = _knobs(repo, boot)
    pipeline = Pipeline(repo, boot, knobs, run_id=run_id)
    pipeline.install_signal_handlers()

    log.info(
        "run starting",
        command=command, run_id=run_id, args=args, log=str(log_path),
        backend=knobs.fetcher, concurrency=knobs.concurrency,
    )

    reason = ExitReason.COMPLETED
    try:
        with pipeline.supervised():
            body(pipeline)
        reason = pipeline.exit_reason()
    except KeyboardInterrupt:
        reason = ExitReason.INTERRUPTED
        log.warning("interrupted")
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised as an exit
        reason = ExitReason.CRASHED
        log.error(
            "run crashed", error=str(exc)[:1000], error_type=type(exc).__name__,
            exc_info=True,
        )
    finally:
        counts = pipeline.status_snapshot()
        repo.finish_run(run_id, exit_reason=reason, counts=counts)
        # Leave the control table usable for the next run rather than stuck on
        # 'stopping' forever.
        if repo.get_control().desired_state is DesiredState.STOPPING:
            repo.set_control(desired_state=DesiredState.RUNNING)
        log.info("run finished", exit_reason=reason.value, counts=counts)
        close_logs()
        repo.engine.dispose()

    code = exit_code_for(reason)
    if code:
        raise typer.Exit(code)


def _pid() -> int:
    import os

    return os.getpid()


# --------------------------------------------------------------------------- #
# init / serve / doctor
# --------------------------------------------------------------------------- #


@app.command()
def init(
    config: ConfigOption = None,
    reseed: Annotated[
        bool, typer.Option("--reseed", help="Overwrite settings from config.yaml.")
    ] = False,
) -> None:
    """Create the database and schema, seed settings, make directories."""
    configure_logs()
    boot = _bootstrap(config)
    boot.ensure_dirs()
    typer.echo(f"transcript_dir  {boot.transcript_dir}")
    typer.echo(f"log_dir         {boot.log_dir}")

    try:
        ytdb.create_database(boot.mysql)
    except Exception as exc:  # noqa: BLE001
        typer.secho(
            f"could not create database {boot.mysql.database!r}: {exc}\n"
            f"dsn: {boot.mysql.dsn(hide_password=True)}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(2) from exc

    repo = _repo(boot)
    try:
        with repo.begin() as conn:
            diag = ytdb.check_server(conn)
            ytdb.create_schema(conn)
        typer.secho(f"schema ready on MySQL {diag['version']}", fg=typer.colors.GREEN)
        for warning in diag["warnings"]:
            typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW)

        if reseed:
            repo.put_settings(dict(boot.seeds))
            written = sorted(boot.seeds)
        else:
            written = repo.seed_settings(dict(boot.seeds))
        typer.echo(
            f"settings seeded: {', '.join(written) if written else '(already present)'}"
        )
        knobs = _knobs(repo, boot)
        if not knobs.youtube_api_key:
            typer.secho(
                "no YOUTUBE_API_KEY configured - enumeration will use yt-dlp, "
                "which works but is much slower per video.",
                fg=typer.colors.YELLOW,
            )
        typer.secho("init complete. Next: yt-tx channels add @someone", fg=typer.colors.GREEN)
    finally:
        repo.engine.dispose()


@app.command()
def serve(
    config: ConfigOption = None,
    host: Annotated[Optional[str], typer.Option("--host")] = None,
    port: Annotated[Optional[int], typer.Option("--port")] = None,
) -> None:
    """Run the web UI and REST API."""
    configure_logs()
    from .api import serve as serve_app

    boot = _bootstrap(config)
    try:
        serve_app(boot, host=host, port=port)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


@app.command()
def doctor(
    config: ConfigOption = None,
    fix: Annotated[bool, typer.Option("--fix", help="Repair what is safely repairable.")] = False,
    deep: Annotated[bool, typer.Option("--deep", help="Verify every stored sha256.")] = False,
) -> None:
    """Reconcile database against disk; report anything that makes state lie."""
    configure_logs(stderr=False)
    from .maintenance import doctor as run_doctor

    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        report = run_doctor(repo, boot, fix=fix, deep=deep)
    finally:
        repo.engine.dispose()
    typer.echo(report.as_text())
    if not report.healthy:
        raise typer.Exit(1)


@app.command()
def fulltext(
    config: ConfigOption = None,
    build: Annotated[bool, typer.Option("--build", help="Create the index.")] = False,
    drop: Annotated[bool, typer.Option("--drop", help="Drop it before a bulk load.")] = False,
) -> None:
    """Manage the transcript FULLTEXT index.

    A FULLTEXT index roughly triples insert cost, so for a first pass over
    hundreds of thousands of videos: ``--drop``, harvest, then ``--build``.
    """
    configure_logs()
    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        with repo.begin() as conn:
            if drop:
                typer.echo("dropped" if ytdb.drop_fulltext_index(conn) else "not present")
            elif build:
                typer.echo("built" if ytdb.build_fulltext_index(conn) else "already present")
            else:
                present = ytdb.fulltext_index_exists(conn)
                typer.echo(f"fulltext index: {'present' if present else 'absent'}")
                typer.echo(f"innodb_ft_min_token_size: {ytdb.ft_min_token_size(conn)}")
    finally:
        repo.engine.dispose()


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #

channels_app = typer.Typer(help="Manage channels.", no_args_is_help=True)
app.add_typer(channels_app, name="channels")


@channels_app.command("add")
def channels_add(
    refs: Annotated[Optional[list[str]], typer.Argument(help="@handle, UC id, or URL.")] = None,
    file: Annotated[
        Optional[Path], typer.Option("--file", help="Newline-delimited refs.")
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Resolve and store channels. Resolution is cached and never repeated."""
    configure_logs()
    from .discover import ResolveError, resolve_channel
    from .youtube_api import YouTubeAPI

    entries = list(refs or [])
    if file:
        if not file.exists():
            typer.secho(f"no such file: {file}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        entries += [
            line.strip()
            for line in file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not entries:
        typer.secho("nothing to add: pass refs or --file", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    boot = _bootstrap(config)
    repo = _repo(boot)
    _require_schema(repo)
    knobs = _knobs(repo, boot)
    api = (
        YouTubeAPI(knobs.youtube_api_key, proxy=knobs.proxy)
        if knobs.youtube_api_key
        else None
    )

    failures = 0
    try:
        for ref in entries:
            try:
                row = resolve_channel(repo, ref, api=api)
            except (ResolveError, Exception) as exc:  # noqa: BLE001
                failures += 1
                typer.secho(f"  x {ref}: {exc}", fg=typer.colors.RED)
                continue
            typer.secho(
                f"  + {row.handle or row.channel_id}  {row.title or ''} "
                f"({row.reported_video_count or '?'} videos)",
                fg=typer.colors.GREEN,
            )
    finally:
        if api is not None:
            api.close()
        repo.engine.dispose()

    if failures:
        raise typer.Exit(1)


@channels_app.command("list")
def channels_list(config: ConfigOption = None) -> None:
    """List channels with coverage."""
    configure_logs(stderr=False)
    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        rows = repo.channel_stats()
    finally:
        repo.engine.dispose()
    if not rows:
        typer.echo("no channels yet: yt-tx channels add @someone")
        return
    typer.echo(
        f"{'CHANNEL':<28}{'TOTAL':>8}{'DONE':>8}{'NO-TX':>8}{'FAIL':>7}{'COVER':>8}  ENUM"
    )
    for row in rows:
        flag = "" if row.is_enabled else " (disabled)"
        typer.echo(
            f"{(row.handle or row.channel_id)[:27]:<28}"
            f"{row.total:>8}{row.done:>8}{row.no_transcript:>8}{row.failed:>7}"
            f"{row.coverage_pct:>7.1f}%  "
            f"{'complete' if row.enumeration_complete else 'partial'}{flag}"
        )


@channels_app.command("remove")
def channels_remove(
    channel_id: Annotated[str, typer.Argument()],
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Delete a channel and, by cascade, its videos and transcript rows."""
    configure_logs()
    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        counts = repo.status_counts(channel_id=channel_id)
        total = sum(counts.values())
        if not yes:
            typer.confirm(
                f"delete {channel_id} and {total} video row(s)? "
                "(transcript files stay on disk; `doctor` can reclaim them)",
                abort=True,
            )
        if repo.delete_channel(channel_id):
            typer.secho(f"deleted {channel_id}", fg=typer.colors.GREEN)
        else:
            typer.secho("no such channel", fg=typer.colors.RED, err=True)
            raise typer.Exit(1)
    finally:
        repo.engine.dispose()


# --------------------------------------------------------------------------- #
# pipeline stages
# --------------------------------------------------------------------------- #


@app.command()
def discover(
    config: ConfigOption = None,
    channel: ChannelOption = None,
    incremental: Annotated[
        bool,
        typer.Option("--incremental", help="RSS check first; skip unchanged channels."),
    ] = False,
    limit: LimitOption = None,
    run_id: RunIdOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Enumerate videos into the database, resuming mid-channel if interrupted."""
    _run_stage(
        command="discover",
        config=config,
        verbose=verbose,
        run_id=run_id,
        args={"channel": channel, "incremental": incremental, "limit": limit},
        body=lambda p: p.discover(
            channel_id=channel, incremental=incremental, limit=limit
        ),
    )


@app.command()
def hydrate(
    config: ConfigOption = None,
    channel: ChannelOption = None,
    limit: LimitOption = None,
    run_id: RunIdOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Fetch metadata for discovered videos, 50 per API call."""
    _run_stage(
        command="hydrate",
        config=config,
        verbose=verbose,
        run_id=run_id,
        args={"channel": channel, "limit": limit},
        body=lambda p: p.hydrate(channel_id=channel, limit=limit),
    )


@app.command()
def fetch(
    config: ConfigOption = None,
    channel: ChannelOption = None,
    limit: LimitOption = None,
    concurrency: Annotated[
        Optional[int], typer.Option("--concurrency", min=1, max=32)
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would be fetched.")
    ] = False,
    skip_hydrate: SkipHydrateOption = True,
    run_id: RunIdOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Download transcripts for everything queued."""
    boot = _bootstrap(config)
    if concurrency is not None:
        # Written to runtime_control so the value is visible in the UI and picked
        # up by any already-running worker too.
        repo = _repo(boot)
        try:
            repo.set_control(concurrency=concurrency)
        finally:
            repo.engine.dispose()

    _run_stage(
        command="fetch",
        config=config,
        verbose=verbose,
        run_id=run_id,
        args={
            "channel": channel, "limit": limit,
            "concurrency": concurrency, "dry_run": dry_run,
            "skip_hydrate": skip_hydrate,
        },
        body=lambda p: p.fetch(
            channel_id=channel, limit=limit, dry_run=dry_run,
            skip_hydrate=skip_hydrate,
        ),
    )


@app.command()
def run(
    config: ConfigOption = None,
    channel: ChannelOption = None,
    incremental: Annotated[bool, typer.Option("--incremental")] = False,
    limit: LimitOption = None,
    skip_hydrate: SkipHydrateOption = True,
    run_id: RunIdOption = None,
    verbose: VerboseOption = False,
) -> None:
    """discover, then fetch. Add --hydrate for the metadata stage in between."""
    _run_stage(
        command="run",
        config=config,
        verbose=verbose,
        run_id=run_id,
        args={
            "channel": channel, "incremental": incremental, "limit": limit,
            "skip_hydrate": skip_hydrate,
        },
        body=lambda p: p.run_all(
            channel_id=channel, incremental=incremental, limit=limit,
            skip_hydrate=skip_hydrate,
        ),
    )


@app.command()
def retry(
    config: ConfigOption = None,
    channel: ChannelOption = None,
    failed: Annotated[
        bool, typer.Option("--failed", help="Reopen videos that exhausted their retries.")
    ] = False,
    force_recheck: Annotated[
        bool,
        typer.Option(
            "--force-recheck",
            help="Reopen no_transcript / lang_missing regardless of recheck_after.",
        ),
    ] = False,
    age_restricted: Annotated[
        bool,
        typer.Option("--age-restricted", help="Reopen age-restricted (needs cookies)."),
    ] = False,
    run_id: RunIdOption = None,
    verbose: VerboseOption = False,
) -> None:
    """Requeue videos, then fetch them."""
    if not (failed or force_recheck or age_restricted):
        typer.secho(
            "pick at least one of --failed, --force-recheck, --age-restricted",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(2)

    def body(pipeline: Pipeline) -> None:
        statuses: list[Status] = []
        if failed:
            statuses.append(Status.FAILED)
        if force_recheck:
            statuses += [Status.NO_TRANSCRIPT, Status.LANG_MISSING]
        if age_restricted:
            if not pipeline.knobs.cookies_file:
                log.warning(
                    "reopening age-restricted videos without cookies configured; "
                    "they will fail again for exactly the same reason"
                )
            statuses.append(Status.AGE_RESTRICTED)

        reopened = pipeline.repo.reopen(
            statuses=statuses,
            channel_id=channel,
            reason="reopened by yt-tx retry",
        )
        log.info("reopened videos", count=reopened,
                 statuses=[s.value for s in statuses])
        pipeline.fetch(channel_id=channel)

    _run_stage(
        command="retry",
        config=config,
        verbose=verbose,
        run_id=run_id,
        args={
            "channel": channel, "failed": failed,
            "force_recheck": force_recheck, "age_restricted": age_restricted,
        },
        body=body,
    )


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #


@app.command()
def stats(
    config: ConfigOption = None,
    channel: ChannelOption = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show progress."""
    configure_logs(stderr=False)
    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        if channel:
            counts = repo.status_counts(channel_id=channel)
            payload: dict[str, Any] = {"channel_id": channel, "by_status": counts,
                                       "total": sum(counts.values())}
        else:
            snapshot = repo.stats()
            eta = snapshot.eta_seconds()
            payload = {
                "total": snapshot.total,
                "remaining": snapshot.remaining,
                "coverage_pct": snapshot.coverage_pct,
                "by_status": snapshot.by_status,
                "transcripts": snapshot.transcripts,
                "needs_audio": snapshot.needs_audio,
                "videos_per_minute": snapshot.videos_per_minute,
                "eta_seconds": list(eta) if eta else None,
                "quota_used": snapshot.quota_used,
                "quota_day": snapshot.quota_day.isoformat(),
                "active_run": snapshot.active_run.id if snapshot.active_run else None,
            }
    finally:
        repo.engine.dispose()

    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    total = payload.get("total", 0)
    typer.echo(f"total videos      {total}")
    for status in Status:
        count = payload["by_status"].get(status.value, 0)
        if count:
            share = f"{100.0 * count / total:.1f}%" if total else "-"
            typer.echo(f"  {status.value:<16}{count:>8}  {share:>7}")
    if "coverage_pct" in payload:
        typer.echo(f"coverage          {payload['coverage_pct']}%")
        typer.echo(f"needs audio       {payload['needs_audio']}")
        typer.echo(f"throughput        {payload['videos_per_minute']} vid/min")
        eta = payload.get("eta_seconds")
        if eta:
            typer.echo(f"eta               {eta[0] // 60}-{eta[1] // 60} min")
        typer.echo(f"quota             {payload['quota_used']} units "
                   f"({payload['quota_day']})")


@app.command("audio-queue")
def audio_queue(
    out: Annotated[Path, typer.Option("--out", help="Destination JSONL.")],
    config: ConfigOption = None,
    limit: LimitOption = None,
) -> None:
    """Export the phase-2 queue: every video with needs_audio=1."""
    configure_logs()
    from .exports import audio_queue_jsonl

    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        count = audio_queue_jsonl(repo, out, limit=limit)
    finally:
        repo.engine.dispose()
    typer.secho(f"wrote {count} video(s) to {out}", fg=typer.colors.GREEN)


@app.command()
def export(
    out: Annotated[Path, typer.Option("--out", help="Destination directory.")],
    config: ConfigOption = None,
    format: Annotated[str, typer.Option("--format", help="jsonl | txt | csv")] = "jsonl",
    channel: ChannelOption = None,
    per_video: Annotated[
        bool, typer.Option("--per-video", help="One .txt per video (txt only).")
    ] = False,
) -> None:
    """Export stored transcripts."""
    configure_logs()
    if format not in {"jsonl", "txt", "csv"}:
        typer.secho(f"unknown format {format!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    from .exports import export_to_dir

    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        count, target = export_to_dir(
            repo, out,
            fmt=format,  # type: ignore[arg-type]
            channel_id=channel,
            per_video=per_video,
        )
    finally:
        repo.engine.dispose()
    typer.secho(f"exported {count} transcript(s) to {target}", fg=typer.colors.GREEN)


@app.command()
def prune(
    config: ConfigOption = None,
    older_than: Annotated[
        str, typer.Option("--older-than", help="e.g. 30d, 12h, 2w")
    ] = "30d",
    keep_logs: Annotated[bool, typer.Option("--keep-logs")] = False,
    reclaim_orphan_files: Annotated[
        bool,
        typer.Option(
            "--reclaim-orphans",
            help="Also delete transcript files with no database row.",
        ),
    ] = False,
) -> None:
    """Delete old fetch_attempts rows and run logs."""
    configure_logs()
    from .maintenance import prune as run_prune
    from .maintenance import reclaim_orphans

    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        report = run_prune(repo, boot, older_than=older_than, logs=not keep_logs)
        typer.echo(report.as_text())
        if reclaim_orphan_files:
            count, freed = reclaim_orphans(repo, boot)
            typer.echo(
                f"reclaimed {count} orphan transcript file(s) "
                f"({freed / 1024 / 1024:.1f} MB)"
            )
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    finally:
        repo.engine.dispose()


@app.command()
def control(
    config: ConfigOption = None,
    pause: Annotated[bool, typer.Option("--pause")] = False,
    resume: Annotated[bool, typer.Option("--resume")] = False,
    stop: Annotated[bool, typer.Option("--stop")] = False,
    concurrency: Annotated[Optional[int], typer.Option("--concurrency", min=1, max=32)] = None,
    rps: Annotated[Optional[float], typer.Option("--rps", min=0.01, max=20.0)] = None,
) -> None:
    """Adjust a running worker without restarting it (the CLI half of the UI's sliders)."""
    configure_logs(stderr=False)
    boot = _bootstrap(config)
    repo = _repo(boot)
    try:
        desired: DesiredState | None = None
        if stop:
            desired = DesiredState.STOPPING
        elif pause:
            desired = DesiredState.PAUSED
        elif resume:
            desired = DesiredState.RUNNING
        if desired or concurrency or rps:
            repo.set_control(
                desired_state=desired, concurrency=concurrency, requests_per_second=rps
            )
        current = repo.get_control()
        typer.echo(
            f"desired_state        {current.desired_state.value}\n"
            f"concurrency          {current.concurrency}\n"
            f"requests_per_second  {current.requests_per_second}"
        )
        active = repo.active_run()
        typer.echo(
            f"active run           {active.id} (pid {active.pid})" if active
            else "active run           none"
        )
    finally:
        repo.engine.dispose()


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
