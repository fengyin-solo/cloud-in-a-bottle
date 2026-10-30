-- v17: bounded, per-app history of real status transitions.
--
-- One row per transition where apps.status actually changed (plus the single
-- NULL -> 'building' birth row recorded at install time by the application).
-- Polling the read-only app_status endpoint never writes here. Retention is
-- enforced by the application: only the newest HISTORY_LIMIT seq values are
-- kept per app, and gaps in seq tell the API that older events were pruned.
--
-- The migration seeds nothing on purpose: an upgraded instance must show
-- empty history for its existing apps instead of a fake healthy record.

CREATE TABLE IF NOT EXISTS app_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    -- Per-app monotonic event number (1, 2, 3, ...); retained across pruning
    -- so a gap between MIN(seq) and the row count signals truncation.
    seq INTEGER NOT NULL,
    -- NULL only on the install-time birth row (NULL -> 'building').
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    error_message TEXT,
    -- Stable trigger-source identifier; see core/app_status_history.py for
    -- the vocabulary (install, owner_stop, startup_recovery, ...).
    source TEXT NOT NULL,
    FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE,
    UNIQUE(app_id, seq),
    CHECK (from_status IS NULL OR from_status IN ('building', 'starting', 'running', 'stopped', 'error', 'removing')),
    CHECK (to_status IN ('building', 'starting', 'running', 'stopped', 'error', 'removing'))
);

CREATE INDEX IF NOT EXISTS idx_app_status_history_app_seq ON app_status_history(app_id, seq);
