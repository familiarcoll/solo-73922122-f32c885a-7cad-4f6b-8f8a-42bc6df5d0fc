"""Targeted tests for the choice-vs-publish race on the fall-back hour.

Scenario under test
-------------------
One device has two different source records both stamped at the repeated
wall-clock time ``2024-11-03 01:30`` (America/New_York).  Two review
windows of the same upload are open:

* the original selection is (S1=early, S2=late) -> distinct UTC instants,
  publishable;
* the second window changes S2 from *late* to *early* and saves while a
  publish request is already in flight -> both rows then map to the same
  UTC instant and must be reported as a conflict.

Two orderings are both required to behave correctly:

1. **choice wins the commit** — the publish must see the new selection
   inside its transaction, reject with a conflict, register no new
   publication, and leave the previous complete CSV as the latest
   download;
2. **publish wins the commit** — the stored CSV is a snapshot and the
   later choice must not alter the already published content.

The ordering is made deterministic by blocking inside the transactions
with threading events / a pre-commit seam.  A multiprocessing test
reproduces ordering 1 with real OS processes sharing one SQLite file,
which is exactly the Docker Compose multi-worker deployment.
"""

from __future__ import annotations

import multiprocessing as mp
import sqlite3
import threading

import pytest

from app import db
from app.publishing import is_publishable

# Two ambiguous rows on the fall-back day; choices decide their UTC.
RACE_CSV = (
    "device_id,source_record_id,revision,local_time,water_level\n"
    "D1,S1,1,2024-11-03 01:30:00,1.0\n"
    "D1,S2,1,2024-11-03 01:30:00,2.0\n"
)

# A clean file used to create an already published "previous complete CSV".
CLEAN_CSV = (
    "device_id,source_record_id,revision,local_time,water_level\n"
    "D9,B1,1,2024-07-01 00:00:00,9.0\n"
)


