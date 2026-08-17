"""FastAPI control plane.

The pipeline never runs in this process. ``POST /api/runs`` spawns ``yt-tx`` as a
subprocess with a **list** argv and ``shell=False``, records its PID, and from
then on communicates only through MySQL (``runtime_control``) and the run's JSONL
log file. Channel references come from user input and end up adjacent to a
command line, so they are never interpolated into a shell string.

Three details that are easy to get wrong and unpleasant to debug:

* **Orphan recovery on startup.** Any ``runs`` row with ``finished_at IS NULL``
  whose PID is gone is marked ``crashed`` and its leases released. Without this
  the UI shows a phantom RUNNING forever after a reboot and refuses to start
  anything new.
* **SSE, not WebSocket.** Log traffic is one-directional and ``EventSource``
  reconnects for free. Reconnection replays the gap via ``Last-Event-ID``.
  Behind nginx this needs ``proxy_buffering off``; the ``X-Accel-Buffering: no``
  header here is the half we can control.
* **Binding.** Loopback by default. The settings page holds an API key and a
  cookies path, so binding a routable interface without an auth token is refused
  outright rather than warned about.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final, Literal, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import db as ytdb
from .logs import get_logger, run_log_path
from .repo import Repo, quota_day
from .settings import (
    KNOB_SPECS,
    LIVE_KNOBS,
    SECRET_KNOBS,
    Bootstrap,
    ConfigError,
    Knobs,
    load_bootstrap,
    mask_secret,
)
from .states import DesiredState, ExitReason, Status
from .worker import worker_id

log = get_logger(__name__)

SSE_POLL_SECONDS: Final = 0.25
SSE_HEARTBEAT_SECONDS: Final = 15.0
MAX_LOG_BACKFILL: Final = 2000
"""Matches the client's DOM ring buffer; sending more just to be discarded is waste."""

STOP_ESCALATE_SIGTERM: Final = 60.0
STOP_ESCALATE_SIGKILL: Final = 90.0


# --------------------------------------------------------------------------- #
# Request/response models
# --------------------------------------------------------------------------- #


class AddChannels(BaseModel):
    refs: list[str] = Field(min_length=1, max_length=500)


class PatchChannel(BaseModel):
    is_enabled: bool


class StartRun(BaseModel):
    command: Literal["run", "discover", "hydrate", "fetch", "retry"] = "run"
    channel_id: str | None = None
    limit: int | None = Field(default=None, ge=1, le=1_000_000)
    incremental: bool = False
    dry_run: bool = False
    force_recheck: bool = False
    retry_failed: bool = False
    # Default: no metadata pass. A `run` that hydrates 15k videos first spends
    # hours before its first transcript, which is not what Start looks like it
    # does. Opt back in with --hydrate / the UI checkbox.
    skip_hydrate: bool = True


class SettingsPatch(BaseModel):
    values: dict[str, Any]


# --------------------------------------------------------------------------- #
# Application state
# --------------------------------------------------------------------------- #


class AppState:
    """Everything the request handlers need, built once at startup."""

    def __init__(self, bootstrap: Bootstrap) -> None:
        self.bootstrap = bootstrap
        self.engine = ytdb.make_engine(bootstrap.mysql)
        self.repo = Repo(self.engine)
        self.processes: dict[int, subprocess.Popen[bytes]] = {}
        self._stop_requested: dict[int, float] = {}

    def knobs(self) -> Knobs:
        """Runtime knobs from the DB, falling back to YAML seeds then defaults."""
        stored = self.repo.get_settings()
        merged: dict[str, Any] = dict(self.bootstrap.seeds)
        merged.update(stored)
        return Knobs.from_mapping(merged)

    def close(self) -> None:
        self.engine.dispose()


_state: AppState | None = None


def get_state() -> AppState:
    if _state is None:  # pragma: no cover - set during startup
        raise HTTPException(503, "application state is not initialised")
    return _state


