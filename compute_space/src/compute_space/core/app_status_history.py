"""Bounded, durable history of real app status transitions.

Every apps.status write that can reach the dashboard goes through
:func:`record_transition` (or :func:`record_initial` for the install-time
birth row). The read-only /api/app_status poller never writes, so polling can
not pile up duplicate rows.

Contract for callers
--------------------

* Call the recorder INSIDE the same transaction and on the same connection as
  the apps.status UPDATE. The history row then commits or rolls back together
  with the status change.
* :func:`record_transition` reads the CURRENT apps row itself, so invoke it
  immediately BEFORE executing the UPDATE that changes the status.
* Sites that flip status through a conditional UPDATE ... WHERE claim (remove,
  reload) use :func:`record_known_transition` only when the claim succeeded,
  passing the pre-claim status they selected first.
* A recorder failure must never change the outcome of the start/stop flow. All
  history statements run inside a SAVEPOINT; any error rolls back to it and is
  logged, never raised.

Retention
---------

Each app keeps the newest HISTORY_LIMIT rows; seq is a per-app monotonic
counter that is NOT reused after pruning, so a gap between MAX(seq) and the row
count marks where older events were dropped (``HistoryWindow.truncated``).
"""

from __future__ import annotations

import contextlib
import contextvars
import sqlite3
import uuid
from collections.abc import Iterator

import attr

from compute_space.core.logging import logger

#: Newest transitions retained per app. Older rows are pruned on insert.
HISTORY_LIMIT = 50

#: Default window size for the recent-history API.
DEFAULT_FETCH_LIMIT = 20

# Stable trigger-source identifiers. They are stored verbatim and returned by
# the API, so treat the strings as a public contract; the detail page maps them
# to human labels.
SOURCE_INSTALL = "install"
SOURCE_RELOAD = "reload"
SOURCE_UPDATE = "update"
SOURCE_OWNER_STOP = "owner_stop"
SOURCE_OWNER_REMOVE = "owner_remove"
SOURCE_STARTUP_RECOVERY = "startup_recovery"
SOURCE_STORAGE_GUARD = "storage_guard"
SOURCE_DOMAIN_PROMOTION = "primary_domain_promotion"
SOURCE_RENAME_RESTART = "rename_restart"
SOURCE_ARCHIVE_MIGRATION = "archive_migration"
SOURCE_SYSTEM = "system"

_status_source: contextvars.ContextVar[str] = contextvars.ContextVar("app_status_source", default=SOURCE_SYSTEM)


@contextlib.contextmanager
def status_source(source: str) -> Iterator[None]:
    """Bind the trigger source recorded by deep helpers in this context.

    Background worker threads set one of these at their entry point (install,
    reload, startup recovery, ...). Route handlers on the shared event loop
    pass ``source=`` explicitly instead of relying on the context variable.
    """
    token = _status_source.set(source)
    try:
        yield
    finally:
        _status_source.reset(token)


class _LeaveUnchanged:
    """Sentinel: the caller is not changing apps.error_message."""


#: Pass as ``error_message`` when the UPDATE leaves apps.error_message as-is.
LEAVE: _LeaveUnchanged = _LeaveUnchanged()


@attr.s(auto_attribs=True, frozen=True)
class StatusEvent:
    """One persisted transition, as returned by the history query."""

    seq: int
    from_status: str | None
    to_status: str
    changed_at: str
    error_message: str | None
    source: str


@attr.s(auto_attribs=True, frozen=True)
class FailureClues:
    """Repeated-failure hints derived from one returned history window.

    ``error_count`` counts transitions into 'error' inside the window.
    ``last_error_at`` is the newest such transition's time.
    ``attempts_since_last_running`` counts starting/building/error events after
    the newest 'running' event in the window (or all of them when the window has
    no running event), which exposes a restart loop.
    """

    error_count: int
    last_error_at: str | None
    attempts_since_last_running: int


@attr.s(auto_attribs=True, frozen=True)
class HistoryWindow:
    """The recent-history API payload, minus failure clues (derived by caller)."""

    current_status: str
    events: list[StatusEvent]
    limit: int
    returned: int
    #: Rows still retained for the app after pruning (may exceed ``returned``).
    retained: int
    #: True when retention pruning dropped older events (a gap exists in seq).
    truncated: bool


def _savepoint_name() -> str:
    return f"sp_app_hist_{uuid.uuid4().hex}"


def _normalize_error(value: str | None) -> str | None:
    """Treat NULL and empty string as the same (no) error."""
    return value or None


def _resolve_source(source: str | None) -> str:
    return source if source is not None else _status_source.get()


def _write_transition(
    db: sqlite3.Connection,
    app_id: str,
    from_status: str,
    to_status: str,
    error_message: str | None | _LeaveUnchanged,
    source: str,
) -> None:
    """Compare-and-record against a known previous status. Caller holds the tx."""
    if from_status == to_status:
        # Same status: a repeated check must not add a row. A changed error
        # message is folded into the most recent row instead, leaving its time
        # and seq untouched.
        if not isinstance(error_message, _LeaveUnchanged):
            new_error = _normalize_error(error_message)
            db.execute(
                "UPDATE app_status_history SET error_message = ? "
                "WHERE id = ("
                "SELECT id FROM app_status_history WHERE app_id = ? ORDER BY seq DESC LIMIT 1"
                ")",
                (new_error, app_id),
            )
        return

    history_error = None if isinstance(error_message, _LeaveUnchanged) else _normalize_error(error_message)
    row = db.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM app_status_history WHERE app_id = ?",
        (app_id,),
    ).fetchone()
    next_seq = int(row[0]) + 1
    db.execute(
        "INSERT INTO app_status_history "
        "(app_id, seq, from_status, to_status, error_message, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (app_id, next_seq, from_status, to_status, history_error, source),
    )
    cutoff = next_seq - HISTORY_LIMIT
    if cutoff > 0:
        db.execute(
            "DELETE FROM app_status_history WHERE app_id = ? AND seq <= ?",
            (app_id, cutoff),
        )


