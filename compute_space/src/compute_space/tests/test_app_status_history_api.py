"""Tests for GET /api/app_status_history/<app_id>."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import api_apps_routes


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path, port=20120)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


def _seed_app(db_path: str, status: str = "running") -> str:
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, 'notes', '1.0', '/repo/notes', 20121, ?)",
            (app_id, status),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _add_event(
    db_path: str,
    app_id: str,
    seq: int,
    from_status: str | None,
    to_status: str,
    changed_at: str,
    *,
    error_message: str | None = None,
    source: str = "startup_recovery",
) -> None:
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO app_status_history (app_id, seq, from_status, to_status, changed_at, error_message, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (app_id, seq, from_status, to_status, changed_at, error_message, source),
        )
        db.commit()
    finally:
        db.close()


def test_history_requires_auth(client: TestClient[Litestar]) -> None:
    resp = client.get(f"/api/app_status_history/{new_app_id()}")
    assert resp.status_code == 401


def test_history_400_for_invalid_app_id(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    client.cookies.update(cookies)
    resp = client.get("/api/app_status_history/bad-app-id!")
    assert resp.status_code == 400


def test_history_404_for_unknown_app(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    client.cookies.update(cookies)
    resp = client.get(f"/api/app_status_history/{new_app_id()}")
    assert resp.status_code == 404


def test_old_app_has_empty_history_but_current_status(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, status="running")  # migrated-style: no history rows
    client.cookies.update(cookies)
    resp = client.get(f"/api/app_status_history/{app_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload == {
        "app_id": app_id,
        "current_status": "running",
        "limit": 20,
        "returned": 0,
        "retained": 0,
        "truncated": False,
        "events": [],
        "failure_clues": {"error_count": 0, "last_error_at": None, "attempts_since_last_running": 0},
    }


def test_history_returns_events_oldest_first_with_failure_clues(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, status="error")
    _add_event(cfg.db_path, app_id, 1, None, "building", "2026-01-01 00:00:00", source="install")
    _add_event(cfg.db_path, app_id, 2, "building", "error", "2026-01-01 00:01:00", error_message="boom one")
    _add_event(cfg.db_path, app_id, 3, "error", "starting", "2026-01-01 00:02:00")
    _add_event(cfg.db_path, app_id, 4, "starting", "error", "2026-01-01 00:03:00", error_message="boom two")
    client.cookies.update(cookies)

    resp = client.get(f"/api/app_status_history/{app_id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["current_status"] == "error"
    assert payload["returned"] == 4
    assert payload["retained"] == 4
    assert payload["truncated"] is False
    assert [(e["from_status"], e["to_status"]) for e in payload["events"]] == [
        (None, "building"),
        ("building", "error"),
        ("error", "starting"),
        ("starting", "error"),
    ]
    assert set(payload["events"][0]) == {"from_status", "to_status", "changed_at", "error_message", "source"}
    assert payload["events"][0]["source"] == "install"
    assert payload["events"][1]["error_message"] == "boom one"
    assert payload["failure_clues"] == {
        "error_count": 2,
        "last_error_at": "2026-01-01 00:03:00",
        # No running event in the window, so every building/starting/error event
        # counts toward the restart-loop hint.
        "attempts_since_last_running": 4,
    }


@pytest.mark.parametrize(
    ("query_limit", "applied_limit", "expected_days"),
    [("0", 1, ["04"]), ("999", 50, ["01", "02", "03", "04"]), ("2", 2, ["03", "04"])],
)
def test_history_limit_is_clamped_and_window_is_newest(
    cfg: Any,
    client: TestClient[Litestar],
    cookies: dict[str, str],
    query_limit: str,
    applied_limit: int,
    expected_days: list[str],
) -> None:
    app_id = _seed_app(cfg.db_path)
    for seq in range(1, 5):
        _add_event(
            cfg.db_path,
            app_id,
            seq,
            "stopped" if seq == 1 else "running",
            "running",
            f"2026-01-{seq:02d} 00:00:00",
        )
    client.cookies.update(cookies)

    resp = client.get(f"/api/app_status_history/{app_id}?limit={query_limit}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["limit"] == applied_limit
    # Window is the newest N events, still served oldest to newest.
    assert [e["changed_at"][8:10] for e in payload["events"]] == expected_days


def test_history_truncated_reflects_retention_gap_not_window(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path)
    # Simulate post-pruning state: 55 transitions happened, only seq 6..55 remain.
    for seq in range(6, 56):
        _add_event(
            cfg.db_path,
            app_id,
            seq,
            "running",
            "stopped" if seq % 2 else "running",
            f"2026-02-0{seq % 9} 00:00:0{seq % 10}",
        )
    client.cookies.update(cookies)

    resp = client.get(f"/api/app_status_history/{app_id}?limit=20")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["returned"] == 20
    assert payload["retained"] == 50
    assert payload["truncated"] is True
