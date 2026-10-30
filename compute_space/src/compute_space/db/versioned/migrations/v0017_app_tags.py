"""v17: add the ``app_tags`` table.  Body in v0017_app_tags.sql."""

from __future__ import annotations

from compute_space.db.versioned.base import SqlFileMigration


class Migration0017AppTags(SqlFileMigration):
    version = 17
    sql_file = "v0017_app_tags.sql"
