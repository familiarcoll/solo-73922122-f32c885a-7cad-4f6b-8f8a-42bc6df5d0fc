"""SQLite persistence.

Two kinds of state live here:

* **upload sessions** — every upload gets a monotonically increasing
  serial.  Only the newest session is *active*; every older session is
  *superseded*.  A superseded session's pages can still be viewed, but
  fold choices and publication are rejected (HTTP 409).  This is what
  makes preview/selection strictly per-file even across browsers.
* **publications** — the full result of every successful release.  The
  newest row is always the downloadable "previous complete result",
  which remains available after a later upload fails validation.

Concurrency model (safe with several service processes sharing one file)
-----------------------------------------------------------------------

The database runs in WAL mode and every mutating operation opens with
``BEGIN IMMEDIATE``, which takes the database write lock up front.  A
publication therefore runs as one serialized write transaction: the rows
are re-read *inside* the lock and re-validated, so a fold choice saved by
another window/process while a publish was in flight is either visible to
the publish (and its conflict blocks it, with nothing registered) or is
forced to wait until the publish has committed the immutable CSV
snapshot.  ``busy_timeout`` makes contenders wait for the lock instead
of failing immediately.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import KeptRow, ParseResult
from .publishing import apply_choice, is_publishable, render_utc_csv

DB_PATH = Path(os.environ.get("TIDEDB_PATH", "/data/tide.db"))

# How long a writer waits on the write lock held by another process
# before raising OperationalError("database is locked").
BUSY_TIMEOUT_MS = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    serial          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    status          TEXT NOT NULL,            -- active | superseded
    created_at      TEXT NOT NULL,
    errors          TEXT NOT NULL,            -- JSON list[str]
    duplicate_lines TEXT NOT NULL,            -- JSON list[int]
    superseded_info TEXT NOT NULL             -- JSON list[dict]
);

CREATE TABLE IF NOT EXISTS rows (
    session_id      TEXT NOT NULL,
    line_no         INTEGER NOT NULL,
    device_id       TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    local_time      TEXT NOT NULL,            -- naive NY wall clock
    raw_local_text  TEXT NOT NULL,
    water_level     REAL NOT NULL,
    time_kind       TEXT NOT NULL,
    fold_choice     TEXT,                     -- early | late | NULL
    utc_time        TEXT,                     -- ISO UTC or NULL
    PRIMARY KEY (session_id, line_no)
);

CREATE TABLE IF NOT EXISTS publications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    row_count       INTEGER NOT NULL,
    content         TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit; transactions are started
    # explicitly with BEGIN IMMEDIATE so the write lock is taken eagerly.
    conn = sqlite3.connect(
        DB_PATH,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        # WAL lets readers (review pages, downloads) keep working while a
        # choice/publish transaction holds the write lock. The mode is
        # persisted in the database file, so every worker process inherits it.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


class StaleSession(Exception):
    """Raised when a superseded session is used for a mutating action."""


class PublishConflict(Exception):
    """Raised when a publish transaction sees unresolvable problems.

    The transaction is rolled back, so no publication row is registered
    and the previously published complete CSV stays the latest one.
    """

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("；".join(reasons))


# Test seam: invoked after the publication row is inserted but before the
# transaction commits.  Nothing in production code assigns this.
_publish_pre_commit: Optional[Callable[[str], None]] = None


class SessionView:
    """Reconstructed view of one upload, matching ParseResult's needs."""

    def __init__(self, row: sqlite3.Row, kept_rows: list[KeptRow]):
        self.session_id: str = row["session_id"]
        self.serial: int = row["serial"]
        self.filename: str = row["filename"]
        self.status: str = row["status"]
        self.created_at: str = row["created_at"]
        self.errors: list[str] = json.loads(row["errors"])
        self.duplicate_lines: list[int] = json.loads(row["duplicate_lines"])
        self.superseded_info: list[dict] = json.loads(row["superseded_info"])
        self.kept_rows = kept_rows

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def as_parse_result(self) -> ParseResult:
        return ParseResult(
            filename=self.filename,
            kept_rows=self.kept_rows,
            errors=list(self.errors),
            duplicate_lines=list(self.duplicate_lines),
        )


def create_session(session_id: str, filename: str, result: ParseResult) -> SessionView:
    """Persist a new upload and mark every earlier session superseded."""
    now = datetime.now(timezone.utc).isoformat()
    superseded_info = [
        {
            "line_no": r.line_no,
            "device_id": r.device_id,
            "source_record_id": r.source_record_id,
            "revision": r.revision,
        }
        for r in result.superseded_rows
    ]
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE sessions SET status = 'superseded'")
        conn.execute(
            "INSERT INTO sessions (session_id, filename, status, created_at, "
            "errors, duplicate_lines, superseded_info) VALUES (?, ?, 'active', ?, ?, ?, ?)",
            (
                session_id,
                filename,
                now,
                json.dumps(result.errors, ensure_ascii=False),
                json.dumps(result.duplicate_lines),
                json.dumps(superseded_info, ensure_ascii=False),
            ),
        )
        conn.executemany(
            "INSERT INTO rows (session_id, line_no, device_id, source_record_id, "
            "revision, local_time, raw_local_text, water_level, time_kind, "
            "fold_choice, utc_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    r.line_no,
                    r.device_id,
                    r.source_record_id,
                    r.revision,
                    r.local_time.isoformat(),
                    r.raw_local_text,
                    r.water_level,
                    r.time_kind,
                    r.fold_choice,
                    r.utc_time.isoformat() if r.utc_time else None,
                )
                for r in result.kept_rows
            ],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    view = get_session(session_id)
    assert view is not None
    return view


