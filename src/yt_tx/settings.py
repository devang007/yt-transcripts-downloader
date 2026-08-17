"""Configuration: bootstrap (YAML/env) and runtime knobs (the ``settings`` table).

Two tiers, deliberately separated:

* :class:`Bootstrap` - how to reach MySQL, where files live, how to bind the web
  server. Read from YAML on every process start, because you cannot read the DB
  without it.
* :class:`Knobs` - everything else. Seeded from YAML by ``yt-tx init``, then
  owned by the ``settings`` table and editable from the UI.

:data:`KNOB_SPECS` is the single source of truth for the runtime knobs: it drives
DB seeding, ``PUT /api/settings`` validation, and the UI knobs panel (including
which tier a knob belongs to). A knob that exists in :class:`Knobs` but not in
:data:`KNOB_SPECS` is a bug, and is caught at import time.
"""

from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal, Mapping, Sequence, cast

import yaml

Tier = Literal["live", "next_run", "secret"]
"""Which tier a knob belongs to.

``live``     - mirrored into ``runtime_control``; a running worker picks it up
               within 2 seconds.
``next_run`` - stored in ``settings``; read when the next run starts.
``secret``   - stored in ``settings`` but never returned in full by the API.

The UI must label every knob with its tier. A rate-limit slider that silently
does nothing until restart is worse than no slider at all.
"""

_ENV_PATTERN: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(RuntimeError):
    """Configuration is missing or invalid. Always fatal, never retried."""


