"""Config loading, knob validation, and the state machine. All pure."""

from __future__ import annotations

from pathlib import Path

import pytest

from yt_tx.settings import (
    KNOB_SPECS,
    KNOB_SPECS_BY_KEY,
    LIVE_KNOBS,
    SECRET_KNOBS,
    ConfigError,
    Knobs,
    MySQLConfig,
    WebConfig,
    load_bootstrap,
    mask_secret,
)
from yt_tx.states import (
    CLAIMABLE,
    LEGAL_TRANSITIONS,
    NEEDS_AUDIO_STATUSES,
    TERMINAL,
    IllegalTransition,
    Status,
    assert_transition,
    is_reprocessed,
)

# --------------------------------------------------------------------------- #
# Knob registry
# --------------------------------------------------------------------------- #


def test_every_knob_is_documented_and_tiered() -> None:
    """The UI cannot label a knob's tier if the registry does not carry one."""
    for spec in KNOB_SPECS:
        assert spec.tier in {"live", "next_run", "secret"}, spec.key
        assert spec.label, spec.key
        assert spec.help and len(spec.help) > 20, f"{spec.key} needs a real explanation"
        assert spec.group, spec.key


def test_live_knobs_are_exactly_the_ones_runtime_control_can_carry() -> None:
    """``runtime_control`` has columns for these three and nothing else.

    A knob marked live that has nowhere to be written would appear to work and
    silently do nothing, which is the failure this pins down.
    """
    assert LIVE_KNOBS == {"concurrency", "requests_per_second"}
    assert SECRET_KNOBS == {"youtube_api_key"}


def test_numeric_bounds_are_enforced() -> None:
    for key, bad in (
        ("concurrency", 0), ("concurrency", 99),
        ("requests_per_second", 0.0), ("jitter", 1.5),
        ("quota_stop_at_pct", 0), ("quota_stop_at_pct", 101),
        ("max_attempts", 0), ("lease_seconds", 5),
    ):
        with pytest.raises(ConfigError):
            KNOB_SPECS_BY_KEY[key].coerce(bad)


def test_choices_are_enforced() -> None:
    spec = KNOB_SPECS_BY_KEY["fetcher"]
    assert spec.coerce("yt-dlp") == "yt-dlp"
    with pytest.raises(ConfigError):
        spec.coerce("whisper-someday")


def test_bool_accepts_form_values() -> None:
    spec = KNOB_SPECS_BY_KEY["prefer_manual"]
    for truthy in (True, "true", "on", "1", "yes", 1):
        assert spec.coerce(truthy) is True
    for falsy in (False, "false", "off", "0", "no", 0):
        assert spec.coerce(falsy) is False
    with pytest.raises(ConfigError):
        spec.coerce("maybe")


def test_lists_accept_comma_or_whitespace_separated_text() -> None:
    spec = KNOB_SPECS_BY_KEY["languages"]
    assert spec.coerce("en, en-GB  hi") == ["en", "en-GB", "hi"]
    assert spec.coerce(["en", " hi "]) == ["en", "hi"]
    with pytest.raises(ConfigError):
        spec.coerce([])
    with pytest.raises(ConfigError):
        spec.coerce("")


def test_int_list_coercion() -> None:
    spec = KNOB_SPECS_BY_KEY["cooldown_schedule_seconds"]
    assert spec.coerce("300, 600,1200") == [300, 600, 1200]
    assert spec.coerce([300, 600]) == [300, 600]


def test_nullable_knobs_accept_empty() -> None:
    assert KNOB_SPECS_BY_KEY["proxy"].coerce("") is None
    assert KNOB_SPECS_BY_KEY["cookies_file"].coerce(None) is None
    with pytest.raises(ConfigError):
        KNOB_SPECS_BY_KEY["concurrency"].coerce("")


def test_knobs_from_mapping_ignores_unknown_keys() -> None:
    """A downgrade after a knob was added must not make the worker unstartable."""
    knobs = Knobs.from_mapping({"concurrency": 8, "knob_from_the_future": "?"})
    assert knobs.concurrency == 8


def test_knobs_defaults_match_the_spec() -> None:
    knobs = Knobs()
    assert knobs.concurrency == 3
    assert knobs.requests_per_second == pytest.approx(0.66)
    assert knobs.languages == ["en", "en-US", "en-GB", "hi"]
    assert knobs.max_duration_seconds == 43200
    assert knobs.cooldown_schedule_seconds == [300, 600, 1200, 2400, 3600]
    assert knobs.fetcher == "youtube-transcript-api"


def test_secret_masking() -> None:
    assert mask_secret("AIzaSyABCDEFGH1234") == "**************1234"
    assert mask_secret("abcd") == "****"
    assert mask_secret("") is None
    assert mask_secret(None) is None
    assert "ABCDEFGH" not in str(mask_secret("AIzaSyABCDEFGH1234"))


