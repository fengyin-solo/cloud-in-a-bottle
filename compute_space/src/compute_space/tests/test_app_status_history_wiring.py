"""Route-level tests that owner actions write the right status history."""

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
from compute_space.web.routes.api.apps import api_apps_routes


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


def _seed_app(db_path: str, name: str, status: str = "running") -> str:
    app_id = new_app_id()
    db = sqlite3.connect(db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
            "VALUES (?, ?, '1.0', '/r', 19500, ?)",
            (app_id, name, status),
        )
        db.commit()
    finally:
        db.close()
    return app_id


def _history(db_path: str, app_id: str) -> list[tuple[Any, ...]]:
    db = sqlite3.connect(db_path)
    try:
        return [
            tuple(r)
            for r in db.execute(
                "SELECT from_status, to_status, source FROM app_status_history WHERE app_id = ? ORDER BY seq",
                (app_id,),
            )
        ]
    finally:
        db.close()


def test_stop_records_one_transition_and_dedupes_repeated_stop(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", status="running")
    client.cookies.update(cookies)

    with (
        patch("compute_space.web.routes.api.apps.stop_app_process"),
        patch("compute_space.web.routes.api.apps.stop_container"),
    ):
        first = client.post(f"/stop_app/{app_id}")
        second = client.post(f"/stop_app/{app_id}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert _history(cfg.db_path, app_id) == [("running", "stopped", "owner_stop")]


def test_plain_reload_records_building_with_reload_source(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", status="stopped")
    client.cookies.update(cookies)

    with (
        patch("compute_space.web.routes.api.apps.Thread"),
        patch("compute_space.web.routes.api.apps.stop_app_process"),
    ):
        resp = client.post(f"/reload_app/{app_id}", json={})

    assert resp.status_code == 200
    assert _history(cfg.db_path, app_id) == [("stopped", "building", "reload")]


def test_update_reload_records_building_with_update_source(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str], tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    app_id = new_app_id()
    db = sqlite3.connect(cfg.db_path)
    try:
        db.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, repo_url, local_port, status) "
            "VALUES (?, 'myapp', '1.0', ?, 'https://github.com/owner/repo@main', 19501, 'running')",
            (app_id, str(repo)),
        )
        db.commit()
    finally:
        db.close()
    client.cookies.update(cookies)

    def _fake_pull(*_args: Any, **_kwargs: Any) -> tuple[bool, None]:
        return True, None

    with (
        patch("compute_space.web.routes.api.apps.Thread"),
        patch("compute_space.web.routes.api.apps.stop_app_process"),
        patch("compute_space.web.routes.api.apps.git_pull", side_effect=_fake_pull),
        patch("compute_space.web.routes.api.apps._pin_refless_to_landed_branch", return_value=None),
    ):
        resp = client.post(f"/reload_app/{app_id}", json={"update": True})

    assert resp.status_code == 200
    assert _history(cfg.db_path, app_id) == [("running", "building", "update")]


def test_refused_reload_records_nothing(cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]) -> None:
    app_id = _seed_app(cfg.db_path, "busyapp", status="building")
    client.cookies.update(cookies)

    with (
        patch("compute_space.web.routes.api.apps.Thread") as Thread,
        patch("compute_space.web.routes.api.apps.stop_app_process") as stop,
    ):
        resp = client.post(f"/reload_app/{app_id}", json={})

    assert resp.status_code == 409
    Thread.assert_not_called()
    stop.assert_not_called()
    assert _history(cfg.db_path, app_id) == []


def test_remove_claim_records_removing_transition(
    cfg: Any, client: TestClient[Litestar], cookies: dict[str, str]
) -> None:
    app_id = _seed_app(cfg.db_path, "myapp", status="running")
    client.cookies.update(cookies)

    # Mock the worker thread: the row stays in 'removing', letting us assert on
    # the history the claim transaction wrote.
    with patch("compute_space.web.routes.api.apps.Thread"):
        resp = client.post(f"/remove_app/{app_id}")

    assert resp.status_code == 202
    assert _history(cfg.db_path, app_id) == [("running", "removing", "owner_remove")]
