"""Tests for the owner-only batch stop/reload endpoint.

``POST /api/batch_app_action`` runs the same lifecycle logic as the
single-app ``/stop_app/<id>`` and ``/reload_app/<id>`` routes, but reports
each app's independent outcome (accepted / skipped / error) in request
order. These tests pin:

* owner auth is required (same guard as the single-app routes);
* malformed envelopes (unknown action, empty / over-long / mistyped id
  list) are rejected wholesale with 400 before any app is touched;
* per-item failures don't roll back or block accepted entries, and the
  batch-level ``ok`` only reflects all-accepted;
* repeated app_ids are de-duplicated and result order follows the request;
* reload is the plain restart-from-disk action: a rebuild is claimed
  (status flips to 'building', worker spawned) and conflicted transient
  states are skipped.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from litestar import Litestar
from litestar.testing import TestClient

from compute_space.core.app_id import new_app_id
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api import apps as apps_api
from compute_space.web.routes.api.apps import MAX_BATCH_APPS
from compute_space.web.routes.api.apps import api_apps_routes

BATCH_URL = "/api/batch_app_action"


@pytest.fixture
def cfg(tmp_path: Path) -> Any:
    cfg = _make_test_config(tmp_path)
    init_db(cfg.db_path)
    return cfg


@pytest.fixture
def client(cfg: Any) -> Iterator[TestClient[Litestar]]:
    with TestClient(app=make_test_app(api_apps_routes)) as c:
        yield c


@pytest.fixture
def cookies(cfg: Any) -> dict[str, str]:
    return auth_cookie(cfg)


_seed_port = 19500


def _seed_app(db_path: str, name: str, status: str = "running") -> str:
    global _seed_port
    _seed_port += 1
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) VALUES (?, ?, '1.0', '/r', ?, ?)",
            (app_id, name, _seed_port, status),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _status(db_path: str, app_id: str) -> str:
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("SELECT status FROM apps WHERE app_id = ?", (app_id,)).fetchone()
    finally:
        db.close()
    assert row is not None
    return str(row[0])


def _batch(client: TestClient[Litestar], cookies: dict[str, str], action: str, app_ids: list[str]) -> Any:
    client.cookies.update(cookies)
    return client.post(BATCH_URL, json={"action": action, "app_ids": app_ids})


# ─── auth & envelope validation ────────────────────────────────────────────


def test_batch_requires_owner_auth(client: TestClient[Litestar]) -> None:
    resp = client.post(BATCH_URL, json={"action": "stop", "app_ids": [new_app_id()]})
    assert resp.status_code == 401


def test_batch_unknown_action_400(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = new_app_id()
    resp = _batch(client, cookies, "explode", [app_id])
    assert resp.status_code == 400
    assert "unknown action" in resp.json()["detail"].lower()


def test_batch_empty_list_400(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = _batch(client, cookies, "stop", [])
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


def test_batch_missing_body_400(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    client.cookies.update(cookies)
    resp = client.post(BATCH_URL)
    assert resp.status_code == 400


def test_batch_over_limit_400(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    too_many = [new_app_id() for _ in range(MAX_BATCH_APPS + 1)]
    resp = _batch(client, cookies, "stop", too_many)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert str(MAX_BATCH_APPS) in detail
    assert str(MAX_BATCH_APPS + 1) in detail


def test_batch_app_ids_must_be_string_list_400(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    resp = _batch(client, cookies, "stop", [12345])  # type: ignore[list-item]
    assert resp.status_code == 400


# ─── stop ──────────────────────────────────────────────────────────────────


def test_batch_stop_accepts_each_app_and_commits(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_a = _seed_app(cfg.db_path, "app-a")
    app_b = _seed_app(cfg.db_path, "app-b")

    with patch("compute_space.web.routes.api.apps.stop_container") as stop_container:
        resp = _batch(client, cookies, "stop", [app_a, app_b])

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["action"] == "stop"
    assert body["accepted"] == 2 and body["skipped"] == 0 and body["errors"] == 0
    assert [r["app_id"] for r in body["results"]] == [app_a, app_b]
    assert all(r["result"] == "accepted" for r in body["results"])
    assert stop_container.call_count == 2
    assert _status(cfg.db_path, app_a) == "stopped"
    assert _status(cfg.db_path, app_b) == "stopped"


def test_batch_stop_skipped_while_removing(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "going-away", status="removing")

    with patch("compute_space.web.routes.api.apps.stop_container") as stop_container:
        resp = _batch(client, cookies, "stop", [app_id])

    body = resp.json()
    assert body["ok"] is False and body["accepted"] == 0 and body["skipped"] == 1
    result = body["results"][0]
    assert result["result"] == "skipped"
    assert result["code"] == "removing"
    assert "removed" in (result["reason"] or "").lower()
    stop_container.assert_not_called()
    assert _status(cfg.db_path, app_id) == "removing"


def test_batch_stop_accepts_already_stopped_app(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    # Parity with /stop_app/<id>: stopping a settled app is always accepted.
    app_id = _seed_app(cfg.db_path, "stopped-app", status="stopped")

    with patch("compute_space.web.routes.api.apps.stop_container"):
        resp = _batch(client, cookies, "stop", [app_id])

    assert resp.json()["results"][0]["result"] == "accepted"
    assert _status(cfg.db_path, app_id) == "stopped"


# ─── reload ────────────────────────────────────────────────────────────────


def test_batch_reload_claims_building_and_spawns_worker(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, "myapp")

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_app_process"),
    ):
        resp = _batch(client, cookies, "reload", [app_id])

    body = resp.json()
    assert body["ok"] is True
    assert body["results"][0]["result"] == "accepted"
    Thread.assert_called_once()
    Thread.return_value.start.assert_called_once()
    assert _status(cfg.db_path, app_id) == "building"


@pytest.mark.parametrize("busy_status", ["building", "starting"])
def test_batch_reload_skipped_while_transient(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], busy_status: str
) -> None:
    app_id = _seed_app(cfg.db_path, "busy", status=busy_status)

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
    ):
        resp = _batch(client, cookies, "reload", [app_id])

    result = resp.json()["results"][0]
    assert result["result"] == "skipped"
    assert result["code"] == busy_status
    Thread.assert_not_called()
    stop.assert_not_called()
    assert _status(cfg.db_path, app_id) == busy_status


def test_batch_reload_skipped_while_removing(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "going-away", status="removing")

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
    ):
        resp = _batch(client, cookies, "reload", [app_id])

    result = resp.json()["results"][0]
    assert result["result"] == "skipped"
    assert result["code"] == "removing"
    Thread.assert_not_called()
    stop.assert_not_called()
    assert _status(cfg.db_path, app_id) == "removing"


# ─── per-item failures, de-duplication, ordering ───────────────────────────


def test_batch_invalid_id_is_per_item_error_others_still_apply(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    good = _seed_app(cfg.db_path, "good")

    with patch("compute_space.web.routes.api.apps.stop_container"):
        resp = _batch(client, cookies, "stop", ["not-an-id!", good])

    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is False
    assert body["accepted"] == 1 and body["errors"] == 1
    invalid, accepted = body["results"]
    assert invalid["app_id"] == "not-an-id!"
    assert invalid["result"] == "error" and invalid["code"] == "invalid_id"
    assert accepted["app_id"] == good and accepted["result"] == "accepted"
    assert _status(cfg.db_path, good) == "stopped"


def test_batch_unknown_app_is_per_item_error(client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    missing = new_app_id()
    resp = _batch(client, cookies, "stop", [missing])

    result = resp.json()["results"][0]
    assert result["result"] == "error"
    assert result["code"] == "not_found"


def test_batch_partial_failure_never_rolls_back(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """A mixed reload batch: one claim succeeds, one app is missing, one is
    already building. The accepted rebuild stays claimed (no rollback), the
    batch is not reported as a whole success, and results follow request
    order."""
    runnable = _seed_app(cfg.db_path, "runnable")
    missing = new_app_id()
    busy = _seed_app(cfg.db_path, "busy", status="building")

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_app_process"),
    ):
        resp = _batch(client, cookies, "reload", [runnable, missing, busy])

    body = resp.json()
    assert body["ok"] is False
    assert body["accepted"] == 1 and body["errors"] == 1 and body["skipped"] == 1
    assert [r["app_id"] for r in body["results"]] == [runnable, missing, busy]
    assert [r["result"] for r in body["results"]] == ["accepted", "error", "skipped"]
    # The accepted rebuild was not undone by the later failures.
    assert _status(cfg.db_path, runnable) == "building"
    assert _status(cfg.db_path, busy) == "building"
    Thread.assert_called_once()


def test_batch_all_entries_fail_still_200_with_clear_results(
    client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    resp = _batch(client, cookies, "stop", [new_app_id(), "bad-id"])

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["accepted"] == 0 and body["errors"] == 2
    assert {r["code"] for r in body["results"]} == {"not_found", "invalid_id"}


def test_batch_duplicate_ids_acted_on_once(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "twice")

    with patch("compute_space.web.routes.api.apps.stop_container") as stop_container:
        resp = _batch(client, cookies, "stop", [app_id, app_id])

    body = resp.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["app_id"] == app_id
    assert body["results"][0]["result"] == "accepted"
    stop_container.assert_called_once()


def test_batch_results_follow_request_order(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_a = _seed_app(cfg.db_path, "aaa")
    app_b = _seed_app(cfg.db_path, "bbb")
    app_c = _seed_app(cfg.db_path, "ccc")
    ordered = [app_c, app_a, app_b]

    with patch("compute_space.web.routes.api.apps.stop_container"):
        resp = _batch(client, cookies, "stop", ordered)

    assert [r["app_id"] for r in resp.json()["results"]] == ordered


def test_batch_unexpected_single_app_failure_is_isolated(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    """If the shared stop helper raises unexpectedly for one app, that entry
    is an 'error' with a reason and the next app still gets stopped."""
    boom = _seed_app(cfg.db_path, "boom")
    fine = _seed_app(cfg.db_path, "fine")

    real_stop = apps_api._stop_app_impl

    def _side_effect(app_id: str, db: sqlite3.Connection) -> None:
        if app_id == boom:
            raise RuntimeError("podman exploded")
        real_stop(app_id, db)

    with (
        patch("compute_space.web.routes.api.apps._stop_app_impl", side_effect=_side_effect),
        patch("compute_space.web.routes.api.apps.stop_container"),
    ):
        resp = _batch(client, cookies, "stop", [boom, fine])

    body = resp.json()
    assert body["accepted"] == 1 and body["errors"] == 1
    errored, accepted = body["results"]
    assert errored["app_id"] == boom and errored["code"] == "error"
    assert "podman exploded" in (errored["reason"] or "")
    assert accepted["app_id"] == fine
    assert _status(cfg.db_path, fine) == "stopped"
