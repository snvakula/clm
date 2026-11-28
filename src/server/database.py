"""Server schema migrations and transaction handling."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .certificate import utc

SCHEMA = """
CREATE TABLE certificates (
    identifier TEXT PRIMARY KEY,
    api_id TEXT NOT NULL UNIQUE,
    algorithm TEXT NOT NULL,
    account TEXT,
    alias TEXT,
    zone TEXT,
    generation INTEGER NOT NULL DEFAULT 0,
    previous INTEGER,
    serial TEXT,
    not_before TEXT,
    not_after TEXT,
    status TEXT NOT NULL,
    created TEXT NOT NULL,
    last_renewal TEXT,
    last_attempt TEXT,
    last_error TEXT
);
CREATE TABLE clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    token_hash BLOB NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    last_authentication TEXT,
    last_download TEXT
);
CREATE TABLE grants (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    identifier TEXT NOT NULL REFERENCES certificates(identifier),
    granted TEXT NOT NULL,
    PRIMARY KEY (client_id, identifier)
);
CREATE TABLE audit (
    id INTEGER PRIMARY KEY,
    timestamp TEXT NOT NULL,
    operation TEXT NOT NULL,
    actor TEXT NOT NULL,
    identifier TEXT,
    generation INTEGER,
    result TEXT NOT NULL,
    source TEXT,
    detail TEXT
);
CREATE TABLE publication (
    identifier TEXT PRIMARY KEY REFERENCES certificates(identifier),
    version INTEGER NOT NULL,
    account TEXT,
    alias TEXT,
    zone TEXT,
    renewed INTEGER NOT NULL
);
CREATE TABLE bundle_leases (
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    identifier TEXT NOT NULL REFERENCES certificates(identifier),
    version INTEGER NOT NULL,
    token_hash BLOB NOT NULL,
    expires INTEGER NOT NULL,
    PRIMARY KEY (client_id, identifier, version)
);
PRAGMA user_version=1;
"""


def now() -> str:
    return utc(datetime.now(UTC))


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self, *, write: bool = False, create: bool = False) -> Iterator[sqlite3.Connection]:
        mode = "rwc" if create else "rw"
        connection = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialise(self) -> None:
        with self.connect(create=True) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        connection.execute(statement)
            elif version != 1:
                raise ValueError("Unsupported database schema version")

    def all(self, sql: str, values: tuple = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, values)]

    def one(self, sql: str, values: tuple = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, values).fetchone()
            return dict(row) if row is not None else None


def audit(
    connection: sqlite3.Connection,
    operation: str,
    actor: str = "administrator",
    *,
    identifier: str | None = None,
    generation: int | None = None,
    result: str = "success",
    source: str | None = None,
    detail: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO audit (timestamp, operation, actor, identifier, generation,
                              result, source, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (now(), operation, actor, identifier, generation, result, source, detail),
    )
