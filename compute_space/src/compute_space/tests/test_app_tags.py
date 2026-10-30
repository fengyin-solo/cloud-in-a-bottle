"""Tests for per-app tags.

Covers:
* normalization rules (trim, lowercase, dedupe, count/length/grammar limits);
* app_tags storage helpers and multi-tag filtering semantics;
* owner-authenticated GET/PUT ``/api/app_tags/{app_id}``;
* the ``tags`` field and ``?tag=`` filtering on ``GET /api/apps``;
* tag replacement not touching app identity/status/runtime columns;
* ON DELETE CASCADE cleanup.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from litestar.testing import TestClient

from compute_space.core.app_id import new_app_id
from compute_space.core.app_tags import MAX_TAGS_PER_APP
from compute_space.core.app_tags import MAX_TAG_LENGTH
from compute_space.core.app_tags import TagValidationError
from compute_space.core.app_tags import find_app_ids_by_tags
from compute_space.core.app_tags import get_tags
from compute_space.core.app_tags import get_tags_map
from compute_space.core.app_tags import normalize_tags
from compute_space.core.app_tags import replace_tags
from compute_space.db.connection import init_db
from compute_space.tests._litestar_helpers import auth_cookie
from compute_space.tests._litestar_helpers import make_test_app
from compute_space.tests.conftest import _make_test_config
from compute_space.web.routes.api.apps import api_apps_routes

# ─── normalization ────────────────────────────────────────────────────────


def test_normalize_trims_lowercases_and_dedupes_keeping_first_position() -> None:
    assert normalize_tags([" Media ", "PRODUCTIVITY", "media", "  ", "", "internal"]) == [
        "media",
        "productivity",
        "internal",
    ]


@pytest.mark.parametrize(
    "tag",
    ["-lead", "trail-", "has space", "has.dot", "slash/x", "émoji"],
)
def test_normalize_rejects_invalid_grammar(tag: str) -> None:
    with pytest.raises(TagValidationError):
        normalize_tags([tag])


@pytest.mark.parametrize("tag", ["a", "abc_def-1", "x" * MAX_TAG_LENGTH, "0", "a_b"])
def test_normalize_accepts_allowed_shapes(tag: str) -> None:
    assert normalize_tags([tag]) == [tag]


def test_normalize_rejects_overlong_tag() -> None:
    with pytest.raises(TagValidationError, match="longer than"):
        normalize_tags(["x" * (MAX_TAG_LENGTH + 1)])


def test_normalize_rejects_too_many_tags() -> None:
    tags = [f"tag{i:02d}" for i in range(MAX_TAGS_PER_APP + 1)]
    assert len(tags) == MAX_TAGS_PER_APP + 1
    with pytest.raises(TagValidationError, match="at most"):
        normalize_tags(tags)
    # The boundary itself is accepted.
    assert len(normalize_tags(tags[:-1])) == MAX_TAGS_PER_APP


def test_normalize_drops_blank_entries_without_counting_them() -> None:
    blanks = ["", "   ", "\t"] * (MAX_TAGS_PER_APP + 2)
    assert normalize_tags(blanks) == []


def test_normalize_rejects_non_list_and_non_string_entries() -> None:
    with pytest.raises(TagValidationError):
        normalize_tags("media")
    with pytest.raises(TagValidationError):
        normalize_tags(["ok", 5])


# ─── storage helpers (in-memory production schema) ────────────────────────


def _insert_app(db: sqlite3.Connection, name: str, port: int) -> str:
    app_id = new_app_id()
    db.execute(
        "INSERT INTO apps (app_id, name, version, repo_path, local_port, status) "
        "VALUES (?, ?, '1.0', ?, ?, 'stopped')",
        (app_id, name, f"/tmp/repo/{name}", port),
    )
    return app_id


def test_replace_and_get_tags_preserves_order(db: sqlite3.Connection) -> None:
    app_id = _insert_app(db, "one", 19001)
    assert get_tags(app_id, db) == []

    replace_tags(app_id, ["media", "personal"], db)
    assert get_tags(app_id, db) == ["media", "personal"]

    # Replacement is whole-set, not append.
    replace_tags(app_id, ["internal"], db)
    assert get_tags(app_id, db) == ["internal"]


def test_get_tags_map_batches_apps(db: sqlite3.Connection) -> None:
    a = _insert_app(db, "alpha", 19001)
    b = _insert_app(db, "beta", 19002)
    replace_tags(a, ["media"], db)
    result = get_tags_map([a, b], db)
    assert result == {a: ["media"]}
    assert get_tags_map([], db) == {}


def test_find_app_ids_requires_all_tags_and_is_stable(db: sqlite3.Connection) -> None:
    aardvark = _insert_app(db, "aardvark", 19001)
    middle = _insert_app(db, "middle", 19002)
    zebra = _insert_app(db, "zebra", 19003)
    replace_tags(aardvark, ["media", "personal"], db)
    replace_tags(middle, ["media", "internal"], db)
    replace_tags(zebra, ["media"], db)

    # Name ordering regardless of insertion order.
    assert find_app_ids_by_tags(["media"], db) == [aardvark, middle, zebra]
    # AND semantics.
    assert find_app_ids_by_tags(["media", "personal"], db) == [aardvark]
    # Order of the filter tags in the request must not change the result.
    assert find_app_ids_by_tags(["personal", "media"], db) == [aardvark]
    # Unknown tag -> empty list, not an error.
    assert find_app_ids_by_tags(["nonexistent"], db) == []
    assert find_app_ids_by_tags(["media", "nonexistent"], db) == []
    assert find_app_ids_by_tags([], db) == []


# ─── API ──────────────────────────────────────────────────────────────────


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[Any]:
    config = _make_test_config(tmp_path)
    init_db(config.db_path)
    yield config


def _seed_app(cfg: Any, name: str, port: int = 19001, **columns: object) -> str:
    app_id = new_app_id()
    cols = ["app_id", "name", "version", "repo_path", "local_port", "status"]
    vals: list[object] = [app_id, name, "1.0", f"/tmp/repo/{name}", port, "stopped"]
    for key, value in columns.items():
        cols.append(key)
        vals.append(value)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            f"INSERT INTO apps ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            tuple(vals),
        )
        conn.commit()
    finally:
        conn.close()
    return app_id


def _auth_client(cfg: Any) -> TestClient:
    client = TestClient(app=make_test_app(api_apps_routes))
    client.cookies.update(auth_cookie(cfg))
    return client


def test_get_and_put_tags_require_owner_auth(cfg: Any) -> None:
    # The minimal route-level app raises NotAuthorizedException straight
    # from the guard (Litestar's default 401); the production
    # GET->login-redirect wrapping is covered by test_auth_required_response.
    app_id = _seed_app(cfg, "noauth-app")
    with TestClient(app=make_test_app(api_apps_routes)) as client:
        assert client.get(f"/api/app_tags/{app_id}").status_code == 401
        assert client.put(f"/api/app_tags/{app_id}", json={"tags": ["media"]}).status_code == 401


def test_get_tags_unknown_and_invalid_app_id(cfg: Any) -> None:
    with _auth_client(cfg) as client:
        missing = new_app_id()
        assert client.get(f"/api/app_tags/{missing}").status_code == 404
        assert client.get("/api/app_tags/not-a-valid-id").status_code == 400


def test_put_then_get_roundtrip_normalizes(cfg: Any) -> None:
    app_id = _seed_app(cfg, "tagged-app")
    with _auth_client(cfg) as client:
        resp = client.put(
            f"/api/app_tags/{app_id}",
            json={"tags": [" Media ", "PRODUCTIVITY", "media", "personal "]},
        )
        assert resp.status_code == 200
        assert resp.json() == {"app_id": app_id, "tags": ["media", "productivity", "personal"]}

        got = client.get(f"/api/app_tags/{app_id}")
        assert got.status_code == 200
        assert got.json()["tags"] == ["media", "productivity", "personal"]

    # The canonical rows landed in the DB in the same order.
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert get_tags(app_id, conn) == ["media", "productivity", "personal"]
    finally:
        conn.close()


def test_put_tags_validation_error_leaves_existing_tags(cfg: Any) -> None:
    app_id = _seed_app(cfg, "tagged-app")
    with _auth_client(cfg) as client:
        ok = client.put(f"/api/app_tags/{app_id}", json={"tags": ["media"]})
        assert ok.status_code == 200

        bad = client.put(f"/api/app_tags/{app_id}", json={"tags": ["bad tag!"]})
        assert bad.status_code == 400

        too_many = client.put(
            f"/api/app_tags/{app_id}",
            json={"tags": [f"t{i}" for i in range(MAX_TAGS_PER_APP + 1)]},
        )
        assert too_many.status_code == 400

        # Prior set survives both rejected replacements.
        assert client.get(f"/api/app_tags/{app_id}").json()["tags"] == ["media"]


def test_put_tags_unknown_app_is_404(cfg: Any) -> None:
    with _auth_client(cfg) as client:
        resp = client.put(f"/api/app_tags/{new_app_id()}", json={"tags": ["media"]})
        assert resp.status_code == 404


def test_put_tags_does_not_touch_app_row(cfg: Any) -> None:
    app_id = _seed_app(
        cfg,
        "runtime-app",
        status="running",
        container_id="abc123container",
        manifest_name="Runtime App",
        version="9.9.9",
        memory_mb=512,
    )
    protected_cols = (
        "app_id",
        "name",
        "status",
        "error_message",
        "local_port",
        "container_port",
        "container_id",
        "manifest_name",
        "version",
        "memory_mb",
        "cpu_cores",
        "public_paths",
        "links",
        "manifest_raw",
        "repo_path",
        "repo_url",
    )
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    before = dict(conn.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone())
    conn.close()

    with _auth_client(cfg) as client:
        resp = client.put(f"/api/app_tags/{app_id}", json={"tags": ["internal"]})
        assert resp.status_code == 200

    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    after = dict(conn.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone())
    conn.close()
    for col in protected_cols:
        assert after[col] == before[col], f"column {col} changed while editing tags"


def test_api_apps_carries_tags_and_preserves_base_fields(cfg: Any) -> None:
    app_id = _seed_app(cfg, "listed-app")
    with _auth_client(cfg) as client:
        client.put(f"/api/app_tags/{app_id}", json={"tags": ["media"]})
        resp = client.get("/api/apps")
        assert resp.status_code == 200
        (entry,) = resp.json()
        # The original four fields survive untouched (same names, same order).
        assert list(entry)[:4] == ["app_id", "name", "status", "error_message"]
        assert entry["app_id"] == app_id
        assert entry["name"] == "listed-app"
        assert entry["status"] == "stopped"
        assert "error_message" in entry
        assert entry["tags"] == ["media"]


def test_api_apps_tag_filters(cfg: Any) -> None:
    a = _seed_app(cfg, "aardvark", port=19001)
    m = _seed_app(cfg, "middle", port=19002)
    z = _seed_app(cfg, "zebra", port=19003)
    with _auth_client(cfg) as client:
        client.put(f"/api/app_tags/{a}", json={"tags": ["media", "personal"]})
        client.put(f"/api/app_tags/{m}", json={"tags": ["media", "internal"]})
        client.put(f"/api/app_tags/{z}", json={"tags": ["media"]})

        def names(**params: object) -> list[str]:
            return [a["name"] for a in client.get("/api/apps", params=params).json()]

        # Stable name order.
        assert names(tag="media") == ["aardvark", "middle", "zebra"]
        # Multi-tag AND via repeated params (requests repeats list values).
        assert names(tag=["media", "personal"]) == ["aardvark"]
        # The order filter tags arrive in must not change the result.
        assert names(tag=["personal", "media"]) == ["aardvark"]
        # Query values are normalized like stored tags (trim + case).
        assert names(tag=" Media ") == ["aardvark", "middle", "zebra"]
        assert names(tag="MEDIA") == ["aardvark", "middle", "zebra"]
        # Unknown tag -> empty 200 list, never a server error.
        resp = client.get("/api/apps", params={"tag": "does-not-exist"})
        assert resp.status_code == 200
        assert resp.json() == []
        assert names(tag=["media", "does-not-exist"]) == []


def test_deleting_app_cascades_its_tags(cfg: Any) -> None:
    app_id = _seed_app(cfg, "doomed-app")
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        replace_tags(app_id, ["media", "personal"], conn)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM app_tags").fetchone()[0] == 2

        conn.execute("DELETE FROM apps WHERE app_id = ?", (app_id,))
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM app_tags WHERE app_id = ?", (app_id,)).fetchone()[0] == 0
    finally:
        conn.close()
