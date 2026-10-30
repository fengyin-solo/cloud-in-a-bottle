"""Server-render checks for the batch-action UI on the dashboard.

The interaction itself (selection, confirm, per-item results, status poll)
is exercised in the browser suite; here we pin the progressive-enhancement
contract: the batch bar and one checkbox per app render whenever apps exist,
the batch endpoint is wired into the page config, and an empty dashboard
renders neither.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar import Litestar
from litestar.di import Provide
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.template.config import TemplateConfig
from litestar.testing import TestClient

from compute_space.config import provide_config
from compute_space.db import provide_db
from compute_space.db.connection import init_db
from compute_space.web.app import _template_globals
from compute_space.web.routes.pages.apps import dashboard

from ._litestar_helpers import auth_cookie
from ._litestar_helpers import stash_zone_middleware
from .conftest import _make_test_config


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path)
    init_db(config.db_path)
    yield config


def _build_app(cfg: Any) -> Litestar:
    web_dir = Path(__file__).resolve().parents[1] / "web"
    template_config: TemplateConfig[JinjaTemplateEngine] = TemplateConfig(
        directory=web_dir / "templates",
        engine=JinjaTemplateEngine,
    )

    def _install_globals(app: Litestar) -> None:
        engine = app.template_engine
        if isinstance(engine, JinjaTemplateEngine):
            engine.engine.globals.update(_template_globals(cfg, web_dir / "static"))

    return Litestar(
        route_handlers=[dashboard],
        template_config=template_config,
        dependencies={
            "config": Provide(provide_config, sync_to_thread=False),
            "db": Provide(provide_db),
        },
        middleware=[stash_zone_middleware],
        on_startup=[_install_globals],
        openapi_config=None,
    )


def _seed_app(db_path: str, name: str, status: str = "running", port: int = 19500) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) VALUES (?, ?, '1.0', '/r', ?, ?)",
            (name, name, port, status),
        )
        conn.commit()


def test_dashboard_renders_batch_controls_for_each_app(cfg: Any) -> None:
    _seed_app(cfg.db_path, "alpha", port=19501)
    _seed_app(cfg.db_path, "beta", status="building", port=19502)
    cookie = auth_cookie(cfg)

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/dashboard")

    assert resp.status_code == 200
    html = resp.text
    assert 'id="batch-bar"' in html
    assert 'id="batch-stop"' in html and 'id="batch-reload"' in html
    assert 'id="batch-results"' in html
    assert html.count('class="app-row__select"') == 2
    assert '"/api/batch_app_action"' in html


def test_dashboard_without_apps_renders_no_batch_controls(cfg: Any) -> None:
    cookie = auth_cookie(cfg)

    with TestClient(app=_build_app(cfg)) as client:
        client.cookies.update(cookie)
        resp = client.get("/dashboard")

    assert resp.status_code == 200
    html = re.sub(r"\s+", " ", resp.text)
    assert "batch-bar" not in html
    assert "app-row__select" not in html
    assert "Deploy one now" in html