def test_redacted_hides_only_secrets() -> None:
    knobs = Knobs(youtube_api_key="AIzaSyREALSECRET", concurrency=5)
    redacted = knobs.redacted()
    assert redacted["concurrency"] == 5
    assert "REALSECRET" not in str(redacted["youtube_api_key"])


# --------------------------------------------------------------------------- #
# Bootstrap
# --------------------------------------------------------------------------- #


def test_dsn_forces_utf8mb4_and_can_hide_the_password() -> None:
    """A latin1 connection raises *Incorrect string value* on the first emoji."""
    cfg = MySQLConfig(user="yt_tx", password="p@ss word/!", database="yt_tx")
    dsn = cfg.dsn()
    assert "charset=utf8mb4" in dsn
    assert "p%40ss+word%2F%21" in dsn, "special characters must be percent-encoded"
    assert "p@ss" not in cfg.dsn(hide_password=True)
    assert "***" in cfg.dsn(hide_password=True)


def test_loopback_detection() -> None:
    assert WebConfig(host="127.0.0.1").is_loopback is True
    assert WebConfig(host="localhost").is_loopback is True
    assert WebConfig(host="0.0.0.0").is_loopback is False


def test_yaml_is_loaded_with_env_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_DB_PASSWORD", "hunter2")
    monkeypatch.setenv("TEST_API_KEY", "AIzaFromEnv")
    config = tmp_path / "config.yaml"
    config.write_text(
        """
mysql:
  host: db.internal
  port: 3307
  database: harvest
  user: reader
  password: ${TEST_DB_PASSWORD}
transcript_dir: /data/tx
log_dir: /var/log/yt
languages: [en, de]
concurrency: 6
circuit_breaker:
  consecutive_blocks_to_open: 5
  cooldown_schedule_seconds: [60, 120]
  max_reopens: 2
youtube_api_key: ${TEST_API_KEY}
proxy: null
web:
  host: 127.0.0.1
  port: 9000
""",
        encoding="utf-8",
    )
    boot = load_bootstrap(config)
    assert boot.mysql.host == "db.internal"
    assert boot.mysql.password == "hunter2"
    assert boot.transcript_dir == Path("/data/tx")
    assert boot.web.port == 9000

    knobs = Knobs.from_mapping(boot.seeds)
    assert knobs.languages == ["en", "de"]
    assert knobs.concurrency == 6
    # The nested circuit_breaker block is flattened onto the knob namespace.
    assert knobs.consecutive_blocks_to_open == 5
    assert knobs.cooldown_schedule_seconds == [60, 120]
    assert knobs.max_reopens == 2
    assert knobs.youtube_api_key == "AIzaFromEnv"
    assert knobs.proxy is None