def _load_kept_rows(conn: sqlite3.Connection, session_id: str) -> list[KeptRow]:
    records = conn.execute(
        "SELECT * FROM rows WHERE session_id = ? ORDER BY line_no", (session_id,)
    ).fetchall()
    rows: list[KeptRow] = []
    for rec in records:
        utc_text = rec["utc_time"]
        rows.append(
            KeptRow(
                line_no=rec["line_no"],
                device_id=rec["device_id"],
                source_record_id=rec["source_record_id"],
                revision=rec["revision"],
                local_time=datetime.fromisoformat(rec["local_time"]),
                raw_local_text=rec["raw_local_text"],
                water_level=rec["water_level"],
                time_kind=rec["time_kind"],
                fold_choice=rec["fold_choice"],
                utc_time=datetime.fromisoformat(utc_text) if utc_text else None,
            )
        )
    return rows


def get_session(session_id: str) -> Optional[SessionView]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return SessionView(row, _load_kept_rows(conn, session_id))
    finally:
        conn.close()


def latest_session_id() -> Optional[str]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT session_id FROM sessions ORDER BY serial DESC LIMIT 1"
        ).fetchone()
        return row["session_id"] if row else None
    finally:
        conn.close()


def set_fold_choice(session_id: str, line_no: int, choice: str) -> None:
    """Persist one early/late choice; rejects stale sessions.

    Runs as a single ``BEGIN IMMEDIATE`` transaction so it is mutually
    exclusive with a concurrent publish (and any other writer), even when
    the requests land in different worker processes.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT status FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise KeyError(session_id)
        if session["status"] != "active":
            raise StaleSession(session_id)
        rec = conn.execute(
            "SELECT * FROM rows WHERE session_id = ? AND line_no = ?",
            (session_id, line_no),
        ).fetchone()
        if rec is None:
            raise KeyError(f"row {line_no}")
        row = KeptRow(
            line_no=rec["line_no"],
            device_id=rec["device_id"],
            source_record_id=rec["source_record_id"],
            revision=rec["revision"],
            local_time=datetime.fromisoformat(rec["local_time"]),
            raw_local_text=rec["raw_local_text"],
            water_level=rec["water_level"],
            time_kind=rec["time_kind"],
        )
        error = apply_choice(row, choice)
        if error:
            raise ValueError(error)
        conn.execute(
            "UPDATE rows SET fold_choice = ?, utc_time = ? "
            "WHERE session_id = ? AND line_no = ?",
            (row.fold_choice, row.utc_time.isoformat(), session_id, line_no),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def publish_session(session_id: str) -> dict:
    """Publish one session atomically.

    The whole decision runs inside one ``BEGIN IMMEDIATE`` transaction:

    1. take the database write lock, blocking any concurrent choice or
       publish (across processes, via SQLite file locking + busy timeout);
    2. re-read the session and every row *within* the lock, so choices
       saved just now cannot be missed by a stale in-memory snapshot;
    3. re-run the full publishability check, including UTC conflicts;
    4. on any problem roll back — no publication row is registered and
       the previous complete CSV remains the latest download;
    5. otherwise insert the rendered CSV snapshot and commit.  Later
       choices can no longer touch the stored content.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise KeyError(session_id)
        if session["status"] != "active":
            raise StaleSession(session_id)

        view = SessionView(session, _load_kept_rows(conn, session_id))
        result = view.as_parse_result()
        ok, reasons = is_publishable(result)
        if not ok:
            raise PublishConflict(reasons)

        csv_text = render_utc_csv(result)
        published_at = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO publications (session_id, source_filename, "
            "published_at, row_count, content) VALUES (?, ?, ?, ?, ?)",
            (session_id, view.filename, published_at,
             len(result.kept_rows), csv_text),
        )
        pub_id = cur.lastrowid
        if _publish_pre_commit is not None:
            _publish_pre_commit(session_id)
        conn.commit()
        return {
            "id": pub_id,
            "published_at": published_at,
            "row_count": len(result.kept_rows),
            "source_filename": view.filename,
            "content": csv_text,
        }
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def latest_publication() -> Optional[sqlite3.Row]:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM publications ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def publication_by_id(pub_id: int) -> Optional[sqlite3.Row]:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM publications WHERE id = ?", (pub_id,)
        ).fetchone()
    finally:
        conn.close()