def _recover_orphans() -> None:
    """Close out runs whose process is gone, and release what they were holding.

    Without this the UI shows a phantom RUNNING forever after a reboot or an OOM
    kill, and refuses to start anything new because it thinks a run is live.
    """
    state = get_state()
    with state.engine.connect() as conn:
        missing = ytdb.missing_tables(conn)
    if missing:
        log.error("schema is incomplete; run `yt-tx init`", missing_tables=missing)
        return

    active = state.repo.active_runs()
    alive = {r.pid for r in active if r.pid and process_is_alive(r.pid)}
    crashed = state.repo.mark_orphan_runs_crashed(alive=alive)
    if crashed:
        released = state.repo.reap_expired_leases()
        log.warning("recovered orphaned runs", run_ids=crashed, leases_reset=released)

    # A crashed worker may have left the control table saying 'stopping', which
    # would make the next run exit the moment it started.
    control = state.repo.get_control()
    if control.should_stop and not alive:
        state.repo.set_control(desired_state=DesiredState.RUNNING)
        log.info("reset runtime_control to running after orphan recovery")


def process_is_alive(pid: int) -> bool:
    """Whether a PID is running, without needing to be its parent."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but belongs to someone else; from our perspective it is alive.
        return True
    return True


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #


def _iso(value: datetime | date | None) -> str | None:
    return None if value is None else value.isoformat()


def _channel_json(stats: Any) -> dict[str, Any]:
    return {
        "channel_id": stats.channel_id,
        "handle": stats.handle,
        "title": stats.title,
        "input_ref": stats.input_ref,
        "is_enabled": stats.is_enabled,
        "enumeration_complete": stats.enumeration_complete,
        "reported_video_count": stats.reported_video_count,
        "last_enumerated_at": _iso(stats.last_enumerated_at),
        "counts": stats.counts,
        "total": stats.total,
        "done": stats.done,
        "no_transcript": stats.no_transcript,
        "failed": stats.failed,
        "coverage_pct": stats.coverage_pct,
    }


def _video_json(row: Any, *, full: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "video_id": row.video_id,
        "channel_id": row.channel_id,
        "title": row.title,
        "published_at": _iso(row.published_at),
        "duration_seconds": row.duration_seconds,
        "status": row.status.value,
        "status_reason": row.status_reason,
        "attempts": row.attempts,
        "recheck_count": row.recheck_count,
        "needs_audio": row.needs_audio,
        "is_short": row.is_short,
        "was_livestream": row.was_livestream,
        "view_count": row.view_count,
        "thumbnail_url": row.thumbnail_url,
        "available_transcripts": row.available_transcripts,
    }
    if full:
        payload.update(
            {
                "description": row.description,
                "tags": row.tags,
                "like_count": row.like_count,
                "comment_count": row.comment_count,
                "category_id": row.category_id,
                "default_language": row.default_language,
                "default_audio_language": row.default_audio_language,
                "live_broadcast_content": row.live_broadcast_content,
                "metadata_fetched_at": _iso(row.metadata_fetched_at),
                "next_attempt_at": _iso(row.next_attempt_at),
                "recheck_after": _iso(row.recheck_after),
                "claimed_by": row.claimed_by,
                "lease_expires_at": _iso(row.lease_expires_at),
            }
        )
    return payload


def _run_json(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "command": row.command,
        "args": row.args,
        "pid": row.pid,
        "host": row.host,
        "log_path": row.log_path,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "heartbeat_at": _iso(row.heartbeat_at),
        "counts": row.counts,
        "exit_reason": row.exit_reason,
        "is_active": row.is_active,
    }


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #


def create_app(bootstrap: Bootstrap | None = None) -> FastAPI:
    global _state
    boot = bootstrap or load_bootstrap()
    boot.validate()
    boot.ensure_dirs()
    _state = AppState(boot)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Recover orphaned runs on the way up, dispose the pool on the way down.

        Uses the lifespan protocol rather than ``@app.on_event``, which FastAPI
        has deprecated.
        """
        _recover_orphans()
        try:
            yield
        finally:
            if _state is not None:
                _state.close()

    app = FastAPI(
        title="yt-tx", version="1.0.0", docs_url="/api/docs", lifespan=lifespan
    )

    @app.middleware("http")
    async def _auth(request: Request, call_next: Any) -> Response:
        """Require a bearer token whenever we are not on loopback."""
        state = get_state()
        token = state.bootstrap.web.auth_token
        if token and not request.url.path.startswith("/api/docs"):
            provided = request.headers.get("authorization", "")
            expected = f"Bearer {token}"
            query_token = request.query_params.get("token")
            # EventSource cannot set headers, so SSE may authenticate by query.
            if provided != expected and query_token != token:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return cast(Response, await call_next(request))

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:  # noqa: C901 - a flat route table
    # -- UI ---------------------------------------------------------------- #

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        path = Path(__file__).parent / "static" / "index.html"
        if not path.exists():  # pragma: no cover
            raise HTTPException(500, "static/index.html is missing from the package")
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def health(state: AppState = Depends(get_state)) -> dict[str, Any]:
        try:
            with state.engine.connect() as conn:
                diag = ytdb.check_server(conn)
                missing = ytdb.missing_tables(conn)
            return {"ok": not missing, "missing_tables": missing, "mysql": diag}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:500]}

    # -- stats ------------------------------------------------------------- #

    @app.get("/api/stats")
    def stats(state: AppState = Depends(get_state)) -> dict[str, Any]:
        snapshot = state.repo.stats()
        knobs = state.knobs()
        control = state.repo.get_control()
        eta = snapshot.eta_seconds()
        run = snapshot.active_run
        running = bool(run and run.pid and process_is_alive(run.pid))

        # Breaker state comes from the worker's heartbeat: it lives in the worker
        # process and this one has no view of it otherwise.
        breaker: dict[str, Any] | None = None
        if run is not None and running and isinstance(run.counts, dict):
            candidate = cast("dict[str, Any]", run.counts).get("breaker")
            if isinstance(candidate, dict):
                breaker = cast("dict[str, Any]", candidate)

        return {
            "breaker": breaker or {"state": "closed"},
            "by_status": snapshot.by_status,
            "total": snapshot.total,
            "remaining": snapshot.remaining,
            "coverage_pct": snapshot.coverage_pct,
            "transcripts": snapshot.transcripts,
            "needs_audio": snapshot.needs_audio,
            "videos_per_minute": snapshot.videos_per_minute,
            "completed_last_5m": snapshot.completed_last_5m,
            # Hidden by the client while paused or circuit-open, where a trailing
            # rate says nothing useful about how long the rest will take.
            "eta_seconds": {"low": eta[0], "high": eta[1]} if eta else None,
            "state": (
                "running" if running and control.desired_state is DesiredState.RUNNING
                else control.desired_state.value if running
                else "idle"
            ),
            "desired_state": control.desired_state.value,
            "concurrency": control.concurrency,
            "requests_per_second": float(control.requests_per_second),
            "active_run": _run_json(run) if run else None,
            "run_alive": running,
            "quota": {
                "used": snapshot.quota_used,
                "budget": knobs.daily_quota_units,
                "pct": round(
                    100.0 * snapshot.quota_used / max(1, knobs.daily_quota_units), 1
                ),
                "day": _iso(snapshot.quota_day),
                "stop_at_pct": knobs.quota_stop_at_pct,
            },
            "statuses": [s.value for s in Status],
        }

    # -- channels ---------------------------------------------------------- #

    @app.get("/api/channels")
    def list_channels(state: AppState = Depends(get_state)) -> list[dict[str, Any]]:
        return [_channel_json(c) for c in state.repo.channel_stats()]

    @app.post("/api/channels")
    def add_channels(
        body: AddChannels, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        from .discover import ResolveError, resolve_channel
        from .youtube_api import YouTubeAPI, YouTubeAPIError

        knobs = state.knobs()
        api: YouTubeAPI | None = None
        if knobs.youtube_api_key:
            try:
                api = YouTubeAPI(knobs.youtube_api_key, proxy=knobs.proxy)
            except YouTubeAPIError:
                api = None

        added: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        try:
            for ref in body.refs:
                cleaned = ref.strip()
                if not cleaned:
                    continue
                try:
                    row = resolve_channel(state.repo, cleaned, api=api)
                except (ResolveError, YouTubeAPIError, Exception) as exc:  # noqa: BLE001
                    errors.append({"ref": cleaned, "error": str(exc)[:400]})
                    continue
                added.append(
                    {
                        "channel_id": row.channel_id,
                        "handle": row.handle,
                        "title": row.title,
                        "uploads_playlist_id": row.uploads_playlist_id,
                        "reported_video_count": row.reported_video_count,
                    }
                )
        finally:
            if api is not None:
                api.close()
        return {"added": added, "errors": errors}

    @app.delete("/api/channels/{channel_id}")
    def delete_channel(
        channel_id: str, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        if not state.repo.delete_channel(channel_id):
            raise HTTPException(404, "no such channel")
        return {"deleted": channel_id}

    @app.patch("/api/channels/{channel_id}")
    def patch_channel(
        channel_id: str, body: PatchChannel, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        if not state.repo.set_channel_enabled(channel_id, body.is_enabled):
            raise HTTPException(404, "no such channel")
        return {"channel_id": channel_id, "is_enabled": body.is_enabled}

    # -- settings ---------------------------------------------------------- #

    @app.get("/api/settings")
    def get_settings(state: AppState = Depends(get_state)) -> dict[str, Any]:
        knobs = state.knobs()
        return {
            "values": knobs.redacted(),
            "knobs": [
                {
                    "key": spec.key,
                    "tier": spec.tier,
                    "group": spec.group,
                    "kind": spec.kind,
                    "label": spec.label,
                    "help": spec.help,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "choices": list(spec.choices) if spec.choices else None,
                    "nullable": spec.nullable,
                }
                for spec in KNOB_SPECS
            ],
            "secret_keys": sorted(SECRET_KNOBS),
            "live_keys": sorted(LIVE_KNOBS),
            "bootstrap": {
                "transcript_dir": str(state.bootstrap.transcript_dir),
                "log_dir": str(state.bootstrap.log_dir),
                "mysql": state.bootstrap.mysql.dsn(hide_password=True),
                "config_path": str(state.bootstrap.source_path or ""),
            },
        }

    @app.put("/api/settings")
    def put_settings(
        body: SettingsPatch, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        from .settings import KNOB_SPECS_BY_KEY

        clean: dict[str, Any] = {}
        for key, raw in body.values.items():
            spec = KNOB_SPECS_BY_KEY.get(key)
            if spec is None:
                raise HTTPException(400, f"unknown setting {key!r}")
            # A masked secret echoed back from the form means "leave it alone".
            if key in SECRET_KNOBS and isinstance(raw, str) and set(raw[:-4]) == {"*"}:
                continue
            try:
                clean[key] = spec.coerce(raw)
            except ConfigError as exc:
                raise HTTPException(400, str(exc)) from exc

        state.repo.put_settings(clean)

        # Mirror live knobs into runtime_control so a running worker picks them
        # up within a couple of seconds instead of at the next restart.
        live = {k: v for k, v in clean.items() if k in LIVE_KNOBS}
        if live:
            state.repo.set_control(
                concurrency=(
                    int(live["concurrency"]) if "concurrency" in live else None
                ),
                requests_per_second=(
                    float(live["requests_per_second"])
                    if "requests_per_second" in live
                    else None
                ),
            )
        return {
            "updated": sorted(clean),
            "applied_live": sorted(live),
            "values": state.knobs().redacted(),
        }

    # -- runs -------------------------------------------------------------- #

    @app.get("/api/runs")
    def list_runs(
        limit: int = Query(default=50, ge=1, le=500),
        state: AppState = Depends(get_state),
    ) -> list[dict[str, Any]]:
        return [_run_json(r) for r in state.repo.list_runs(limit=limit)]

    @app.post("/api/runs")
    def start_run(
        body: StartRun, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        existing = state.repo.active_run()
        if existing and existing.pid and process_is_alive(existing.pid):
            raise HTTPException(
                409,
                f"run {existing.id} is already active (pid {existing.pid}); "
                "stop it first",
            )
        if existing:
            state.repo.finish_run(existing.id, exit_reason=ExitReason.CRASHED)
            state.repo.reap_expired_leases()

        # A previous stop must not silently kill the run we are about to start.
        state.repo.set_control(desired_state=DesiredState.RUNNING)

        run_id = state.repo.create_run(body.command, args=body.model_dump())
        log_path = run_log_path(state.bootstrap.log_dir, run_id)

        argv = _build_argv(body, run_id)
        log.info("spawning worker", run_id=run_id, argv=argv)
        try:
            process = subprocess.Popen(  # noqa: S603 - list argv, shell=False
                argv,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(Path.cwd()),
                env={**os.environ, "YT_TX_RUN_ID": str(run_id)},
            )
        except OSError as exc:
            state.repo.finish_run(run_id, exit_reason=ExitReason.CRASHED)
            raise HTTPException(500, f"could not spawn worker: {exc}") from exc

        state.processes[run_id] = process
        state.repo.set_run_pid(run_id, process.pid, str(log_path))
        return {
            "run_id": run_id,
            "pid": process.pid,
            "log_path": str(log_path),
            "command": shlex.join(argv),
        }

    @app.post("/api/runs/{run_id}/pause")
    def pause_run(run_id: int, state: AppState = Depends(get_state)) -> dict[str, Any]:
        _require_run(state, run_id)
        state.repo.set_control(desired_state=DesiredState.PAUSED)
        return {"run_id": run_id, "desired_state": "paused"}

    @app.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: int, state: AppState = Depends(get_state)) -> dict[str, Any]:
        _require_run(state, run_id)
        state.repo.set_control(desired_state=DesiredState.RUNNING)
        return {"run_id": run_id, "desired_state": "running"}

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: int, state: AppState = Depends(get_state)) -> dict[str, Any]:
        run = _require_run(state, run_id)
        state.repo.set_control(desired_state=DesiredState.STOPPING)
        state._stop_requested.setdefault(run_id, time.monotonic())
        return {
            "run_id": run_id,
            "desired_state": "stopping",
            "note": (
                "the worker is draining in-flight work; it will be sent SIGTERM "
                f"after {int(STOP_ESCALATE_SIGTERM)}s and SIGKILL after "
                f"{int(STOP_ESCALATE_SIGKILL)}s"
            ),
            "pid": run.pid,
        }

    @app.post("/api/runs/{run_id}/escalate")
    def escalate_stop(
        run_id: int, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        """Send the next signal in the escalation ladder, if it is due.

        Polled by the UI while a stop is pending. Escalation lives here rather
        than on a background timer so that a restarted API cannot lose track of
        it - the elapsed time is derived from the request, not from in-memory
        state alone.
        """
        run = _require_run(state, run_id)
        if run.pid is None:
            raise HTTPException(400, "run has no recorded pid")
        if not process_is_alive(run.pid):
            state.repo.finish_run(run_id, exit_reason=ExitReason.STOPPED)
            return {"run_id": run_id, "action": "already exited"}

        since = state._stop_requested.get(run_id)
        if since is None:
            raise HTTPException(400, "no stop has been requested for this run")
        elapsed = time.monotonic() - since

        action = "waiting"
        if elapsed >= STOP_ESCALATE_SIGKILL:
            os.kill(run.pid, signal.SIGKILL)
            state.repo.finish_run(run_id, exit_reason=ExitReason.STOPPED)
            state.repo.reap_expired_leases()
            action = "sigkill"
        elif elapsed >= STOP_ESCALATE_SIGTERM:
            os.kill(run.pid, signal.SIGTERM)
            action = "sigterm"
        return {"run_id": run_id, "action": action, "elapsed": round(elapsed, 1)}

    # -- logs -------------------------------------------------------------- #

    @app.get("/api/runs/{run_id}/logs")
    def backfill_logs(
        run_id: int,
        since: int = Query(default=0, ge=0),
        limit: int = Query(default=MAX_LOG_BACKFILL, ge=1, le=MAX_LOG_BACKFILL),
        state: AppState = Depends(get_state),
    ) -> dict[str, Any]:
        """Replay log lines after a reconnect, keyed on line sequence number."""
        path = _log_path_for(state, run_id)
        lines: list[dict[str, Any]] = []
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for index, raw in enumerate(handle, start=1):
                    if index <= since:
                        continue
                    payload = _parse_log_line(raw, index)
                    if payload is not None:
                        lines.append(payload)
                    if len(lines) >= limit:
                        break
        return {"run_id": run_id, "since": since, "lines": lines}

    @app.get("/api/runs/{run_id}/logs/stream")
    async def stream_logs(
        run_id: int,
        request: Request,
        max_seconds: float = Query(default=900.0, ge=0.5, le=86400.0),
        state: AppState = Depends(get_state),
    ) -> StreamingResponse:
        """Tail a run's JSONL log as Server-Sent Events.

        The stream closes itself after ``max_seconds``, and as soon as a finished
        run's log has been fully read. Both bounds matter: an unbounded generator
        per connection leaks a server-side task for every browser tab that ever
        watched a run, and ``EventSource`` reconnects on its own and replays the
        gap through ``Last-Event-ID``, so closing costs the client nothing.
        """
        path = _log_path_for(state, run_id)
        last_event_id = request.headers.get("last-event-id")
        try:
            start_at = int(last_event_id) if last_event_id else 0
        except ValueError:
            start_at = 0

        async def events() -> AsyncIterator[bytes]:
            sequence = start_at
            position = 0
            started = time.monotonic()
            last_beat = started

            # readline() throughout, never `for line in handle`: Python disables
            # tell() on a text file while it is being iterated, and this loop is
            # built entirely on remembering byte offsets between polls.
            if sequence and path.exists():
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    seen = 0
                    while seen < sequence:
                        if not handle.readline():
                            break
                        seen += 1
                    position = handle.tell()
                    # If the log is shorter than the client's Last-Event-ID (it
                    # was rotated or pruned), resync rather than emitting ids
                    # that appear to go backwards.
                    sequence = seen

            while True:
                if await request.is_disconnected():
                    return
                emitted = False
                if path.exists():
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        handle.seek(position)
                        while True:
                            raw = handle.readline()
                            if not raw:
                                break
                            if not raw.endswith("\n"):
                                # A partially written line. Leave `position`
                                # where it was and wait for the rest, rather
                                # than emitting truncated JSON.
                                break
                            position = handle.tell()
                            sequence += 1
                            payload = _parse_log_line(raw, sequence)
                            if payload is None:
                                continue
                            emitted = True
                            body = json.dumps(payload, ensure_ascii=False)
                            yield f"id: {sequence}\ndata: {body}\n\n".encode()
                if emitted:
                    last_beat = time.monotonic()
                    continue

                if time.monotonic() - started >= max_seconds:
                    return
                # Caught up on a run that is over: nothing more will ever arrive.
                run = state.repo.get_run(run_id)
                if run is not None and not run.is_active:
                    return
                if time.monotonic() - last_beat >= SSE_HEARTBEAT_SECONDS:
                    last_beat = time.monotonic()
                    # Comment frame: keeps proxies and browsers from timing out
                    # an idle stream.
                    yield b": keep-alive\n\n"
                await asyncio.sleep(SSE_POLL_SECONDS)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                # Without this nginx buffers the whole stream and the console
                # stays empty until the run ends.
                "X-Accel-Buffering": "no",
            },
        )

    # -- videos ------------------------------------------------------------ #

    @app.get("/api/videos")
    def list_videos(
        channel_id: str | None = None,
        status: str | None = None,
        q: str | None = None,
        sort: str = "published_desc",
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=50, ge=1, le=500),
        state: AppState = Depends(get_state),
    ) -> dict[str, Any]:
        if status and status not in {s.value for s in Status}:
            raise HTTPException(400, f"unknown status {status!r}")
        rows, total = state.repo.list_videos(
            channel_id=channel_id, status=status, query=q,
            sort=sort, page=page, per_page=per_page,
        )
        return {
            "videos": [_video_json(r) for r in rows],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        }

    @app.get("/api/videos/{video_id}")
    def get_video(
        video_id: str, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        row = state.repo.get_video(video_id)
        if row is None:
            raise HTTPException(404, "no such video")
        return {
            "video": _video_json(row, full=True),
            "transcripts": [
                {
                    "id": t.id,
                    "language_code": t.language_code,
                    "kind": t.kind.value,
                    "is_preferred": t.is_preferred,
                    "segment_count": t.segment_count,
                    "char_count": t.char_count,
                    "word_count": t.word_count,
                    "covered_seconds": t.covered_seconds,
                    "source": t.source,
                    "fetched_at": _iso(t.fetched_at),
                }
                for t in state.repo.list_transcripts(video_id)
            ],
            "attempts": [
                {
                    **{k: v for k, v in attempt.items() if k not in
                       {"started_at", "finished_at"}},
                    "started_at": _iso(cast("datetime | None", attempt["started_at"])),
                    "finished_at": _iso(cast("datetime | None", attempt["finished_at"])),
                }
                for attempt in state.repo.recent_attempts(video_id)
            ],
        }

    @app.get("/api/videos/{video_id}/transcript")
    def get_transcript(
        video_id: str,
        lang: str | None = None,
        kind: str | None = None,
        state: AppState = Depends(get_state),
    ) -> dict[str, Any]:
        row = state.repo.get_transcript(video_id, language=lang, kind=kind)
        if row is None:
            raise HTTPException(404, "no transcript stored for this video")

        segments: list[dict[str, Any]] = []
        path = Path(row.raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.exists():
            from .fetch import read_transcript_file

            try:
                segments = cast(
                    "list[dict[str, Any]]", read_transcript_file(path).get("segments", [])
                )
            except (OSError, json.JSONDecodeError, EOFError) as exc:
                log.error("unreadable transcript file", path=str(path),
                          error=str(exc)[:300], video_id=video_id)
        return {
            "video_id": video_id,
            "language_code": row.language_code,
            "kind": row.kind.value,
            "source": row.source,
            "segment_count": row.segment_count,
            "word_count": row.word_count,
            "covered_seconds": row.covered_seconds,
            "fetched_at": _iso(row.fetched_at),
            "raw_path": row.raw_path,
            "file_present": path.exists(),
            "plaintext": row.plaintext,
            "segments": segments,
        }

    @app.post("/api/videos/{video_id}/refetch")
    def refetch_video(
        video_id: str, state: AppState = Depends(get_state)
    ) -> dict[str, Any]:
        if not state.repo.reopen_video(video_id):
            raise HTTPException(404, "no such video")
        return {
            "video_id": video_id,
            "status": Status.METADATA_OK.value,
            "note": "queued; it will be picked up by the next fetch run",
        }

    # -- search and export ------------------------------------------------- #

    @app.get("/api/search")
    def search(
        q: str = Query(min_length=1),
        limit: int = Query(default=50, ge=1, le=500),
        state: AppState = Depends(get_state),
    ) -> dict[str, Any]:
        with state.engine.connect() as conn:
            has_index = ytdb.fulltext_index_exists(conn)
            min_token = ytdb.ft_min_token_size(conn)
        hits = state.repo.search_transcripts(q, limit=limit, use_fulltext=has_index)
        notes: list[str] = []
        if not has_index:
            notes.append(
                "the FULLTEXT index is not present; falling back to LIKE, which "
                "is correct but slow. Rebuild it with `yt-tx fulltext --build`."
            )
        if len(q) < min_token and has_index:
            notes.append(
                f"innodb_ft_min_token_size is {min_token}, so words shorter than "
                f"that are not indexed and this query cannot match."
            )
        return {"query": q, "hits": hits, "count": len(hits), "notes": notes}

    @app.get("/api/export")
    def export(
        format: Literal["jsonl", "txt", "csv"] = "jsonl",
        channel_id: str | None = None,
        state: AppState = Depends(get_state),
    ) -> Response:
        from .exports import export_stream

        media = {
            "jsonl": "application/x-ndjson",
            "txt": "text/plain; charset=utf-8",
            "csv": "text/csv; charset=utf-8",
        }[format]
        suffix = channel_id or "all"
        return StreamingResponse(
            export_stream(state.repo, fmt=format, channel_id=channel_id),
            media_type=media,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="yt-tx-{suffix}.{format}"'
                )
            },
        )

    @app.get("/api/audio-queue")
    def audio_queue(
        limit: int = Query(default=500, ge=1, le=100_000),
        state: AppState = Depends(get_state),
    ) -> dict[str, Any]:
        rows = state.repo.audio_queue(limit=limit)
        return {
            "count": len(rows),
            "total_seconds": sum(r.duration_seconds or 0 for r in rows),
            "videos": [
                {
                    "video_id": r.video_id,
                    "channel_id": r.channel_id,
                    "title": r.title,
                    "duration_seconds": r.duration_seconds,
                    "status": r.status.value,
                    "available_transcripts": r.available_transcripts,
                }
                for r in rows
            ],
        }

    @app.get("/api/doctor", response_class=PlainTextResponse)
    def doctor_report(state: AppState = Depends(get_state)) -> str:
        from .maintenance import doctor

        return doctor(state.repo, state.bootstrap).as_text()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _require_run(state: AppState, run_id: int) -> Any:
    run = state.repo.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return run


def _log_path_for(state: AppState, run_id: int) -> Path:
    run = state.repo.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    if run.log_path:
        path = Path(run.log_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path
    return run_log_path(state.bootstrap.log_dir, run_id)


def _parse_log_line(raw: str, sequence: int) -> dict[str, Any] | None:
    """Turn one JSONL line into a client event, tolerating junk.

    A malformed line must never break the stream - a half-flushed record or a
    stray print from a dependency should degrade to a visible raw line, not kill
    the console.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"seq": sequence, "level": "info", "event": text[:2000], "raw": True}
    if not isinstance(payload, dict):
        return {"seq": sequence, "level": "info", "event": text[:2000], "raw": True}
    data = cast("dict[str, Any]", payload)
    data["seq"] = sequence
    data.setdefault("level", "info")
    return data


def _build_argv(body: StartRun, run_id: int) -> list[str]:
    """Build the worker command as a list. Never a shell string.

    Channel ids are user-supplied and land next to a command line; a shell string
    here would be a straightforward injection. ``shell=False`` plus a list argv
    makes the whole class of problem impossible.
    """
    argv = [sys.executable, "-m", "yt_tx", body.command, "--run-id", str(run_id)]
    if body.channel_id:
        argv += ["--channel", body.channel_id]
    if body.limit:
        argv += ["--limit", str(body.limit)]
    if body.incremental:
        argv.append("--incremental")
    if not body.skip_hydrate and body.command in {"run", "fetch"}:
        argv.append("--hydrate")
    if body.command == "fetch" and body.dry_run:
        argv.append("--dry-run")
    if body.command == "retry":
        if body.retry_failed:
            argv.append("--failed")
        if body.force_recheck:
            argv.append("--force-recheck")
    return argv


def serve(
    bootstrap: Bootstrap | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
) -> None:
    """Run uvicorn. Refuses to expose a routable interface without a token."""
    import uvicorn

    boot = bootstrap or load_bootstrap()
    from dataclasses import replace

    if host is not None or port is not None:
        boot = replace(
            boot,
            web=replace(
                boot.web,
                host=host if host is not None else boot.web.host,
                port=port if port is not None else boot.web.port,
            ),
        )
    boot.validate()

    app = create_app(boot)
    log.info(
        "serving", host=boot.web.host, port=boot.web.port,
        auth=bool(boot.web.auth_token),
    )
    uvicorn.run(app, host=boot.web.host, port=boot.web.port, log_level="warning")


__all__ = ["create_app", "serve", "process_is_alive"]
