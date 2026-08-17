"""Engine construction, connection hygiene, and the schema DDL.

Everything MySQL-specific that bites you at 3am lives here:

* ``charset=utf8mb4`` on the connection, plus ``utf8mb4`` on database and tables.
* ``time_zone='+00:00'`` and ``sql_mode`` forced on *every* pooled connection,
  including ones handed out after a reconnect.
* ``pool_pre_ping`` so a managed MySQL's short ``wait_timeout`` does not kill the
  worker with *MySQL server has gone away* on the first idle gap.
* :func:`with_deadlock_retry`, because InnoDB deadlocks are expected behaviour
  under concurrent claiming, not a bug to debug away.

Time rule: all comparisons and derived timestamps are computed *server-side*
with ``UTC_TIMESTAMP(6)``. Python never sends a "now" into a query. Mixing the
two gives you leases that expire early or never, depending on clock skew.
"""

from __future__ import annotations

import functools
import logging
import os
import random
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Final, ParamSpec, TypeVar

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.exc import DBAPIError, OperationalError

from .settings import MySQLConfig

log = logging.getLogger(__name__)

P = ParamSpec("P")
T = TypeVar("T")

ER_LOCK_DEADLOCK: Final = 1213
ER_LOCK_WAIT_TIMEOUT: Final = 1205
ER_LOCK_NOWAIT: Final = 3572
RETRYABLE_MYSQL_ERRNOS: Final[frozenset[int]] = frozenset(
    {ER_LOCK_DEADLOCK, ER_LOCK_WAIT_TIMEOUT, ER_LOCK_NOWAIT}
)

REQUIRED_SQL_MODE: Final = (
    "STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION,ERROR_FOR_DIVISION_BY_ZERO"
)

MIN_MYSQL_VERSION: Final[tuple[int, int]] = (8, 0)


class SchemaError(RuntimeError):
    """The server or schema cannot support this application."""