# --------------------------------------------------------------------------- #
# Knob registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class KnobSpec:
    """Metadata for one runtime knob."""

    key: str
    tier: Tier
    group: str
    kind: Literal["bool", "int", "float", "str", "str_list", "int_list"]
    label: str
    help: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    nullable: bool = False

    def coerce(self, raw: object) -> Any:
        """Validate and normalise a value arriving from JSON or YAML.

        Raises:
            ConfigError: if the value cannot be represented as this knob's kind
                or falls outside the declared bounds.
        """
        if raw is None or raw == "":
            if self.nullable:
                return None
            raise ConfigError(f"{self.key} may not be empty")

        try:
            value = self._coerce_kind(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{self.key}: expected {self.kind}, got {raw!r}") from exc

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise ConfigError(f"{self.key} must be >= {self.minimum} (got {value})")
            if self.maximum is not None and value > self.maximum:
                raise ConfigError(f"{self.key} must be <= {self.maximum} (got {value})")
        if self.choices is not None and value not in self.choices:
            raise ConfigError(
                f"{self.key} must be one of {', '.join(self.choices)} (got {value!r})"
            )
        return value

    def _coerce_kind(self, raw: object) -> Any:
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if lowered in {"true", "1", "yes", "on"}:
                    return True
                if lowered in {"false", "0", "no", "off"}:
                    return False
            if isinstance(raw, int):
                return bool(raw)
            raise ValueError(raw)
        if self.kind == "int":
            if isinstance(raw, bool) or isinstance(raw, (list, dict)):
                raise ValueError(raw)
            return int(cast(Any, raw))
        if self.kind == "float":
            if isinstance(raw, bool) or isinstance(raw, (list, dict)):
                raise ValueError(raw)
            return float(cast(Any, raw))
        if self.kind == "str":
            if isinstance(raw, (list, dict)):
                raise ValueError(raw)
            return str(raw)
        if self.kind == "str_list":
            items = self._as_sequence(raw)
            out = [str(i).strip() for i in items if str(i).strip()]
            if not out:
                raise ConfigError(f"{self.key} must contain at least one entry")
            return out
        # int_list
        items = self._as_sequence(raw)
        out_ints = [int(str(i).strip()) for i in items if str(i).strip()]
        if not out_ints:
            raise ConfigError(f"{self.key} must contain at least one entry")
        return out_ints

    @staticmethod
    def _as_sequence(raw: object) -> Sequence[object]:
        if isinstance(raw, str):
            # Accept "en, en-GB" from a text input.
            return [p for p in re.split(r"[,\s]+", raw) if p]
        if isinstance(raw, (list, tuple)):
            return list(cast("Sequence[object]", raw))
        raise ValueError(raw)


def _spec(*args: Any, **kwargs: Any) -> KnobSpec:
    return KnobSpec(*args, **kwargs)


KNOB_SPECS: Final[tuple[KnobSpec, ...]] = (
    # -- live -------------------------------------------------------------- #
    _spec(
        "concurrency", "live", "Throughput", "int",
        "Worker threads",
        "Parallel transcript fetches. Above ~5 you will be blocked quickly "
        "without residential proxies.",
        minimum=1, maximum=32,
    ),
    _spec(
        "requests_per_second", "live", "Throughput", "float",
        "Requests / second",
        "Sustained token-bucket refill rate, shared across all worker threads.",
        minimum=0.01, maximum=20.0,
    ),
    # -- next run ---------------------------------------------------------- #
    _spec(
        "burst", "next_run", "Throughput", "int",
        "Burst size",
        "Token bucket capacity: how many requests may fire back-to-back.",
        minimum=1, maximum=50,
    ),
    _spec(
        "jitter", "next_run", "Throughput", "float",
        "Jitter",
        "Randomises every inter-request delay by +/- this fraction. Constant "
        "intervals are a fingerprint.",
        minimum=0.0, maximum=1.0,
    ),
    _spec(
        "languages", "next_run", "Transcripts", "str_list",
        "Languages",
        "Preference order. A video whose captions are in none of these becomes "
        "lang_missing rather than no_transcript.",
    ),
    _spec(
        "prefer_manual", "next_run", "Transcripts", "bool",
        "Prefer manual captions",
        "Within a language, take human-written captions over auto-generated.",
    ),
    _spec(
        "store_all_variants", "next_run", "Transcripts", "bool",
        "Store every variant",
        "Download all language/kind combinations, not just the best match. "
        "Multiplies request count and disk use.",
    ),
    _spec(
        "accept_translated", "next_run", "Transcripts", "bool",
        "Accept machine translation",
        "If no configured language exists, translate a translatable track "
        "instead of recording lang_missing.",
    ),
    _spec(
        "include_shorts", "next_run", "Scope", "bool",
        "Include Shorts", "Union in the channel's /shorts tab during discovery.",
    ),
    _spec(
        "include_streams", "next_run", "Scope", "bool",
        "Include livestreams", "Union in the channel's /streams tab during discovery.",
    ),
    _spec(
        "max_duration_seconds", "next_run", "Scope", "int",
        "Max duration (s)",
        "Videos longer than this are skipped. Default 43200 = 12 hours.",
        minimum=1, maximum=360000,
    ),
    _spec(
        "max_attempts", "next_run", "Retries", "int",
        "Max attempts",
        "Per-video attempt ceiling before status becomes failed.",
        minimum=1, maximum=20,
    ),
    _spec(
        "backoff_base_seconds", "next_run", "Retries", "float",
        "Backoff base (s)", "Full-jitter exponential backoff base.",
        minimum=0.1, maximum=60.0,
    ),
    _spec(
        "backoff_cap_seconds", "next_run", "Retries", "float",
        "Backoff cap (s)", "Upper bound on any single retry sleep.",
        minimum=1.0, maximum=3600.0,
    ),
    _spec(
        "lease_seconds", "next_run", "Retries", "int",
        "Claim lease (s)",
        "How long a worker owns a claimed video. A killed worker's rows return "
        "to the queue once this expires.",
        minimum=30, maximum=86400,
    ),
    _spec(
        "consecutive_blocks_to_open", "next_run", "Circuit breaker", "int",
        "Blocks to open",
        "Consecutive HardBlocks that trip the breaker and pause all workers.",
        minimum=1, maximum=20,
    ),
    _spec(
        "cooldown_schedule_seconds", "next_run", "Circuit breaker", "int_list",
        "Cooldown schedule (s)",
        "Escalating pause before each retest. The last value repeats.",
    ),
    _spec(
        "max_reopens", "next_run", "Circuit breaker", "int",
        "Max reopens",
        "After this many failed retests the run exits with circuit_open, "
        "leaving all work queued.",
        minimum=1, maximum=50,
    ),
    _spec(
        "daily_quota_units", "next_run", "YouTube API", "int",
        "Daily quota units",
        "Data API budget. Default project allowance is 10000/day.",
        minimum=1, maximum=100_000_000,
    ),
    _spec(
        "quota_stop_at_pct", "next_run", "YouTube API", "int",
        "Stop at quota %",
        "Stop gracefully at this percentage of the daily budget.",
        minimum=1, maximum=100,
    ),
    _spec(
        "fetcher", "next_run", "Backend", "str",
        "Transcript backend",
        "youtube-transcript-api is faster; yt-dlp survives some formats the "
        "former cannot parse.",
        choices=("youtube-transcript-api", "yt-dlp"),
    ),
    _spec(
        "cookies_file", "next_run", "Backend", "str",
        "Cookies file",
        "Netscape-format cookie jar. Required to reach age-restricted videos.",
        nullable=True,
    ),
    _spec(
        "proxy", "next_run", "Backend", "str",
        "Proxy URL",
        "http(s):// or socks5:// URL. Datacenter IPs get blocked on the caption "
        "endpoint within a few hundred requests; rotating residential proxies "
        "are the practical fix.",
        nullable=True,
    ),
    # -- secret ------------------------------------------------------------ #
    _spec(
        "youtube_api_key", "secret", "YouTube API", "str",
        "Data API key",
        "Used for enumeration and metadata. Without it both fall back to yt-dlp.",
        nullable=True,
    ),
)

KNOB_SPECS_BY_KEY: Final[Mapping[str, KnobSpec]] = {s.key: s for s in KNOB_SPECS}

LIVE_KNOBS: Final[frozenset[str]] = frozenset(
    s.key for s in KNOB_SPECS if s.tier == "live"
)
SECRET_KNOBS: Final[frozenset[str]] = frozenset(
    s.key for s in KNOB_SPECS if s.tier == "secret"
)


# --------------------------------------------------------------------------- #
# Runtime knobs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Knobs:
    """Resolved runtime knobs. Field names match :data:`KNOB_SPECS` keys."""

    concurrency: int = 3
    requests_per_second: float = 0.66
    burst: int = 3
    jitter: float = 0.3
    languages: list[str] = field(default_factory=lambda: ["en", "en-US", "en-GB", "hi"])
    prefer_manual: bool = True
    store_all_variants: bool = False
    accept_translated: bool = False
    include_shorts: bool = True
    include_streams: bool = False
    max_duration_seconds: int = 43200
    max_attempts: int = 4
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 300.0
    lease_seconds: int = 600
    consecutive_blocks_to_open: int = 3
    cooldown_schedule_seconds: list[int] = field(
        default_factory=lambda: [300, 600, 1200, 2400, 3600]
    )
    max_reopens: int = 5
    daily_quota_units: int = 10000
    quota_stop_at_pct: int = 90
    fetcher: str = "youtube-transcript-api"
    cookies_file: str | None = None
    proxy: str | None = None
    youtube_api_key: str | None = None

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> Knobs:
        """Build knobs from stored values, ignoring unknown keys.

        Unknown keys are dropped rather than raising: a downgrade after a knob
        was added must not make the worker unstartable.
        """
        kwargs: dict[str, Any] = {}
        for key, raw in values.items():
            spec = KNOB_SPECS_BY_KEY.get(key)
            if spec is None:
                continue
            kwargs[key] = spec.coerce(raw)
        return cls(**kwargs)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def redacted(self) -> dict[str, Any]:
        """As :meth:`as_dict`, but secrets replaced by a masked hint."""
        out = self.as_dict()
        for key in SECRET_KNOBS:
            out[key] = mask_secret(cast("str | None", out.get(key)))
        return out


def mask_secret(value: str | None) -> str | None:
    """Render a secret as ``****1a2b`` - enough to recognise, useless to steal."""
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * max(4, len(value) - 4) + value[-4:]


_KNOB_FIELDS: Final[frozenset[str]] = frozenset(f.name for f in dataclasses.fields(Knobs))
if _KNOB_FIELDS != frozenset(KNOB_SPECS_BY_KEY):
    missing = _KNOB_FIELDS - set(KNOB_SPECS_BY_KEY)
    extra = set(KNOB_SPECS_BY_KEY) - _KNOB_FIELDS
    raise AssertionError(
        f"Knobs/KNOB_SPECS drift - unspecified fields: {sorted(missing)}, "
        f"unknown specs: {sorted(extra)}"
    )


# --------------------------------------------------------------------------- #
# Bootstrap configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    database: str = "yt_tx"
    user: str = "yt_tx"
    password: str = ""
    charset: str = "utf8mb4"
    pool_size: int = 5
    pool_recycle: int = 3600

    def dsn(self, *, database: str | None = None, hide_password: bool = False) -> str:
        """SQLAlchemy URL.

        ``charset=utf8mb4`` on the connection is not optional: without it the
        driver negotiates latin1 and a title containing an emoji raises
        *Incorrect string value* partway through the first large channel.
        """
        from urllib.parse import quote_plus

        secret = "***" if hide_password else quote_plus(self.password)
        db = self.database if database is None else database
        return (
            f"mysql+pymysql://{quote_plus(self.user)}:{secret}"
            f"@{self.host}:{self.port}/{db}?charset={self.charset}"
        )


@dataclass(frozen=True, slots=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    auth_token: str | None = None

    @property
    def is_loopback(self) -> bool:
        return self.host in {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True, slots=True)
class Bootstrap:
    """Everything needed before the database is reachable."""

    mysql: MySQLConfig
    transcript_dir: Path
    log_dir: Path
    web: WebConfig
    seeds: Mapping[str, Any]
    """Knob values parsed out of the YAML, used only by ``init``."""

    source_path: Path | None = None

    def validate(self) -> None:
        """Fail fast on configurations that are unsafe rather than merely wrong."""
        if not self.web.is_loopback and not self.web.auth_token:
            raise ConfigError(
                f"web.host is {self.web.host!r}, which exposes the settings page "
                "(holding an API key and a cookies path) to the network. "
                "Set web.auth_token to a non-empty value, or bind 127.0.0.1."
            )

    def ensure_dirs(self) -> None:
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def _expand_env(value: str) -> str:
    """Replace ``${VAR}`` with the environment value, or empty string if unset."""
    return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _walk_expand(node: object) -> object:
    if isinstance(node, str):
        return _expand_env(node)
    if isinstance(node, dict):
        return {k: _walk_expand(v) for k, v in cast("dict[str, object]", node).items()}
    if isinstance(node, list):
        return [_walk_expand(v) for v in cast("list[object]", node)]
    return node


def default_config_path() -> Path:
    """First of ``$YT_TX_CONFIG``, ``config.local.yaml``, ``config.yaml``."""
    override = os.environ.get("YT_TX_CONFIG")
    if override:
        return Path(override)
    for candidate in (Path("config.local.yaml"), Path("config.yaml")):
        if candidate.exists():
            return candidate
    return Path("config.yaml")


def load_bootstrap(path: Path | None = None) -> Bootstrap:
    """Read YAML (with ``.env`` loaded first) into a :class:`Bootstrap`.

    Raises:
        ConfigError: if the file is missing, unparsable, or unsafe.
    """
    from dotenv import load_dotenv

    load_dotenv(override=False)

    cfg_path = path or default_config_path()
    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found: {cfg_path} "
            "(copy config.yaml from the repo root, or set YT_TX_CONFIG)"
        )
    try:
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{cfg_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{cfg_path}: top level must be a mapping")

    raw = cast("dict[str, Any]", _walk_expand(loaded))

    my = raw.get("mysql") or {}
    if not isinstance(my, dict):
        raise ConfigError("mysql: must be a mapping")
    mysql = MySQLConfig(
        host=str(my.get("host", "127.0.0.1")),
        port=int(my.get("port", 3306)),
        database=str(my.get("database", "yt_tx")),
        user=str(my.get("user", "yt_tx")),
        password=str(my.get("password", "")),
        charset=str(my.get("charset", "utf8mb4")),
        pool_size=int(my.get("pool_size", 5)),
        pool_recycle=int(my.get("pool_recycle", 3600)),
    )

    web_raw = raw.get("web") or {}
    if not isinstance(web_raw, dict):
        raise ConfigError("web: must be a mapping")
    token = web_raw.get("auth_token") or None
    web = WebConfig(
        host=str(web_raw.get("host", "127.0.0.1")),
        port=int(web_raw.get("port", 8000)),
        auth_token=str(token) if token else None,
    )

    bootstrap = Bootstrap(
        mysql=mysql,
        transcript_dir=Path(str(raw.get("transcript_dir", "data/transcripts"))),
        log_dir=Path(str(raw.get("log_dir", "logs"))),
        web=web,
        seeds=_collect_seeds(raw),
        source_path=cfg_path,
    )
    bootstrap.validate()
    return bootstrap


def _collect_seeds(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Pull knob values out of the YAML, flattening the nested blocks.

    Unset or empty ``${VAR}`` expansions are dropped so the dataclass default
    survives instead of becoming an empty string.
    """
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        if key in KNOB_SPECS_BY_KEY:
            flat[key] = value
    breaker = raw.get("circuit_breaker")
    if isinstance(breaker, dict):
        for key, value in cast("dict[str, Any]", breaker).items():
            if key in KNOB_SPECS_BY_KEY:
                flat[key] = value

    seeds: dict[str, Any] = {}
    for key, value in flat.items():
        spec = KNOB_SPECS_BY_KEY[key]
        if value in (None, "") and spec.nullable:
            seeds[key] = None
            continue
        if value in (None, ""):
            continue  # keep the dataclass default
        seeds[key] = spec.coerce(value)
    return seeds