def _upload(client, text: str) -> str:
    resp = client.post(
        "/upload",
        files={"file": ("data.csv", text.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return resp.headers["location"].split("/")[-1]


def _prepare_race_session(client) -> str:
    """Upload the two-row file and select (S1=early, S2=late)."""
    sid = _upload(client, RACE_CSV)
    for line_no, choice in ((2, "early"), (3, "late")):
        r = client.post(
            f"/review/{sid}/choice",
            data={"line_no": line_no, "choice": choice},
            follow_redirects=False,
        )
        assert r.status_code == 303
    view = db.get_session(sid)
    assert view is not None
    ok, _ = is_publishable(view.as_parse_result())
    assert ok
    return sid


def _publication_count() -> int:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0]
    finally:
        conn.close()


def _latest_pub_id() -> int:
    conn = sqlite3.connect(db.DB_PATH)
    try:
        return conn.execute("SELECT MAX(id) FROM publications").fetchone()[0]
    finally:
        conn.close()


def _publish_clean_predecessor(client) -> tuple[int, str]:
    sid = _upload(client, CLEAN_CSV)
    resp = client.post(f"/review/{sid}/publish", follow_redirects=False)
    assert resp.status_code == 303
    pub_id = int(resp.headers["location"].split("published=")[-1])
    text = client.get(f"/download/publication/{pub_id}").text
    # Re-upload the race file so the race session is active again.
    _prepare_race_session(client)
    return pub_id, text


# --------------------------------------------------------------------------- #
# Ordering 1 — choice commits first — at the db layer with real lock waiting.
# --------------------------------------------------------------------------- #


def test_choice_first_publish_detects_conflict_and_registers_nothing_db(client):
    prev_id, prev_text = _publish_clean_predecessor(client)
    sid = db.latest_session_id()
    before = _publication_count()

    got_lock = threading.Event()   # raw lock holder has taken the write lock
    release_lock = threading.Event()  # gate for the holder to commit

    def hold_write_lock():
        # Mimic set_fold_choice: take the same BEGIN IMMEDIATE write lock
        # and hold it, so the publish blocks on busy_timeout until released.
        conn = sqlite3.connect(db.DB_PATH, timeout=15, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        got_lock.set()
        release_lock.wait(timeout=15)
        # Now apply the competing choice (late -> early) and commit first.
        conn.execute(
            "UPDATE rows SET fold_choice = 'early', utc_time = ? "
            "WHERE session_id = ? AND line_no = 3",
            ("2024-11-03T05:30:00+00:00", sid),
        )
        conn.commit()
        conn.close()

    holder = threading.Thread(target=hold_write_lock)
    errors: list[BaseException] = []

    def publish_after_choice():
        got_lock.wait(timeout=15)  # ensure publish starts while the lock is held
        try:
            db.publish_session(sid)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    publisher = threading.Thread(target=publish_after_choice)
    holder.start()
    publisher.start()

    got_lock.wait(timeout=15)
    release_lock.set()  # choice commits before the publish can proceed
    publisher.join(timeout=30)
    holder.join(timeout=5)

    assert len(errors) == 1
    assert isinstance(errors[0], db.PublishConflict)
    assert any("冲突" in reason for reason in errors[0].reasons)

    # Nothing was registered and the previous complete CSV stays latest.
    assert _publication_count() == before
    assert _latest_pub_id() == prev_id
    assert db.latest_publication()["content"] == prev_text
    latest = client.get("/download/latest")
    assert latest.status_code == 200 and latest.text == prev_text


# --------------------------------------------------------------------------- #
# Ordering 1 — through the HTTP API end to end.
# --------------------------------------------------------------------------- #


def test_choice_first_publish_rejected_over_http(client):
    prev_id, prev_text = _publish_clean_predecessor(client)
    sid = db.latest_session_id()
    before = _publication_count()

    got_lock = threading.Event()
    release_lock = threading.Event()

    def hold_write_lock():
        conn = sqlite3.connect(db.DB_PATH, timeout=15, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        got_lock.set()
        release_lock.wait(timeout=15)
        conn.execute(
            "UPDATE rows SET fold_choice = 'early', utc_time = ? "
            "WHERE session_id = ? AND line_no = 3",
            ("2024-11-03T05:30:00+00:00", sid),
        )
        conn.commit()
        conn.close()

    statuses: list[int] = []
    bodies: list[str] = []

    def http_publish():
        got_lock.wait(timeout=15)
        resp = client.post(f"/review/{sid}/publish", follow_redirects=False)
        statuses.append(resp.status_code)
        bodies.append(resp.text)

    holder = threading.Thread(target=hold_write_lock)
    publisher = threading.Thread(target=http_publish)
    holder.start()
    publisher.start()
    got_lock.wait(timeout=15)
    release_lock.set()
    publisher.join(timeout=30)
    holder.join(timeout=5)

    assert statuses == [422]
    assert "冲突" in bodies[0]
    assert _publication_count() == before
    latest = client.get("/download/latest")
    assert latest.status_code == 200 and latest.text == prev_text
    assert prev_id == _latest_pub_id()


# --------------------------------------------------------------------------- #
# Ordering 2 — publish commits first — the later choice cannot change the CSV.
# --------------------------------------------------------------------------- #


def test_publish_first_later_choice_does_not_change_published_csv(client):
    sid = _prepare_race_session(client)
    committed = threading.Event()
    proceed = threading.Event()

    def seam(_session_id: str) -> None:
        committed.set()
        proceed.wait(timeout=15)

    original_seam = db._publish_pre_commit
    db._publish_pre_commit = seam
    try:
        result: dict = {}

        def publish():
            result["meta"] = db.publish_session(sid)

        publisher = threading.Thread(target=publish)
        publisher.start()
        committed.wait(timeout=15)  # publication row inserted, lock still held

        # While the publish transaction is still open, the competing choice
        # must block on the write lock rather than sneak in before commit.
        choice_error: list[BaseException] = []

        def late_choice():
            try:
                db.set_fold_choice(sid, 3, "early")
            except BaseException as exc:  # noqa: BLE001
                choice_error.append(exc)

        chooser = threading.Thread(target=late_choice)
        chooser.start()
        chooser.join(timeout=2)
        assert chooser.is_alive(), "choice must wait for the publish lock"

        proceed.set()
        publisher.join(timeout=15)
        chooser.join(timeout=30)
        assert choice_error == []
    finally:
        db._publish_pre_commit = original_seam

    pub_id = result["meta"]["id"]
    published_csv = result["meta"]["content"]
    assert published_csv.count("\n") == 3  # header + two rows

    # The later choice is now persisted in the review session...
    view = db.get_session(sid)
    row3 = next(r for r in view.kept_rows if r.line_no == 3)
    assert row3.fold_choice == "early"

    # ...but the already published snapshot still reflects (early, late).
    stored = client.get(f"/download/publication/{pub_id}").text
    assert stored == published_csv
    assert "2024-11-03T05:30:00Z,1.0" in stored  # S1 early
    assert "2024-11-03T06:30:00Z,2.0" in stored  # S2 late at publish time
    assert client.get("/download/latest").text == published_csv


# --------------------------------------------------------------------------- #
# Multi-process: two OS processes sharing one SQLite file (Compose workers).
# --------------------------------------------------------------------------- #


def _mp_choice_holder(db_path: str, sid: str, got_lock, release_lock, queue):
    """Child process: hold the write lock, then commit late->early first."""
    try:
        conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        got_lock.set()
        if not release_lock.wait(timeout=20):
            raise RuntimeError("timed out waiting to commit choice")
        conn.execute(
            "UPDATE rows SET fold_choice = 'early', utc_time = ? "
            "WHERE session_id = ? AND line_no = 3",
            ("2024-11-03T05:30:00+00:00", sid),
        )
        conn.commit()
        conn.close()
        queue.put(("ok", None))
    except BaseException as exc:  # noqa: BLE001
        queue.put(("error", repr(exc)))


def _mp_publisher(sid: str, got_lock, queue):
    """Child process: publish after the holder has acquired the lock."""
    try:
        if not got_lock.wait(timeout=20):
            raise RuntimeError("timed out waiting for lock holder")
        db.publish_session(sid)
        queue.put(("published", None))
    except db.PublishConflict as exc:
        queue.put(("conflict", exc.reasons))
    except BaseException as exc:  # noqa: BLE001
        queue.put(("error", repr(exc)))


@pytest.mark.skipif(
    "fork" not in mp.get_all_start_methods(),
    reason="multiprocessing test relies on fork to inherit the app state",
)
def test_choice_first_publish_conflict_across_processes(client, tmp_path):
    # Give each process an isolated copy of the database so the test never
    # touches other tests' rows.
    shared_db = tmp_path / "shared.db"
    original_path = db.DB_PATH
    db.DB_PATH = shared_db
    db.init_db()
    try:
        sid = _prepare_race_session(client)
        before = _publication_count()
        assert before == 0

        ctx = mp.get_context("fork")
        got_lock = ctx.Event()
        release_lock = ctx.Event()
        queue = ctx.Queue()

        holder = ctx.Process(
            target=_mp_choice_holder,
            args=(str(shared_db), sid, got_lock, release_lock, queue),
        )
        publisher = ctx.Process(
            target=_mp_publisher, args=(sid, got_lock, queue)
        )
        holder.start()
        got_lock.wait(timeout=20)
        publisher.start()
        release_lock.set()

        outcomes = [queue.get(timeout=30) for _ in range(2)]
        holder.join(timeout=10)
        publisher.join(timeout=10)
        assert holder.exitcode == 0
        assert publisher.exitcode == 0
    finally:
        db.DB_PATH = original_path

    kinds = {kind for kind, _ in outcomes}
    assert kinds == {"ok", "conflict"}
    conflict_reasons = next(payload for kind, payload in outcomes
                            if kind == "conflict")
    assert any("冲突" in reason for reason in conflict_reasons)

    # Re-point at the shared file to assert its final state.
    conn = sqlite3.connect(shared_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0
        choice = conn.execute(
            "SELECT fold_choice FROM rows WHERE session_id = ? AND line_no = 3",
            (sid,),
        ).fetchone()[0]
        assert choice == "early"
    finally:
        conn.close()


def test_wal_journal_mode_is_enabled(client):
    conn = sqlite3.connect(db.DB_PATH)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()
    assert mode == "wal"
