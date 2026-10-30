"""v17: per-app bounded status transition history.  Body in v0017_app_status_history.sql.

The migration only creates the table and its index; it deliberately seeds no
rows, so apps that predate the feature keep an empty history rather than
inheriting a fabricated healthy event.
"""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0017AppStatusHistory(SqlFileMigration):
    version = 17
    sql_file = "v0017_app_status_history.sql"
