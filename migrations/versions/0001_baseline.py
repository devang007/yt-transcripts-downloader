"""Baseline schema.

Revision ID: 0001_baseline
Revises: None

The DDL lives in :data:`yt_tx.db.SCHEMA_STATEMENTS` so that ``yt-tx init`` and
Alembic apply byte-identical definitions. There is exactly one copy of the
schema in this repo, and it is the one that gets code-reviewed.

Note on ENUMs: adding a value to any of the ENUM columns here needs an ALTER
TABLE. Appending to the *end* of the value list is in-place in MySQL 8.0;
inserting in the middle rewrites the whole table. Always append.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from yt_tx.db import SCHEMA_STATEMENTS, TABLE_NAMES

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(text(stmt.strip()))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
    try:
        for table in reversed(TABLE_NAMES):
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    finally:
        conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
