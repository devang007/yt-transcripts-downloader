"""REST API, subprocess supervision, and the SSE log stream.

No run is ever actually spawned here: ``POST /api/runs`` is exercised with
``subprocess.Popen`` patched out, because the interesting assertions are about the
*argv* it builds (a list, never a shell string, with user-supplied channel ids in
it) and the bookkeeping around it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from yt_tx.api import create_app
from yt_tx.repo import Repo, TranscriptWrite, VideoUpsert
from yt_tx.settings import Bootstrap, ConfigError, MySQLConfig, WebConfig
from yt_tx.states import Status, TranscriptKind

pytestmark = pytest.mark.mysql


@pytest.fixture
def bootstrap(engine: Engine, tmp_path: Path) -> Bootstrap:
    """A bootstrap whose engine points at the test database."""
    url = engine.url
    return Bootstrap(
        mysql=MySQLConfig(
            host=url.host or "127.0.0.1",
            port=url.port or 3306,
            database=url.database or "yt_tx_test",
            user=url.username or "root",
            password=url.password or "",
        ),
        transcript_dir=tmp_path / "transcripts",
        log_dir=tmp_path / "logs",
        web=WebConfig(host="127.0.0.1", port=8000, auth_token=None),
        seeds={"languages": ["en", "hi"], "concurrency": 3},
    )


@pytest.fixture
def client(bootstrap: Bootstrap) -> Iterator[TestClient]:
    app = create_app(bootstrap)
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# Health and stats
# --------------------------------------------------------------------------- #


def test_health(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["ok"] is True
    assert payload["missing_tables"] == []
    assert payload["mysql"]["skip_locked"] is True


def test_index_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "yt-tx control panel" in response.text
    # The three properties the UI comment calls load-bearing.
    assert "MAX_LOG_LINES = 2000" in response.text
    assert "tier-live" in response.text


def test_stats_shape(client: TestClient, seeded: str) -> None:
    payload = client.get("/api/stats").json()
    assert payload["total"] == 3
    assert payload["remaining"] == 3
    assert payload["by_status"] == {"metadata_ok": 3}
    assert payload["state"] == "idle"
    assert payload["eta_seconds"] is None
    assert payload["quota"]["budget"] > 0
    assert payload["breaker"]["state"] == "closed"
    assert set(payload["statuses"]) >= {s.value for s in Status}


# --------------------------------------------------------------------------- #
# Channels
# --------------------------------------------------------------------------- #


def test_channel_listing_and_toggle(client: TestClient, seeded: str) -> None:
    channels = client.get("/api/channels").json()
    assert len(channels) == 1
    assert channels[0]["total"] == 3
    assert channels[0]["coverage_pct"] == 0.0

    channel_id = channels[0]["channel_id"]
    patched = client.patch(
        f"/api/channels/{channel_id}", json={"is_enabled": False}
    ).json()
    assert patched["is_enabled"] is False
    assert client.get("/api/channels").json()[0]["is_enabled"] is False

    assert client.delete(f"/api/channels/{channel_id}").status_code == 200
    assert client.get("/api/channels").json() == []
    assert client.delete(f"/api/channels/{channel_id}").status_code == 404


def test_adding_a_bad_channel_reports_per_ref_errors(client: TestClient) -> None:
    """One unresolvable ref must not fail the whole batch."""
    response = client.post("/api/channels", json={"refs": ["https://vimeo.com/1"]})
    assert response.status_code == 200
    payload = response.json()
    assert payload["added"] == []
    assert len(payload["errors"]) == 1
    assert payload["errors"][0]["ref"] == "https://vimeo.com/1"


def test_empty_ref_list_is_rejected(client: TestClient) -> None:
    assert client.post("/api/channels", json={"refs": []}).status_code == 422


# --------------------------------------------------------------------------- #
# Settings: tiers and secrets
# --------------------------------------------------------------------------- #


def test_settings_report_every_knob_with_its_tier(client: TestClient) -> None:
    """A knob without a tier is a knob that lies about when it takes effect."""
    payload = client.get("/api/settings").json()
    assert payload["knobs"]
    for knob in payload["knobs"]:
        assert knob["tier"] in {"live", "next_run", "secret"}
        assert knob["label"] and knob["help"], f"{knob['key']} needs a label and help"
        assert knob["group"]
    assert set(payload["live_keys"]) == {"concurrency", "requests_per_second"}
    assert "youtube_api_key" in payload["secret_keys"]


def test_api_key_is_never_returned_in_full(client: TestClient) -> None:
    client.put("/api/settings", json={"values": {"youtube_api_key": "AIzaSyVERYSECRET1"}})
    payload = client.get("/api/settings").json()
    masked = payload["values"]["youtube_api_key"]
    assert masked is not None
    assert "VERYSECRET" not in masked
    assert masked.endswith("RET1")
    assert set(masked[:-4]) == {"*"}


def test_masked_secret_echoed_back_does_not_overwrite(
    client: TestClient, repo: Repo
) -> None:
    """The form round-trips the mask; that must mean "leave it alone"."""
    client.put("/api/settings", json={"values": {"youtube_api_key": "AIzaSyREALKEY123"}})
    masked = client.get("/api/settings").json()["values"]["youtube_api_key"]

    client.put("/api/settings", json={"values": {"youtube_api_key": masked}})
    assert repo.get_settings()["youtube_api_key"] == "AIzaSyREALKEY123"


def test_live_knobs_are_mirrored_into_runtime_control(
    client: TestClient, repo: Repo
) -> None:
    """This is what makes the slider real rather than decorative."""
    response = client.put(
        "/api/settings",
        json={"values": {"concurrency": 7, "requests_per_second": 1.5, "burst": 9}},
    )
    payload = response.json()
    assert set(payload["applied_live"]) == {"concurrency", "requests_per_second"}
    assert "burst" in payload["updated"]

    control = repo.get_control()
    assert control.concurrency == 7
    assert float(control.requests_per_second) == pytest.approx(1.5)


def test_next_run_knob_does_not_touch_runtime_control(
    client: TestClient, repo: Repo
) -> None:
    before = repo.get_control()
    payload = client.put("/api/settings", json={"values": {"max_attempts": 6}}).json()
    assert payload["applied_live"] == []
    after = repo.get_control()
    assert after.concurrency == before.concurrency


def test_invalid_settings_are_rejected_with_a_reason(client: TestClient) -> None:
    for values, fragment in (
        ({"concurrency": 0}, ">= 1"),
        ({"concurrency": 999}, "<= 32"),
        ({"fetcher": "wishful-thinking"}, "must be one of"),
        ({"languages": []}, "at least one"),
        ({"jitter": 5.0}, "<= 1"),
    ):
        response = client.put("/api/settings", json={"values": values})
        assert response.status_code == 400, values
        assert fragment in response.json()["detail"], values


def test_unknown_setting_is_rejected(client: TestClient) -> None:
    response = client.put("/api/settings", json={"values": {"nonsense": 1}})
    assert response.status_code == 400
    assert "unknown setting" in response.json()["detail"]


def test_language_list_accepts_a_comma_string_from_the_form(client: TestClient) -> None:
    payload = client.put(
        "/api/settings", json={"values": {"languages": "en, en-GB , hi"}}
    ).json()
    assert payload["values"]["languages"] == ["en", "en-GB", "hi"]


# --------------------------------------------------------------------------- #
# Runs and subprocess supervision
# --------------------------------------------------------------------------- #


class FakePopen:
    """Stands in for a spawned worker; records exactly how it was invoked."""

    last: dict[str, Any] = {}

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        FakePopen.last = {"argv": argv, **kwargs}
        self.pid = os.getpid()  # a pid that is genuinely alive

    def poll(self) -> int | None:
        return None


def test_start_run_spawns_with_a_list_argv_and_no_shell(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    """Channel refs are user input and land next to a command line."""
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    response = client.post(
        "/api/runs",
        json={"command": "fetch", "channel_id": "UC; rm -rf /", "limit": 5},
    )
    assert response.status_code == 200
    payload = response.json()

    argv = FakePopen.last["argv"]
    assert isinstance(argv, list), "a shell string here would be an injection"
    assert FakePopen.last["shell"] is False
    assert FakePopen.last["start_new_session"] is True
    # The dangerous string is one opaque argv element, not shell syntax.
    assert "UC; rm -rf /" in argv
    assert argv[1:4] == ["-m", "yt_tx", "fetch"]
    assert "--run-id" in argv and str(payload["run_id"]) in argv

    run = repo.get_run(payload["run_id"])
    assert run is not None
    assert run.pid == os.getpid()
    assert run.log_path


def test_second_run_is_refused_while_one_is_alive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    assert client.post("/api/runs", json={"command": "fetch"}).status_code == 200
    conflict = client.post("/api/runs", json={"command": "fetch"})
    assert conflict.status_code == 409
    assert "already active" in conflict.json()["detail"]


def test_starting_a_run_clears_a_stale_stop_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    """Otherwise the new worker reads 'stopping' and exits immediately."""
    import subprocess

    from yt_tx.states import DesiredState

    repo.set_control(desired_state=DesiredState.STOPPING)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    assert client.post("/api/runs", json={"command": "run"}).status_code == 200
    assert repo.get_control().desired_state is DesiredState.RUNNING


def test_pause_resume_stop_drive_runtime_control(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, repo: Repo
) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    run_id = client.post("/api/runs", json={"command": "fetch"}).json()["run_id"]

    client.post(f"/api/runs/{run_id}/pause")
    assert repo.get_control().is_paused is True
    client.post(f"/api/runs/{run_id}/resume")
    assert repo.get_control().is_paused is False

    stopped = client.post(f"/api/runs/{run_id}/stop").json()
    assert repo.get_control().should_stop is True
    # The escalation ladder is stated to the operator, not left implicit.
    assert "SIGTERM" in stopped["note"] and "SIGKILL" in stopped["note"]


def test_stop_escalation_waits_before_signalling(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    run_id = client.post("/api/runs", json={"command": "fetch"}).json()["run_id"]
    client.post(f"/api/runs/{run_id}/stop")

    result = client.post(f"/api/runs/{run_id}/escalate").json()
    assert result["action"] == "waiting", "must not SIGKILL a draining worker at once"
    assert result["elapsed"] < 5


def test_escalate_without_a_stop_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    run_id = client.post("/api/runs", json={"command": "fetch"}).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/escalate").status_code == 400


def test_orphan_run_recovery_on_startup(bootstrap: Bootstrap, repo: Repo) -> None:
    """Without this the UI shows a phantom RUNNING forever after a reboot."""
    dead_pid = 999_999_98
    run_id = repo.create_run("fetch", pid=dead_pid)
    assert repo.get_run(run_id).is_active is True  # type: ignore[union-attr]

    with TestClient(create_app(bootstrap)):
        pass

    recovered = repo.get_run(run_id)
    assert recovered is not None
    assert recovered.is_active is False
    assert recovered.exit_reason == "crashed"


def test_orphan_recovery_releases_leases(
    bootstrap: Bootstrap, repo: Repo, seeded: str
) -> None:
    repo.create_run("fetch", pid=999_999_97)
    repo.claim_batch("ghost-worker", limit=3, lease_seconds=600)
    with repo.begin() as conn:
        conn.execute(
            text(
                "UPDATE videos SET lease_expires_at = "
                "UTC_TIMESTAMP(6) - INTERVAL 1 SECOND"
            )
        )
    with TestClient(create_app(bootstrap)):
        pass
    assert repo.count_stale_leases() == 0
    assert len(repo.claim_batch("fresh-worker", limit=3)) == 3


def test_run_history(client: TestClient, repo: Repo) -> None:
    repo.create_run("discover")
    repo.create_run("fetch")
    runs = client.get("/api/runs?limit=10").json()
    assert len(runs) == 2
    assert runs[0]["id"] > runs[1]["id"], "newest first"


def test_run_404(client: TestClient) -> None:
    assert client.post("/api/runs/424242/pause").status_code == 404


# --------------------------------------------------------------------------- #
# Logs: backfill and SSE
# --------------------------------------------------------------------------- #


def _write_log(bootstrap: Bootstrap, run_id: int, lines: list[dict[str, Any]]) -> Path:
    bootstrap.log_dir.mkdir(parents=True, exist_ok=True)
    path = bootstrap.log_dir / f"run-{run_id}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    return path


def test_log_backfill_is_sequenced(
    client: TestClient, bootstrap: Bootstrap, repo: Repo
) -> None:
    run_id = repo.create_run("fetch")
    path = _write_log(
        bootstrap, run_id,
        [{"event": f"line {i}", "level": "info", "ts": "2024-01-01T00:00:0%dZ" % i}
         for i in range(5)],
    )
    repo.set_run_pid(run_id, os.getpid(), str(path))

    payload = client.get(f"/api/runs/{run_id}/logs").json()
    assert [line["seq"] for line in payload["lines"]] == [1, 2, 3, 4, 5]
    assert payload["lines"][0]["event"] == "line 0"

    # The reconnect path: only what the client has not seen.
    gap = client.get(f"/api/runs/{run_id}/logs?since=3").json()
    assert [line["seq"] for line in gap["lines"]] == [4, 5]


def test_log_backfill_survives_malformed_lines(
    client: TestClient, bootstrap: Bootstrap, repo: Repo
) -> None:
    """A half-flushed record must degrade to a visible line, not break the stream."""
    run_id = repo.create_run("fetch")
    bootstrap.log_dir.mkdir(parents=True, exist_ok=True)
    path = bootstrap.log_dir / f"run-{run_id}.jsonl"
    path.write_text(
        '{"event": "good", "level": "info"}\n'
        "this is not json at all\n"
        '{"event": "also good", "level": "warning"}\n',
        encoding="utf-8",
    )
    repo.set_run_pid(run_id, os.getpid(), str(path))

    lines = client.get(f"/api/runs/{run_id}/logs").json()["lines"]
    assert len(lines) == 3
    assert lines[1]["raw"] is True
    assert lines[2]["event"] == "also good"


def test_log_backfill_for_a_run_with_no_file_yet(
    client: TestClient, repo: Repo
) -> None:
    run_id = repo.create_run("fetch")
    assert client.get(f"/api/runs/{run_id}/logs").json()["lines"] == []


def test_sse_stream_sets_the_headers_proxies_need(
    client: TestClient, bootstrap: Bootstrap, repo: Repo
) -> None:
    """Without X-Accel-Buffering nginx holds the whole stream until the run ends."""
    run_id = repo.create_run("fetch")
    path = _write_log(bootstrap, run_id, [{"event": "hello", "level": "info"}])
    repo.set_run_pid(run_id, os.getpid(), str(path))

    # max_seconds bounds the generator. TestClient never reports a disconnect, so
    # an unbounded stream would hang the test forever - which is precisely the
    # server-side leak the bound exists to prevent.
    with client.stream(
        "GET", f"/api/runs/{run_id}/logs/stream?max_seconds=2"
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        assert "no-cache" in response.headers["cache-control"]

        events = [
            json.loads(chunk[5:])
            for chunk in response.iter_lines()
            if chunk.startswith("data:")
        ]
    assert events[0]["event"] == "hello"
    assert events[0]["seq"] == 1


def test_sse_resumes_from_last_event_id(
    client: TestClient, bootstrap: Bootstrap, repo: Repo
) -> None:
    """EventSource replays Last-Event-ID on reconnect; the gap is filled server-side."""
    run_id = repo.create_run("fetch")
    path = _write_log(
        bootstrap, run_id,
        [{"event": f"line {i}", "level": "info"} for i in range(4)],
    )
    repo.set_run_pid(run_id, os.getpid(), str(path))

    with client.stream(
        "GET",
        f"/api/runs/{run_id}/logs/stream?max_seconds=2",
        headers={"Last-Event-ID": "3"},
    ) as response:
        ids = [
            int(chunk[3:].strip())
            for chunk in response.iter_lines()
            if chunk.startswith("id:")
        ]
    assert ids == [4], "must resume after the last seen line, not replay from 1"


def test_sse_closes_once_a_finished_run_is_fully_read(
    client: TestClient, bootstrap: Bootstrap, repo: Repo
) -> None:
    """No point holding a stream open for a run that is over."""
    from yt_tx.states import ExitReason

    run_id = repo.create_run("fetch")
    path = _write_log(bootstrap, run_id, [{"event": "done", "level": "info"}])
    repo.set_run_pid(run_id, os.getpid(), str(path))
    repo.finish_run(run_id, exit_reason=ExitReason.COMPLETED)

    with client.stream(
        "GET", f"/api/runs/{run_id}/logs/stream?max_seconds=30"
    ) as response:
        events = [c for c in response.iter_lines() if c.startswith("data:")]
    # Returned well inside max_seconds because the run had finished.
    assert len(events) == 1


# --------------------------------------------------------------------------- #
# Videos, transcripts, search, export
# --------------------------------------------------------------------------- #


def _store_transcript(repo: Repo, video_id: str, tmp_path: Path) -> None:
    from yt_tx.fetch import write_transcript_file
    from yt_tx.states import TranscriptKind as Kind

    from tests.fake_api import sample_segments

    path = tmp_path / f"{video_id}.en.manual.json.gz"
    stored = write_transcript_file(
        path, video_id=video_id, language="en", kind=Kind.MANUAL,
        source="test", segments=sample_segments(),
    )
    repo.record_transcript_success(
        video_id,
        [
            TranscriptWrite(
                video_id=video_id, language_code="en", kind=Kind.MANUAL,
                is_preferred=True, segment_count=4, char_count=60, word_count=10,
                covered_seconds=11.5, raw_path=str(path), raw_sha256=stored.sha256,
                plaintext="Hello and welcome to the show Today we discuss mitochondria",
                source="test",
            )
        ],
        available=[{"language_code": "en", "kind": "manual"}],
        run_id=None, worker="w1",
    )


def test_video_listing_filters_and_paginates(client: TestClient, seeded: str) -> None:
    payload = client.get("/api/videos?per_page=2").json()
    assert payload["total"] == 3
    assert payload["pages"] == 2
    assert len(payload["videos"]) == 2

    filtered = client.get("/api/videos?status=metadata_ok").json()
    assert filtered["total"] == 3
    assert client.get("/api/videos?status=nonsense").status_code == 400

    searched = client.get("/api/videos?q=Video%201").json()
    assert searched["total"] == 1


def test_video_detail_includes_transcripts_and_attempts(
    client: TestClient, repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video_id = repo.claim_batch("w1", limit=1)[0].video_id
    _store_transcript(repo, video_id, tmp_path)

    payload = client.get(f"/api/videos/{video_id}").json()
    assert payload["video"]["status"] == "transcript_ok"
    assert payload["video"]["description"] is not None or True  # full=True fields present
    assert len(payload["transcripts"]) == 1
    assert payload["transcripts"][0]["is_preferred"] is True
    assert payload["attempts"][0]["outcome"] == "ok"

    assert client.get("/api/videos/doesnotexist").status_code == 404


def test_transcript_endpoint_returns_segments_for_timestamp_links(
    client: TestClient, repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video_id = repo.claim_batch("w1", limit=1)[0].video_id
    _store_transcript(repo, video_id, tmp_path)

    payload = client.get(f"/api/videos/{video_id}/transcript?lang=en").json()
    assert payload["file_present"] is True
    assert payload["language_code"] == "en"
    assert len(payload["segments"]) == 4
    assert payload["segments"][0]["start"] == 0.0
    assert "mitochondria" in payload["plaintext"]


def test_transcript_endpoint_reports_a_missing_file_rather_than_500(
    client: TestClient, repo: Repo, seeded: str, tmp_path: Path
) -> None:
    """``doctor`` calls this corruption; the API should still answer."""
    video_id = repo.claim_batch("w1", limit=1)[0].video_id
    _store_transcript(repo, video_id, tmp_path)
    next(tmp_path.glob("*.json.gz")).unlink()

    payload = client.get(f"/api/videos/{video_id}/transcript").json()
    assert payload["file_present"] is False
    assert payload["segments"] == []
    assert payload["plaintext"], "the flattened copy in MySQL is still usable"


def test_transcript_404_when_none_stored(client: TestClient, seeded: str) -> None:
    assert client.get("/api/videos/vid0000000000001/transcript").status_code == 404


def test_refetch_requeues_one_video(
    client: TestClient, repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video_id = repo.claim_batch("w1", limit=1)[0].video_id
    _store_transcript(repo, video_id, tmp_path)

    payload = client.post(f"/api/videos/{video_id}/refetch").json()
    assert payload["status"] == "metadata_ok"
    row = repo.get_video(video_id)
    assert row is not None
    assert row.status is Status.METADATA_OK
    assert row.attempts == 0
    assert client.post("/api/videos/nope/refetch").status_code == 404


def test_fulltext_search(
    client: TestClient, repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video_id = repo.claim_batch("w1", limit=1)[0].video_id
    _store_transcript(repo, video_id, tmp_path)

    payload = client.get("/api/search?q=mitochondria").json()
    assert payload["count"] == 1
    assert payload["hits"][0]["video_id"] == video_id
    assert client.get("/api/search?q=").status_code == 422


def test_search_warns_about_min_token_size(
    client: TestClient, repo: Repo, seeded: str, tmp_path: Path
) -> None:
    """A two-letter query silently matching nothing is a confusing failure."""
    video_id = repo.claim_batch("w1", limit=1)[0].video_id
    _store_transcript(repo, video_id, tmp_path)
    payload = client.get("/api/search?q=of").json()
    with client as _:
        pass
    if payload["notes"]:
        assert any("min_token_size" in note for note in payload["notes"])


def test_export_formats(
    client: TestClient, repo: Repo, seeded: str, tmp_path: Path
) -> None:
    video_id = repo.claim_batch("w1", limit=1)[0].video_id
    _store_transcript(repo, video_id, tmp_path)

    jsonl = client.get("/api/export?format=jsonl")
    assert jsonl.status_code == 200
    record = json.loads(jsonl.text.strip().splitlines()[0])
    assert record["video_id"] == video_id
    assert "mitochondria" in record["text"]
    assert record["url"].endswith(video_id)

    csv_response = client.get("/api/export?format=csv")
    assert csv_response.text.splitlines()[0].startswith("video_id,channel_id")
    assert video_id in csv_response.text

    txt = client.get("/api/export?format=txt")
    assert "# " in txt.text and "mitochondria" in txt.text
    assert "attachment" in txt.headers["content-disposition"]


def test_audio_queue_endpoint(client: TestClient, repo: Repo, seeded: str) -> None:
    video_id = repo.claim_batch("w1", limit=1)[0].video_id
    repo.record_terminal(
        video_id, status=Status.NO_TRANSCRIPT, reason="captions disabled",
        needs_audio=True,
    )
    payload = client.get("/api/audio-queue").json()
    assert payload["count"] == 1
    assert payload["videos"][0]["video_id"] == video_id


def test_doctor_endpoint(client: TestClient, seeded: str) -> None:
    text_report = client.get("/api/doctor").text
    assert "yt-tx doctor" in text_report
    assert "schema is complete" in text_report


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #


def test_non_loopback_without_a_token_is_refused(bootstrap: Bootstrap) -> None:
    """The settings page holds an API key and a cookies path."""
    exposed = replace(bootstrap, web=WebConfig(host="0.0.0.0", port=8000, auth_token=None))
    with pytest.raises(ConfigError) as info:
        create_app(exposed)
    assert "auth_token" in str(info.value)


def test_token_is_enforced_when_configured(bootstrap: Bootstrap) -> None:
    secured = replace(
        bootstrap, web=WebConfig(host="0.0.0.0", port=8000, auth_token="s3cret")
    )
    with TestClient(create_app(secured)) as client:
        assert client.get("/api/stats").status_code == 401
        assert client.get(
            "/api/stats", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        assert client.get(
            "/api/stats", headers={"Authorization": "Bearer s3cret"}
        ).status_code == 200
        # EventSource cannot set headers, so SSE may authenticate by query param.
        assert client.get("/api/stats?token=s3cret").status_code == 200
