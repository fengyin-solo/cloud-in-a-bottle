"""Owner-assigned app tags: normalization rules and ``app_tags`` table access.

Tags let the owner group apps beyond name/status browsing (e.g.
``productivity``, ``media``, ``internal``, ``personal``).  Every tag stored
in the DB passes through :func:`normalize_tags`, so the same input spelled
differently (" Media ", "MEDIA", "media" repeated) always collapses to one
canonical row.

The table is append-only metadata: replacing tags only ever DELETEs/INSERTs
rows in ``app_tags`` — it never touches ``apps`` columns (status, port,
container_id, manifest-derived fields) or ``app_id``.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

# Explicit limits surfaced to API clients in 400 messages and to the
# detail-page edit UI via page-config. Kept small on purpose: tags are a
# filtering aid, not free-form notes.
MAX_TAGS_PER_APP = 10
MAX_TAG_LENGTH = 32

# Slug-like grammar: lowercase ASCII alphanumerics with interior hyphens or
# underscores. Lowercase-only is deliberate — case is normalized away, so
# allowing capitals would just create confusing rejected/accepted pairs.
# Tags also stay safe unencoded in ?tag= query params.
_VALID_TAG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")


class TagValidationError(ValueError):
    """A submitted tag set violates the normalization limits/grammar.

    Subclasses ValueError so callers that already catch ValueError keep
    working; routes map it to a 400.
    """


def normalize_tags(raw_tags: object) -> list[str]:
    """Normalize a submitted tag list to its canonical stored form.

    Rules, in order:

    * input must be a list of strings;
    * leading/trailing whitespace is stripped from every entry;
    * entries that are empty after stripping are dropped;
    * case is folded to lowercase;
    * each tag must be 1..MAX_TAG_LENGTH chars of ``[a-z0-9_-]``
      (starting and ending alphanumeric);
    * duplicates (after the above) keep their first occurrence only;
    * the deduped result holds at most ``MAX_TAGS_PER_APP`` tags.

    Raises :class:`TagValidationError` on any violation.
    """
    if not isinstance(raw_tags, list):
        raise TagValidationError("tags must be a list of strings")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_tags:
        if not isinstance(raw, str):
            raise TagValidationError("each tag must be a string")
        tag = raw.strip().lower()
        if not tag:
            # Whitespace-only entries carry no filtering value; dropping
            # them lets the UI submit a trailing comma/blank harmlessly.
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise TagValidationError(
                f"tag {tag!r} is longer than {MAX_TAG_LENGTH} characters"
            )
        if not _VALID_TAG_RE.fullmatch(tag):
            raise TagValidationError(
                f"tag {tag!r} is invalid: use lowercase letters, numbers, hyphens and underscores"
            )
        if tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)

    if len(normalized) > MAX_TAGS_PER_APP:
        raise TagValidationError(f"an app can have at most {MAX_TAGS_PER_APP} tags")
    return normalized


def normalize_tag_filter(raw_tags: Iterable[str] | None) -> list[str]:
    """Lenient normalization for read-side ``?tag=`` filter values.

    Mirrors the stored canonical form (strip, lowercase, dedupe, drop
    empties) but never raises: a value that can't name a stored tag simply
    matches nothing, so an unknown/invalid filter tag yields an empty list
    rather than a server error.
    """
    if not raw_tags:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_tags:
        if not isinstance(raw, str):
            continue
        tag = raw.strip().lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def get_tags(app_id: str, db: sqlite3.Connection) -> list[str]:
    """Return one app's tags in stored (owner-supplied) order."""
    rows = db.execute(
        "SELECT tag FROM app_tags WHERE app_id = ? ORDER BY position",
        (app_id,),
    ).fetchall()
    return [row["tag"] for row in rows]


def get_tags_map(app_ids: Iterable[str], db: sqlite3.Connection) -> dict[str, list[str]]:
    """Batch variant of :func:`get_tags` for list rendering.

    Apps with no tags are absent from the result; callers default them to
    ``[]``. Returned lists are ordered per-app by position.
    """
    ids = list(app_ids)
    if not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT app_id, tag FROM app_tags WHERE app_id IN ({placeholders}) ORDER BY app_id, position",
        tuple(ids),
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(row["app_id"], []).append(row["tag"])
    return result


def all_tags(db: sqlite3.Connection) -> list[str]:
    """The distinct tag vocabulary across all apps, alphabetically ordered.

    Drives the dashboard's filter chips; independent of any one app's
    position order so the chip row is stable.
    """
    rows = db.execute("SELECT DISTINCT tag FROM app_tags ORDER BY tag").fetchall()
    return [row["tag"] for row in rows]


def replace_tags(app_id: str, tags: list[str], db: sqlite3.Connection) -> None:
    """Replace an app's whole tag set with an already-normalized list.

    Caller validates via :func:`normalize_tags` and owns the commit. Only
    ``app_tags`` rows are touched: the apps row (identity, status, ports,
    container, manifest columns) is never read-modify-written here.
    """
    db.execute("DELETE FROM app_tags WHERE app_id = ?", (app_id,))
    db.executemany(
        "INSERT INTO app_tags (app_id, tag, position) VALUES (?, ?, ?)",
        [(app_id, tag, position) for position, tag in enumerate(tags)],
    )


def find_app_ids_by_tags(tags: list[str], db: sqlite3.Connection) -> list[str]:
    """Return app_ids carrying *all* of ``tags`` (AND over multiple filters).

    Order is deterministic and independent of the order the filter tags were
    supplied in: apps sort by name, then app_id as a tiebreaker ("stable
    sorting" for filtered views). An unknown tag matches no rows, so the
    caller naturally serves an empty list instead of erroring.
    """
    if not tags:
        return []
    placeholders = ", ".join("?" for _ in tags)
    rows = db.execute(
        f"""
        SELECT a.app_id
        FROM apps a
        JOIN app_tags t ON t.app_id = a.app_id
        WHERE t.tag IN ({placeholders})
        GROUP BY a.app_id, a.name
        HAVING COUNT(DISTINCT t.tag) = ?
        ORDER BY a.name, a.app_id
        """,
        (*tags, len(set(tags))),
    ).fetchall()
    return [row["app_id"] for row in rows]