def utcnow() -> datetime:
    """Naive UTC now, for log lines and filenames only.

    Never send this into a query that compares against a stored timestamp; use
    ``UTC_TIMESTAMP(6)`` in the SQL instead.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive timestamp read back out of MySQL."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def make_engine(
    cfg: MySQLConfig,
    *,
    database: str | None = None,
    pool_size: int | None = None,
    echo: bool = False,
) -> Engine:
    """Build an engine with the connection hygiene this project depends on.

    Args:
        cfg: MySQL connection settings.
        database: Override the database name; pass ``""`` to connect with no
            default schema (needed to ``CREATE DATABASE``).
        pool_size: Override ``cfg.pool_size``.
        echo: Log every statement.
    """
    return engine_from_url(
        cfg.dsn(database=database),
        pool_size=pool_size if pool_size is not None else cfg.pool_size,
        pool_recycle=cfg.pool_recycle,
        echo=echo,
    )


def engine_from_url(
    url: str,
    *,
    pool_size: int = 5,
    pool_recycle: int = 3600,
    echo: bool = False,
) -> Engine:
    """Build an engine from a DSN string (used by tests via ``YT_TX_TEST_DSN``)."""
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=pool_recycle,
        pool_size=pool_size,
        max_overflow=max(2, pool_size),
        pool_timeout=30,
        future=True,
        echo=echo,
        connect_args={
            "charset": "utf8mb4",
            "connect_timeout": 10,
            # Fail a blocked write in bounded time so the retry decorator can do
            # its job instead of the thread parking for a minute.
            "read_timeout": 120,
            "write_timeout": 120,
        },
    )
    _install_connection_hygiene(engine)
    return engine


def _install_connection_hygiene(engine: Engine) -> None:
    """Force session variables on every new *and reconnected* connection."""

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        with dbapi_conn.cursor() as cur:
            cur.execute("SET SESSION time_zone = '+00:00'")
            cur.execute(f"SET SESSION sql_mode = '{REQUIRED_SQL_MODE}'")
            # Long enough to survive a busy claim batch, short enough that a
            # genuine stall surfaces as errno 1205 and gets retried.
            cur.execute("SET SESSION innodb_lock_wait_timeout = 20")


def check_server(conn: Connection) -> dict[str, Any]:
    """Verify the server can support this application.

    Returns:
        Diagnostics for ``doctor``: version, charset, timezone, packet size.

    Raises:
        SchemaError: on MySQL < 8.0, where ``SKIP LOCKED`` does not exist and
            multi-worker claiming is unsafe.
    """
    version = str(conn.execute(text("SELECT VERSION()")).scalar_one())
    parts: list[int] = []
    for chunk in version.split("-")[0].split("."):
        if chunk.isdigit():
            parts.append(int(chunk))
    tup = tuple(parts[:2])
    if len(tup) < 2 or tup < MIN_MYSQL_VERSION:
        raise SchemaError(
            f"MySQL {version} is too old. 8.0+ is a hard requirement: "
            "SELECT ... FOR UPDATE SKIP LOCKED is what makes concurrent work "
            "claiming safe, and there is no substitute."
        )

    row = conn.execute(
        text(
            "SELECT @@character_set_connection, @@collation_connection, "
            "@@session.time_zone, @@global.max_allowed_packet, @@session.sql_mode"
        )
    ).one()
    diag: dict[str, Any] = {
        "version": version,
        "charset_connection": row[0],
        "collation_connection": row[1],
        "time_zone": row[2],
        "max_allowed_packet": int(row[3]),
        "sql_mode": row[4],
        "skip_locked": True,
    }
    warnings: list[str] = []
    if not str(row[0]).startswith("utf8mb4"):
        warnings.append(
            f"connection charset is {row[0]!r}, not utf8mb4 - emoji in titles "
            "will raise 'Incorrect string value'"
        )
    if str(row[2]) not in {"+00:00", "UTC"}:
        warnings.append(f"session time_zone is {row[2]!r}, expected +00:00")
    if int(row[3]) < 64 * 1024 * 1024:
        warnings.append(
            f"max_allowed_packet is {int(row[3])} bytes; a multi-hour "
            "livestream's plaintext can exceed it. Set >= 64M."
        )
    if "STRICT_TRANS_TABLES" not in str(row[4]):
        warnings.append("sql_mode lacks STRICT_TRANS_TABLES - truncation is silent")
    diag["warnings"] = warnings
    return diag


@contextmanager
def begin(engine: Engine) -> Iterator[Connection]:
    """Transaction scope. Commits on success, rolls back on any exception."""
    with engine.begin() as conn:
        yield conn


# --------------------------------------------------------------------------- #
# Deadlock retry
# --------------------------------------------------------------------------- #


def mysql_errno(exc: BaseException) -> int | None:
    """Extract the MySQL error number from a SQLAlchemy/PyMySQL exception."""
    orig = getattr(exc, "orig", None)
    for candidate in (orig, exc):
        args = getattr(candidate, "args", ())
        if args and isinstance(args[0], int):
            return int(args[0])
    return None


def is_retryable_db_error(exc: BaseException) -> bool:
    """True for deadlock / lock-wait-timeout, which are normal under InnoDB."""
    if not isinstance(exc, DBAPIError):
        return False
    errno = mysql_errno(exc)
    if errno in RETRYABLE_MYSQL_ERRNOS:
        return True
    # A dropped connection mid-statement is retryable when the callable is
    # itself a self-contained transaction, which is the contract here.
    return isinstance(exc, OperationalError) and bool(
        getattr(exc, "connection_invalidated", False)
    )


def with_deadlock_retry(
    attempts: int = 3,
    *,
    base_delay: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Retry a *whole transaction* on deadlock / lock wait timeout.

    The wrapped callable must own its transaction and be safe to run twice: on
    errno 1213 InnoDB has already rolled the transaction back, so resuming
    mid-way is not an option.

    Args:
        attempts: Total tries, including the first.
        base_delay: Backoff base; delay is ``random(0, base * 2**i)``.
        sleep: Injectable for tests.
        rng: Injectable for tests.
    """
    random_source = rng or random.Random()

    def decorate(fn: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            last: BaseException | None = None
            for i in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except DBAPIError as exc:
                    if not is_retryable_db_error(exc) or i == attempts - 1:
                        raise
                    last = exc
                    delay = random_source.uniform(0.0, base_delay * (2**i))
                    log.warning(
                        "db retry %d/%d errno=%s after %.3fs: %s",
                        i + 1, attempts, mysql_errno(exc), delay, fn.__qualname__,
                    )
                    sleep(delay)
            raise AssertionError("unreachable") from last

        return wrapper

    return decorate


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #

CREATE_DATABASE_TEMPLATE: Final = (
    "CREATE DATABASE IF NOT EXISTS `{db}` "
    "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
)

# Kept as literal DDL rather than SQLAlchemy metadata: several things here have
# no clean Core expression (ENUM ordering, FULLTEXT, the singleton CHECK, index
# column order) and the exact text is what gets reviewed.
SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS channels (
      channel_id            VARCHAR(32)   NOT NULL,
      input_ref             VARCHAR(255)  NOT NULL,
      handle                VARCHAR(128)  NULL,
      title                 VARCHAR(512)  NULL,
      uploads_playlist_id   VARCHAR(64)   NULL,
      reported_video_count  INT UNSIGNED  NULL,
      enumeration_cursor    VARCHAR(255)  NULL,
      enumeration_complete  TINYINT(1)    NOT NULL DEFAULT 0,
      last_enumerated_at    DATETIME(6)   NULL,
      newest_published_at   DATETIME(6)   NULL,
      is_enabled            TINYINT(1)    NOT NULL DEFAULT 1,
      created_at            DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      updated_at            DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                          ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (channel_id),
      UNIQUE KEY uq_channels_handle (handle)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS videos (
      video_id               VARCHAR(16)  NOT NULL,
      channel_id             VARCHAR(32)  NOT NULL,
      title                  VARCHAR(1024) NULL,
      description            MEDIUMTEXT   NULL,
      published_at           DATETIME(6)  NULL,
      duration_seconds       INT UNSIGNED NULL,
      view_count             BIGINT UNSIGNED NULL,
      like_count             BIGINT UNSIGNED NULL,
      comment_count          BIGINT UNSIGNED NULL,
      tags_json              JSON         NULL,
      category_id            VARCHAR(8)   NULL,
      default_language       VARCHAR(16)  NULL,
      default_audio_language VARCHAR(16)  NULL,
      live_broadcast_content ENUM('none','live','upcoming') NULL,
      is_short               TINYINT(1)   NULL,
      was_livestream         TINYINT(1)   NULL,
      thumbnail_url          VARCHAR(512) NULL,
      metadata_fetched_at    DATETIME(6)  NULL,
      status ENUM('discovered','metadata_ok','transcript_ok','no_transcript',
                  'lang_missing','unavailable','age_restricted','skipped',
                  'retry','failed') NOT NULL DEFAULT 'discovered',
      status_reason          VARCHAR(255) NULL,
      attempts               SMALLINT UNSIGNED NOT NULL DEFAULT 0,
      recheck_count          TINYINT UNSIGNED  NOT NULL DEFAULT 0,
      next_attempt_at        DATETIME(6)  NULL,
      recheck_after          DATETIME(6)  NULL,
      available_transcripts_json JSON     NULL,
      needs_audio            TINYINT(1)   NOT NULL DEFAULT 0,
      claimed_by             VARCHAR(64)  NULL,
      lease_expires_at       DATETIME(6)  NULL,
      created_at             DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      updated_at             DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                          ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (video_id),
      KEY idx_videos_claim     (status, next_attempt_at, lease_expires_at),
      KEY idx_videos_channel   (channel_id, status),
      KEY idx_videos_audio     (needs_audio, status),
      KEY idx_videos_recheck   (recheck_after),
      KEY idx_videos_listing   (channel_id, published_at DESC),
      CONSTRAINT fk_videos_channel FOREIGN KEY (channel_id)
        REFERENCES channels(channel_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS transcripts (
      id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      video_id       VARCHAR(16)  NOT NULL,
      language_code  VARCHAR(16)  NOT NULL,
      kind           ENUM('manual','asr','translated','whisper') NOT NULL,
      is_preferred   TINYINT(1)   NOT NULL DEFAULT 0,
      segment_count  INT UNSIGNED NULL,
      char_count     INT UNSIGNED NULL,
      word_count     INT UNSIGNED NULL,
      covered_seconds DECIMAL(10,2) NULL,
      raw_path       VARCHAR(512) NOT NULL,
      raw_sha256     CHAR(64)     NOT NULL,
      plaintext      LONGTEXT     NULL,
      source         VARCHAR(32)  NOT NULL,
      fetched_at     DATETIME(6)  NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uq_tx_variant (video_id, language_code, kind),
      FULLTEXT KEY ft_tx_plaintext (plaintext),
      CONSTRAINT fk_tx_video FOREIGN KEY (video_id)
        REFERENCES videos(video_id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS fetch_attempts (
      id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      run_id        BIGINT UNSIGNED NULL,
      video_id      VARCHAR(16) NOT NULL,
      phase         ENUM('metadata','transcript','audio') NOT NULL,
      started_at    DATETIME(6) NOT NULL,
      finished_at   DATETIME(6) NULL,
      outcome       ENUM('ok','retryable','terminal','blocked') NOT NULL,
      error_type    VARCHAR(128) NULL,
      error_message VARCHAR(1024) NULL,
      http_status   SMALLINT UNSIGNED NULL,
      traceback     MEDIUMTEXT NULL,
      worker        VARCHAR(64) NULL,
      PRIMARY KEY (id),
      KEY idx_attempts_video (video_id, started_at),
      KEY idx_attempts_run (run_id, outcome)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS runs (
      id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      command     VARCHAR(64) NOT NULL,
      args_json   JSON NULL,
      pid         INT UNSIGNED NULL,
      host        VARCHAR(128) NULL,
      log_path    VARCHAR(512) NULL,
      started_at  DATETIME(6) NOT NULL,
      finished_at DATETIME(6) NULL,
      heartbeat_at DATETIME(6) NULL,
      counts_json JSON NULL,
      exit_reason ENUM('completed','interrupted','circuit_open',
                       'quota_exhausted','crashed','stopped') NULL,
      PRIMARY KEY (id),
      KEY idx_runs_active (finished_at, started_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_control (
      id                  TINYINT UNSIGNED NOT NULL DEFAULT 1,
      desired_state       ENUM('running','paused','stopping') NOT NULL DEFAULT 'running',
      concurrency         TINYINT UNSIGNED NOT NULL DEFAULT 3,
      requests_per_second DECIMAL(5,3) NOT NULL DEFAULT 0.660,
      updated_at          DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                      ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (id),
      CONSTRAINT chk_singleton CHECK (id = 1)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS settings (
      `key`      VARCHAR(64) NOT NULL,
      value_json JSON NOT NULL,
      updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                             ON UPDATE CURRENT_TIMESTAMP(6),
      PRIMARY KEY (`key`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    """
    CREATE TABLE IF NOT EXISTS api_quota (
      day        DATE NOT NULL,
      units_used INT UNSIGNED NOT NULL DEFAULT 0,
      PRIMARY KEY (day)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """,
    "INSERT IGNORE INTO runtime_control (id) VALUES (1)",
)

TABLE_NAMES: Final[tuple[str, ...]] = (
    "channels", "videos", "transcripts", "fetch_attempts",
    "runs", "runtime_control", "settings", "api_quota",
)

FULLTEXT_INDEX: Final = ("transcripts", "ft_tx_plaintext", "plaintext")


def create_database(cfg: MySQLConfig) -> None:
    """Create the database itself, with utf8mb4. Idempotent."""
    engine = make_engine(cfg, database="", pool_size=1)
    try:
        with engine.begin() as conn:
            conn.execute(text(CREATE_DATABASE_TEMPLATE.format(db=cfg.database)))
    finally:
        engine.dispose()


def create_schema(conn: Connection) -> None:
    """Apply the full schema. Idempotent - every statement is IF NOT EXISTS."""
    check_server(conn)
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(text(stmt.strip()))


def missing_tables(conn: Connection) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE()"
        )
    ).scalars()
    present = {str(r).lower() for r in rows}
    return [t for t in TABLE_NAMES if t not in present]


def fulltext_index_exists(conn: Connection) -> bool:
    table, index, _ = FULLTEXT_INDEX
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t "
            "AND index_name = :i"
        ),
        {"t": table, "i": index},
    ).scalar_one()
    return int(count) > 0


def drop_fulltext_index(conn: Connection) -> bool:
    """Drop the FULLTEXT index before a large bulk load.

    A FULLTEXT index roughly triples insert cost. For a first pass over
    hundreds of thousands of videos, drop it, load, then rebuild.
    """
    table, index, _ = FULLTEXT_INDEX
    if not fulltext_index_exists(conn):
        return False
    conn.execute(text(f"ALTER TABLE {table} DROP INDEX {index}"))
    return True


def build_fulltext_index(conn: Connection) -> bool:
    table, index, column = FULLTEXT_INDEX
    if fulltext_index_exists(conn):
        return False
    conn.execute(text(f"ALTER TABLE {table} ADD FULLTEXT KEY {index} ({column})"))
    return True


def ft_min_token_size(conn: Connection) -> int:
    """``innodb_ft_min_token_size``; defaults to 3, hiding two-letter words."""
    return int(conn.execute(text("SELECT @@innodb_ft_min_token_size")).scalar_one())


# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #


def test_dsn() -> str | None:
    """``YT_TX_TEST_DSN``, if set. Tests marked ``mysql`` skip without it."""
    return os.environ.get("YT_TX_TEST_DSN") or None


def truncate_all(conn: Connection, tables: Sequence[str] = TABLE_NAMES) -> None:
    """Wipe every table, FK order be damned. Test fixture only."""
    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
    try:
        for table in tables:
            conn.execute(text(f"TRUNCATE TABLE {table}"))
    finally:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    conn.execute(text("INSERT IGNORE INTO runtime_control (id) VALUES (1)"))