def test_unset_env_var_falls_back_to_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpanded ``${VAR}`` must not become the literal string or an empty knob."""
    monkeypatch.delenv("DEFINITELY_NOT_SET", raising=False)
    config = tmp_path / "config.yaml"
    config.write_text(
        "mysql: {database: d}\nyoutube_api_key: ${DEFINITELY_NOT_SET}\n"
        "concurrency: ${DEFINITELY_NOT_SET}\n",
        encoding="utf-8",
    )
    boot = load_bootstrap(config)
    knobs = Knobs.from_mapping(boot.seeds)
    assert knobs.youtube_api_key is None
    assert knobs.concurrency == 3, "should fall back to the default, not to 0"


def test_exposed_host_without_a_token_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "mysql: {database: d}\nweb: {host: 0.0.0.0, port: 8000}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError) as info:
        load_bootstrap(config)
    assert "auth_token" in str(info.value)


def test_exposed_host_with_a_token_is_allowed(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "mysql: {database: d}\nweb: {host: 0.0.0.0, auth_token: abc123}\n",
        encoding="utf-8",
    )
    assert load_bootstrap(config).web.auth_token == "abc123"


def test_missing_config_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as info:
        load_bootstrap(tmp_path / "nope.yaml")
    assert "not found" in str(info.value)


def test_malformed_yaml_is_a_clear_error(tmp_path: Path) -> None:
    config = tmp_path / "bad.yaml"
    config.write_text("mysql: [this: is: not: a: mapping\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_bootstrap(config)


def test_ensure_dirs_creates_both(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"mysql: {{database: d}}\ntranscript_dir: {tmp_path}/tx/deep\n"
        f"log_dir: {tmp_path}/logs\n",
        encoding="utf-8",
    )
    boot = load_bootstrap(config)
    boot.ensure_dirs()
    assert boot.transcript_dir.is_dir()
    assert boot.log_dir.is_dir()


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #


def test_every_status_has_transition_rules() -> None:
    """A status missing from the table would raise KeyError mid-run."""
    assert set(LEGAL_TRANSITIONS) == set(Status)


def test_statuses_are_self_transitional() -> None:
    """Re-recording the same outcome must be legal; retries do it constantly."""
    for status in Status:
        if status is Status.DISCOVERED:
            continue
        assert status in LEGAL_TRANSITIONS[status], status


def test_success_is_effectively_absorbing() -> None:
    """Only an explicit refetch, which goes via metadata_ok, may reopen success."""
    assert LEGAL_TRANSITIONS[Status.TRANSCRIPT_OK] == frozenset(
        {Status.TRANSCRIPT_OK, Status.METADATA_OK}
    )
    for status in (Status.NO_TRANSCRIPT, Status.FAILED, Status.RETRY):
        with pytest.raises(IllegalTransition):
            assert_transition("v", Status.TRANSCRIPT_OK, status)


def test_unavailable_does_not_become_a_transcript() -> None:
    with pytest.raises(IllegalTransition):
        assert_transition("v", Status.UNAVAILABLE, Status.TRANSCRIPT_OK)


def test_illegal_transition_names_the_video_and_both_states() -> None:
    with pytest.raises(IllegalTransition) as info:
        assert_transition("dQw4w9WgXcQ", Status.UNAVAILABLE, Status.LANG_MISSING)
    error = info.value
    assert error.video_id == "dQw4w9WgXcQ"
    assert error.before is Status.UNAVAILABLE
    assert error.after is Status.LANG_MISSING
    assert "dQw4w9WgXcQ" in str(error)


def test_recheck_path_is_legal() -> None:
    """no_transcript -> metadata_ok is how auto-captions get a second chance."""
    assert_transition("v", Status.NO_TRANSCRIPT, Status.METADATA_OK)
    assert_transition("v", Status.LANG_MISSING, Status.METADATA_OK)
    assert_transition("v", Status.NO_TRANSCRIPT, Status.TRANSCRIPT_OK)


def test_reopen_paths_are_legal() -> None:
    assert_transition("v", Status.FAILED, Status.METADATA_OK)
    assert_transition("v", Status.AGE_RESTRICTED, Status.METADATA_OK)
    assert_transition("v", Status.SKIPPED, Status.DISCOVERED)


def test_claimable_statuses_match_the_claim_query() -> None:
    from yt_tx.repo import _CLAIM_SELECT

    assert CLAIMABLE == {Status.METADATA_OK, Status.RETRY}
    for status in CLAIMABLE:
        assert f"'{status.value}'" in _CLAIM_SELECT
    # A terminal status appearing in the claim query would mean infinite rework.
    for status in TERMINAL:
        if status in CLAIMABLE:
            continue
        assert f"'{status.value}'" not in _CLAIM_SELECT, status


def test_skip_hydrate_path_is_legal() -> None:
    """--skip-hydrate never passes through metadata_ok, so the fetch terminals
    must be reachable from `discovered` directly."""
    for status in (
        Status.TRANSCRIPT_OK,
        Status.NO_TRANSCRIPT,
        Status.LANG_MISSING,
        Status.AGE_RESTRICTED,
    ):
        assert_transition("v", Status.DISCOVERED, status)


def test_unhydrated_claim_set_only_adds_discovered() -> None:
    from yt_tx.states import CLAIMABLE_UNHYDRATED

    assert CLAIMABLE_UNHYDRATED - CLAIMABLE == {Status.DISCOVERED}
    for status in TERMINAL:
        assert status not in CLAIMABLE_UNHYDRATED, status


def test_only_text_less_statuses_are_queued_for_audio() -> None:
    assert NEEDS_AUDIO_STATUSES == {Status.NO_TRANSCRIPT}


def test_is_reprocessed_matches_the_spec_table() -> None:
    assert is_reprocessed(Status.DISCOVERED) is True
    assert is_reprocessed(Status.METADATA_OK) is True
    assert is_reprocessed(Status.RETRY) is True
    for status in (
        Status.TRANSCRIPT_OK, Status.NO_TRANSCRIPT, Status.LANG_MISSING,
        Status.UNAVAILABLE, Status.AGE_RESTRICTED, Status.SKIPPED, Status.FAILED,
    ):
        assert is_reprocessed(status) is False, status


def test_status_enum_order_matches_the_ddl() -> None:
    """Appending an ENUM value is in-place in MySQL 8; inserting rewrites the table.

    Keeping the Python enum in the DDL's order is what makes that rule
    checkable at review time.
    """
    from yt_tx.db import SCHEMA_STATEMENTS

    videos_ddl = next(s for s in SCHEMA_STATEMENTS if "CREATE TABLE IF NOT EXISTS videos" in s)
    declared = videos_ddl.split("status ENUM(", 1)[1].split(")", 1)[0]
    order = [part.strip().strip("'") for part in declared.split(",")]
    assert order == [s.value for s in Status]
