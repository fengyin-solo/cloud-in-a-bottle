"""Unit tests for the bounded app status transition history core."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from compute_space.core import app_status_history as hist
from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    cfg_path = tmp_path / "data" / "test.db"
    init_db(str(cfg_path))
    conn = sqlite3.connect(str(cfg_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _insert_app(db: sqlite3.Connection, name: str = "myapp", status: str = "building") -> str:
    app_id = new_app_id()
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
        "VALUES (?, ?, '1.0', '/repo/x', ?, ?)",
        (app_id, name, 20000 + len(name), status),
    )
    db.commit()
    return app_id


def _rows(db: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    return list(
        db.execute(
            "SELECT seq, from_status, to_status, changed_at, error_message, source "
            "FROM app_status_history WHERE app_id = ? ORDER BY seq",
            (app_id,),
        ).fetchall()
    )


def test_record_initial_inserts_birth_row_and_is_idempotent(db: sqlite3.Connection) -> None:
    app_id = _insert_app(db, status="building")
    hist.record_initial(db, app_id)
    hist.record_initial(db, app_id)  # replay must not fabricate a second row
    db.commit()

    rows = _rows(db, app_id)
    assert len(rows) == 1
    assert rows[0]["from_status"] is None
    assert rows[0]["to_status"] == "building"
    assert rows[0]["source"] == hist.SOURCE_INSTALL


def test_same_status_does_not_add_rows(db: sqlite3.Connection) -> None:
    app_id = _insert_app(db, status="running")

    hist.record_transition(db, app_id, "running", source=hist.SOURCE_STARTUP_RECOVERY)
    hist.record_transition(db, app_id, "running", source=hist.SOURCE_STARTUP_RECOVERY)
    db.commit()
    assert _rows(db, app_id) == []

    hist.record_transition(db, app_id, "stopped", source=hist.SOURCE_OWNER_STOP)
    db.commit()
    rows = _rows(db, app_id)
    assert len(rows) == 1
    assert (rows[0]["from_status"], rows[0]["to_status"]) == ("running", "stopped")
    assert rows[0]["source"] == "owner_stop"


def test_error_message_change_updates_last_row_without_new_row_or_timestamp(
    db: sqlite3.Connection,
) -> None:
    app_id = _insert_app(db, status="error")
    hist.record_known_transition(
        db, app_id, "starting", "error", error_message="first error", source=hist.SOURCE_INSTALL
    )
    db.execute(
        "UPDATE app_status_history SET changed_at = '2020-01-01 00:00:00' WHERE app_id = ?",
        (app_id,),
    )
    db.commit()

    # Same status, changed message: fold into the last row only.
    hist.record_transition(db, app_id, "error", error_message="second error")
    db.commit()
    rows = _rows(db, app_id)
    assert len(rows) == 1
    assert rows[0]["error_message"] == "second error"
    assert rows[0]["changed_at"] == "2020-01-01 00:00:00"
    assert (rows[0]["from_status"], rows[0]["to_status"]) == ("starting", "error")

    # A real change afterwards still inserts a fresh row.
    hist.record_transition(db, app_id, "running", error_message=None)
    db.commit()
    rows = _rows(db, app_id)
    assert len(rows) == 2
    assert (rows[1]["from_status"], rows[1]["to_status"]) == ("error", "running")


def test_leave_sentinel_never_touches_error_message_on_same_status(db: sqlite3.Connection) -> None:
    app_id = _insert_app(db, status="starting")
    hist.record_known_transition(
        db, app_id, "running", "starting", error_message="keep me", source=hist.SOURCE_INSTALL
    )
    db.commit()
    # startup-style no-op UPDATE (starting -> starting) that preserves error.
    hist.record_transition(db, app_id, "starting", error_message=hist.LEAVE)
    db.commit()
    rows = _rows(db, app_id)
    assert len(rows) == 1
    assert rows[0]["error_message"] == "keep me"


def test_pruning_keeps_newest_limit_and_marks_truncation(db: sqlite3.Connection) -> None:
    app_id = _insert_app(db, status="building")
    # 55 real transitions (building -> starting -> error -> starting -> ...).
    cycle = ["starting", "error", "starting", "running", "stopped"]
    status = "building"
    for i in range(55):
        nxt = cycle[i % len(cycle)]
        hist.record_known_transition(db, app_id, status, nxt, source=hist.SOURCE_SYSTEM)
        status = nxt
    db.commit()

    rows = _rows(db, app_id)
    assert [r["seq"] for r in rows] == list(range(6, 56))
    assert len(rows) == hist.HISTORY_LIMIT

    window = hist.get_history_window(db, app_id, limit=hist.HISTORY_LIMIT)
    assert window is not None
    assert window.returned == 50
    assert window.retained == 50
    assert window.truncated is True

    default_window = hist.get_history_window(db, app_id)  # default 20
    assert default_window is not None
    assert default_window.returned == 20
    assert [e.seq for e in default_window.events] == list(range(36, 56))
    # Retention gap stays visible through the smaller result window.
    assert default_window.truncated is True


def test_truncated_false_when_history_complete(db: sqlite3.Connection) -> None:
    app_id = _insert_app(db, status="building")
    hist.record_known_transition(db, app_id, "building", "running", source=hist.SOURCE_INSTALL)
    db.execute("UPDATE apps SET status = 'running' WHERE app_id = ?", (app_id,))
    current = "running"
    for i in range(9):
        nxt = "stopped" if i % 2 == 0 else "running"
        hist.record_known_transition(db, app_id, current, nxt, source=hist.SOURCE_SYSTEM)
        current = nxt
    db.commit()

    window = hist.get_history_window(db, app_id)
    assert window is not None
    assert window.returned == 10
    assert window.retained == 10
    assert window.truncated is False
    assert window.current_status == "running"
    assert [e.seq for e in window.events] == list(range(1, 11))
    assert window.events[0].from_status == "building"


def test_window_for_unknown_app_is_none_and_empty_old_app_has_empty_events(
    db: sqlite3.Connection,
) -> None:
    assert hist.get_history_window(db, new_app_id()) is None

    app_id = _insert_app(db, name="legacy", status="running")  # migrated-style, no history
    window = hist.get_history_window(db, app_id)
    assert window is not None
    assert window.current_status == "running"
    assert window.events == []
    assert window.returned == 0
    assert window.truncated is False
    clues = hist.derive_failure_clues(window.events)
    assert clues == hist.FailureClues(error_count=0, last_error_at=None, attempts_since_last_running=0)


def test_derive_failure_clues_counts_errors_and_restart_attempts() -> None:
    def ev(seq: int, to: str) -> hist.StatusEvent:
        return hist.StatusEvent(
            seq=seq,
            from_status="x",
            to_status=to,
            changed_at=f"2026-01-0{seq:02d} 00:00:0{seq}",
            error_message=None,
            source="s",
        )

    events = [ev(1, "running"), ev(2, "error"), ev(3, "starting"), ev(4, "error"), ev(5, "starting")]
    clues = hist.derive_failure_clues(events)
    assert clues.error_count == 2
    assert clues.last_error_at == events[3].changed_at
    # Events after the newest running: error, starting, error, starting => 4 attempts.
    assert clues.attempts_since_last_running == 4

    no_running = [ev(1, "error"), ev(2, "starting")]
    clues2 = hist.derive_failure_clues(no_running)
    assert clues2.attempts_since_last_running == 2


def test_failed_history_write_does_not_break_main_transaction(db: sqlite3.Connection) -> None:
    app_id = _insert_app(db, status="running")

    # Break the history table so every history statement fails; apps is intact.
    db.execute("ALTER TABLE app_status_history RENAME TO app_status_history_broken")
    db.commit()

    logs: list[Any] = []
    handler_id = logger.add(lambda message: logs.append(message), level="ERROR")
    try:
        # Must not raise; the main-flow status update still commits in this tx.
        hist.record_transition(db, app_id, "stopped", source=hist.SOURCE_OWNER_STOP)
        db.execute("UPDATE apps SET status = 'stopped', container_id = NULL WHERE app_id = ?", (app_id,))
        db.commit()
    finally:
        logger.remove(handler_id)

    row = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    assert row["status"] == "stopped"
    assert any("Failed to record status transition" in str(record) for record in logs)


def test_history_cascades_with_app_delete(db: sqlite3.Connection) -> None:
    first = _insert_app(db, name="first")
    second = _insert_app(db, name="second")
    hist.record_initial(db, first)
    hist.record_initial(db, second)
    db.commit()

    db.execute("DELETE FROM apps WHERE app_id = ?", (first,))
    db.commit()

    assert db.execute("SELECT COUNT(*) FROM app_status_history WHERE app_id = ?", (first,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM app_status_history WHERE app_id = ?", (second,)).fetchone()[0] == 1


def test_record_transition_for_missing_app_is_noop(db: sqlite3.Connection) -> None:
    # Must not raise even though no apps row exists.
    hist.record_transition(db, new_app_id(), "running")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM app_status_history").fetchone()[0] == 0


def test_status_source_context_var_resets(db: sqlite3.Connection) -> None:
    app_id = _insert_app(db, status="running")
    with hist.status_source(hist.SOURCE_STORAGE_GUARD):
        hist.record_transition(db, app_id, "error", error_message="disk full")
    db.commit()
    # Outside the context the default source applies.
    hist.record_transition(db, app_id, "stopped")
    db.commit()
    rows = _rows(db, app_id)
    assert rows[0]["source"] == hist.SOURCE_STORAGE_GUARD
    assert rows[1]["source"] == hist.SOURCE_SYSTEM
