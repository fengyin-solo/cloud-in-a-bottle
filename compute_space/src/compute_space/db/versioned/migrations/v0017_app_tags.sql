-- v17: per-app tags for browsing/filtering the app list.
--
-- One normalized tag per row. ``position`` preserves the owner-supplied
-- order after normalization (duplicates collapse to their first occurrence),
-- so list/detail rendering and tag replacement are both deterministic.
-- Existing apps get no rows, i.e. an empty tag list. The FK cascades with
-- ON DELETE CASCADE, so removing an app removes its tags in the same DELETE.
CREATE TABLE IF NOT EXISTS app_tags (
    app_id   TEXT NOT NULL,
    tag      TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (app_id, tag),
    FOREIGN KEY (app_id) REFERENCES apps(app_id) ON DELETE CASCADE
);

-- Drives the dashboard's tag filter (lookups by tag value) without scanning
-- the table as the tag vocabulary grows.
CREATE INDEX IF NOT EXISTS idx_app_tags_tag ON app_tags(tag);
