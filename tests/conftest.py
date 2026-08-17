"""Shared fixtures.

MySQL-backed tests are marked ``mysql`` and skip unless ``YT_TX_TEST_DSN`` points
at a real MySQL 8 server. They are deliberately not run against SQLite:
``FOR UPDATE SKIP LOCKED``, ``INSERT ... ON DUPLICATE KEY UPDATE ... AS new``,
ENUM coercion and FULLTEXT have no SQLite equivalent, and those four mechanisms
are precisely what this project's correctness rests on. A green SQLite suite
would be worse than no suite, because it would be reassuring and wrong.

    docker compose up -d db
    export YT_TX_TEST_DSN='mysql+pymysql://root:devpassword@127.0.0.1:3307/yt_tx_test?charset=utf8mb4'
    pytest -m mysql
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from yt_tx import db as ytdb
from yt_tx.repo import Repo, VideoUpsert
from yt_tx.states import Status


def _requires_mysql() -> str:
    dsn = ytdb.test_dsn()
    if not dsn:
        pytest.skip(
            "set YT_TX_TEST_DSN to a MySQL 8 database to run this test "
            "(see docker-compose.yml)"
        )
    return dsn


@pytest.fixture(scope="session")
def mysql_engine() -> Iterator[Engine]:
    """Session-wide engine against the disposable test database."""
    dsn = _requires_mysql()

    # Create the database if the DSN names one that does not exist yet.
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    db_name = url.database
    if db_name:
        # Two SQLAlchemy traps in one line, both silent:
        #   * URL.set(database=None) means "leave it unchanged", not "clear it",
        #     so the admin engine would connect to the very database it is
        #     supposed to create. Empty string is the way to drop it.
        #   * URL.__str__ renders the password as "***", so a str() round-trip
        #     authenticates with the literal string "***".
        admin_url = url.set(database="").render_as_string(hide_password=False)
        admin = ytdb.engine_from_url(admin_url, pool_size=1)
        try:
            with admin.begin() as conn:
                conn.execute(
                    text(ytdb.CREATE_DATABASE_TEMPLATE.format(db=db_name))
                )
        finally:
            admin.dispose()

    engine = ytdb.engine_from_url(dsn, pool_size=8)
    with engine.begin() as conn:
        ytdb.create_schema(conn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def engine(mysql_engine: Engine) -> Iterator[Engine]:
    """A clean database for each test."""
    with mysql_engine.begin() as conn:
        ytdb.truncate_all(conn)
    yield mysql_engine


@pytest.fixture
def repo(engine: Engine) -> Repo:
    return Repo(engine)


@pytest.fixture
def channel(repo: Repo) -> str:
    """One resolved channel, since ``videos`` has an FK to ``channels``."""
    repo.upsert_channel(
        channel_id="UCtest0000000000000001",
        input_ref="@testchannel",
        handle="@testchannel",
        title="Test Channel",
        uploads_playlist_id="UUtest0000000000000001",
        reported_video_count=3,
    )
    return "UCtest0000000000000001"


@pytest.fixture
def seeded(repo: Repo, channel: str) -> str:
    """Three videos in ``metadata_ok``, ready to be claimed."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    repo.upsert_videos(
        [
            VideoUpsert(
                video_id=f"vid{i:013d}",
                channel_id=channel,
                title=f"Video {i}",
                published_at=now - timedelta(days=i),
            )
            for i in range(1, 4)
        ]
    )
    with repo.begin() as conn:
        conn.execute(text("UPDATE videos SET status = 'metadata_ok'"))
    return channel


@pytest.fixture
def tmp_transcript_dir(tmp_path: Path) -> Path:
    target = tmp_path / "transcripts"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture(autouse=True)
def _no_accidental_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Fail loudly if a unit test tries to reach the network.

    Tests marked ``integration`` opt out. Everything else must be fixture-driven:
    a test suite that quietly depends on YouTube being reachable is a test suite
    that fails at 2am for reasons unrelated to the code.
    """
    if request.node.get_closest_marker("integration"):
        return
    if os.environ.get("YT_TX_ALLOW_TEST_NETWORK"):
        return

    import socket as socket_module

    real_connect = socket_module.socket.connect
    allowed_ports = {3306, 3307}

    def guarded(self: socket_module.socket, address: object) -> None:  # type: ignore[misc]
        if isinstance(address, tuple) and len(address) >= 2:
            port = address[1]
            if isinstance(port, int) and port in allowed_ports:
                real_connect(self, address)
                return
        if isinstance(address, str):  # unix socket, e.g. local MySQL
            real_connect(self, address)
            return
        raise AssertionError(
            f"unit test attempted a network connection to {address!r}; "
            "use a fixture, or mark the test with @pytest.mark.integration"
        )

    monkeypatch.setattr(socket_module.socket, "connect", guarded)


def status_of(repo: Repo, video_id: str) -> Status:
    row = repo.get_video(video_id)
    assert row is not None, f"{video_id} not found"
    return row.status