def record_transition(
    db: sqlite3.Connection,
    app_id: str,
    to_status: str,
    *,
    error_message: str | None | _LeaveUnchanged = LEAVE,
    source: str | None = None,
) -> None:
    """Record the status change the caller is about to commit. Never raises.

    Reads the current apps row to determine the previous status, so this must
    run immediately before the matching UPDATE on the same connection. When the
    status is unchanged and no new error message is supplied, nothing is
    written.
    """
    savepoint = _savepoint_name()
    try:
        db.execute(f"SAVEPOINT {savepoint}")
        row = db.execute(
            "SELECT status, error_message FROM apps WHERE app_id = ?",
            (app_id,),
        ).fetchone()
        if row is None:
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return
        resolved_source = _resolve_source(source)
        _write_transition(
            db,
            app_id,
            from_status=row["status"],
            to_status=to_status,
            error_message=error_message,
            source=resolved_source,
        )
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        with contextlib.suppress(Exception):
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        logger.exception("Failed to record status transition for app {}", app_id)


def record_known_transition(
    db: sqlite3.Connection,
    app_id: str,
    from_status: str,
    to_status: str,
    *,
    error_message: str | None = None,
    source: str,
) -> None:
    """Record a transition whose previous status was selected explicitly.

    Used by conditional UPDATE ... WHERE claim sites (remove, reload), which
    must not record when their claim changed zero rows. Call only after the
    claim succeeded. Never raises.
    """
    savepoint = _savepoint_name()
    try:
        db.execute(f"SAVEPOINT {savepoint}")
        _write_transition(db, app_id, from_status, to_status, error_message, source)
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        with contextlib.suppress(Exception):
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        logger.exception("Failed to record claimed status transition for app {}", app_id)


def record_initial(
    db: sqlite3.Connection,
    app_id: str,
    *,
    source: str = SOURCE_INSTALL,
) -> None:
    """Insert the NULL -> 'building' birth row for a newly inserted app.

    Call after the apps INSERT in the same transaction. Idempotent: an app
    that already has history gets nothing, so a replayed install step can not
    fabricate history. Never raises.
    """
    savepoint = _savepoint_name()
    try:
        db.execute(f"SAVEPOINT {savepoint}")
        existing = db.execute(
            "SELECT 1 FROM app_status_history WHERE app_id = ? LIMIT 1",
            (app_id,),
        ).fetchone()
        if existing is not None:
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return
        row = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
        if row is None:
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
            return
        db.execute(
            "INSERT INTO app_status_history (app_id, seq, from_status, to_status, error_message, source) "
            "VALUES (?, 1, NULL, ?, NULL, ?)",
            (app_id, row["status"], source),
        )
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
    except Exception:
        with contextlib.suppress(Exception):
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        logger.exception("Failed to record initial status for app {}", app_id)


def _event_from_row(row: sqlite3.Row) -> StatusEvent:
    return StatusEvent(
        seq=int(row["seq"]),
        from_status=row["from_status"],
        to_status=row["to_status"],
        changed_at=row["changed_at"],
        error_message=row["error_message"],
        source=row["source"],
    )


def get_history_window(
    db: sqlite3.Connection,
    app_id: str,
    limit: int = DEFAULT_FETCH_LIMIT,
) -> HistoryWindow | None:
    """Return the recent window for an app, oldest to newest, or None if the
    app does not exist. Read-only; never touches apps status.

    ``limit`` is clamped to [1, HISTORY_LIMIT]. ``truncated`` reflects
    retention-level pruning (a seq gap), independently of the result window.
    """
    limit = max(1, min(HISTORY_LIMIT, int(limit)))
    current = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    if current is None:
        return None
    totals = db.execute(
        "SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM app_status_history WHERE app_id = ?",
        (app_id,),
    ).fetchone()
    retained = int(totals[0])
    max_seq = int(totals[1])
    rows = db.execute(
        "SELECT seq, from_status, to_status, changed_at, error_message, source "
        "FROM app_status_history WHERE app_id = ? ORDER BY seq DESC LIMIT ?",
        (app_id, limit),
    ).fetchall()
    events = [_event_from_row(row) for row in reversed(rows)]
    # Pruning deletes the lowest seq values, so a retained set that does not
    # cover 1..MAX(seq) proves older events were dropped.
    truncated = max_seq > retained
    return HistoryWindow(
        current_status=current["status"],
        events=events,
        limit=limit,
        returned=len(events),
        retained=retained,
        truncated=truncated,
    )


def derive_failure_clues(events: list[StatusEvent]) -> FailureClues:
    """Summarize repeated-failure evidence inside one returned window."""
    error_events = [event for event in events if event.to_status == "error"]
    last_error_at = error_events[-1].changed_at if error_events else None
    last_running_index: int | None = None
    for index in range(len(events) - 1, -1, -1):
        if events[index].to_status == "running":
            last_running_index = index
            break
    tail = events[last_running_index + 1 :] if last_running_index is not None else events
    attempts = sum(1 for event in tail if event.to_status in ("starting", "building", "error"))
    return FailureClues(
        error_count=len(error_events),
        last_error_at=last_error_at,
        attempts_since_last_running=attempts,
    )
