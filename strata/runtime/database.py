"""保存目录并提供接收、修复和回收所用的事务边界。"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from collections.abc import Callable, Iterator

from strata.runtime.errors import unavailable

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS collections(
        name TEXT PRIMARY KEY,
        retention_seconds INTEGER NOT NULL CHECK(retention_seconds > 0),
        head INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS deposits(
        id TEXT PRIMARY KEY,
        collection TEXT NOT NULL REFERENCES collections(name),
        token TEXT NOT NULL,
        parent INTEGER NOT NULL,
        manifest TEXT NOT NULL,
        manifest_hash TEXT NOT NULL,
        changes TEXT,
        changes_hash TEXT,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','published','canceled','expired')),
        revision INTEGER,
        UNIQUE(collection,token),
        CHECK((status = 'published') = (revision IS NOT NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS expected_parts(
        deposit TEXT NOT NULL REFERENCES deposits(id),
        digest TEXT NOT NULL,
        size INTEGER NOT NULL,
        PRIMARY KEY(deposit,digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS objects(
        digest TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        added_at INTEGER NOT NULL,
        verified_at INTEGER NOT NULL,
        sweep_state TEXT NOT NULL DEFAULT 'live' CHECK(sweep_state IN ('live','marked')),
        marked_at INTEGER,
        CHECK((sweep_state = 'marked') = (marked_at IS NOT NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS received_parts(
        deposit TEXT NOT NULL,
        digest TEXT NOT NULL REFERENCES objects(digest),
        received_at INTEGER NOT NULL,
        PRIMARY KEY(deposit,digest),
        FOREIGN KEY(deposit,digest) REFERENCES expected_parts(deposit,digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS revisions(
        collection TEXT NOT NULL REFERENCES collections(name),
        number INTEGER NOT NULL,
        deposit TEXT NOT NULL UNIQUE REFERENCES deposits(id),
        parent INTEGER NOT NULL,
        manifest_hash TEXT NOT NULL,
        manifest TEXT NOT NULL,
        changes TEXT,
        changes_hash TEXT,
        merge_candidate TEXT,
        published_at INTEGER NOT NULL,
        retain_until INTEGER NOT NULL,
        retired_at INTEGER,
        PRIMARY KEY(collection,number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS revision_parts(
        collection TEXT NOT NULL,
        number INTEGER NOT NULL,
        digest TEXT NOT NULL,
        PRIMARY KEY(collection,number,digest),
        FOREIGN KEY(collection,number) REFERENCES revisions(collection,number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS holds(
        collection TEXT NOT NULL,
        number INTEGER NOT NULL,
        reference TEXT NOT NULL,
        reason TEXT NOT NULL,
        placed_at INTEGER NOT NULL,
        released_at INTEGER,
        PRIMARY KEY(collection,number,reference),
        FOREIGN KEY(collection,number) REFERENCES revisions(collection,number)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS incidents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        digest TEXT NOT NULL,
        opened_at INTEGER NOT NULL,
        observed_at INTEGER NOT NULL,
        reason TEXT NOT NULL,
        resolved_at INTEGER,
        resolution TEXT
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS active_incident ON incidents(digest) WHERE resolved_at IS NULL",
    "CREATE INDEX IF NOT EXISTS revisions_by_digest ON revision_parts(digest,collection,number)",
    "CREATE INDEX IF NOT EXISTS received_by_digest ON received_parts(digest,deposit)",
    "CREATE INDEX IF NOT EXISTS deposit_expiry ON deposits(status,expires_at,id)",
    "CREATE INDEX IF NOT EXISTS sweep_age ON objects(sweep_state,added_at,digest)",
    """
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collection TEXT REFERENCES collections(name),
        kind TEXT NOT NULL,
        entity TEXT NOT NULL,
        at INTEGER NOT NULL,
        payload TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS events_by_collection ON events(collection,id)",
    """
    CREATE TABLE IF NOT EXISTS candidates(
        id TEXT PRIMARY KEY,
        collection TEXT NOT NULL REFERENCES collections(name),
        token TEXT NOT NULL,
        base_revision INTEGER NOT NULL,
        manifest TEXT NOT NULL,
        manifest_hash TEXT NOT NULL,
        changes TEXT NOT NULL,
        changes_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        status TEXT NOT NULL
            CHECK(status IN ('open','published','revoked','expired','closed')),
        published_revision INTEGER,
        concluded_at INTEGER,
        UNIQUE(collection,token),
        CHECK((status = 'published') = (published_revision IS NOT NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_expected_parts(
        candidate TEXT NOT NULL REFERENCES candidates(id),
        digest TEXT NOT NULL,
        size INTEGER NOT NULL,
        PRIMARY KEY(candidate,digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_received_parts(
        candidate TEXT NOT NULL,
        digest TEXT NOT NULL REFERENCES objects(digest),
        received_at INTEGER NOT NULL,
        PRIMARY KEY(candidate,digest),
        FOREIGN KEY(candidate,digest)
            REFERENCES candidate_expected_parts(candidate,digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_dependencies(
        candidate TEXT NOT NULL REFERENCES candidates(id),
        digest TEXT NOT NULL,
        size INTEGER NOT NULL,
        PRIMARY KEY(candidate,digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS merge_decisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate TEXT NOT NULL REFERENCES candidates(id),
        conflict_key TEXT NOT NULL,
        context_hash TEXT NOT NULL,
        conflict_hash TEXT NOT NULL,
        choice TEXT NOT NULL,
        decision TEXT NOT NULL,
        decided_at INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS decisions_by_candidate ON merge_decisions(candidate,id)",
    """
    CREATE TABLE IF NOT EXISTS merges(
        candidate TEXT PRIMARY KEY REFERENCES candidates(id),
        collection TEXT NOT NULL,
        revision INTEGER NOT NULL,
        merge_deposit TEXT NOT NULL REFERENCES deposits(id),
        base_revision INTEGER NOT NULL,
        base_manifest_hash TEXT NOT NULL,
        head_revision INTEGER NOT NULL,
        head_manifest_hash TEXT NOT NULL,
        manifest_hash TEXT NOT NULL,
        context_hash TEXT NOT NULL,
        decisions TEXT NOT NULL,
        published_at INTEGER NOT NULL,
        UNIQUE(collection,revision)
    )
    """,
    "CREATE INDEX IF NOT EXISTS candidate_expiry ON candidates(status,expires_at,id)",
)

MIGRATION_V2 = tuple(
    statement for statement in SCHEMA
    if any(key in statement for key in (
        "candidates", "candidate_expected_parts", "candidate_received_parts",
        "candidate_dependencies", "merge_decisions", "decisions_by_candidate",
        "CREATE TABLE IF NOT EXISTS merges", "candidate_expiry",
    ))
)


class Database:
    def __init__(self, path: Path, clock: Callable[[], int] | None = None):
        self.path = path
        self.clock = clock or (lambda: int(time.time()))
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise RuntimeError(f"Unsupported vault catalog version {version}")
            if version == 0:
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute("PRAGMA user_version=2")
            elif version == 1:
                self._migrate_one_to_two(connection)
                connection.execute("PRAGMA user_version=2")

    @staticmethod
    def _migrate_one_to_two(connection: sqlite3.Connection) -> None:
        for table, column, declaration in (
            ("deposits", "changes", "TEXT"),
            ("deposits", "changes_hash", "TEXT"),
            ("revisions", "changes", "TEXT"),
            ("revisions", "changes_hash", "TEXT"),
            ("revisions", "merge_candidate", "TEXT"),
        ):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        for statement in MIGRATION_V2:
            connection.execute(statement)

    def now(self) -> int:
        return self.clock()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as error:
            connection.rollback()
            if getattr(error, "sqlite_errorcode", 0) & 255 in (
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED
            ):
                raise unavailable("Archive is busy, retry the operation") from error
            raise
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
