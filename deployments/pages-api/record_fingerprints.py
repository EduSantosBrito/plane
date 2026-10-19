"""Hash existing workflow rows without exporting their contents or credentials."""

import hashlib
import json
import os

import psycopg
from psycopg import sql

TABLES = (
    "users",
    "workspaces",
    "workspace_members",
    "projects",
    "project_members",
    "issues",
    "issue_comments",
    "issue_relations",
    "pages",
    "page_versions",
    "project_pages",
    "file_assets",
    "cycles",
    "modules",
    "labels",
    "states",
)

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    result = {}
    for table in TABLES:
        with conn.cursor() as cursor:
            cursor.execute(
                sql.SQL("SELECT id::text, to_jsonb(t)::text FROM {} t ORDER BY id").format(sql.Identifier(table))
            )
            result[table] = {key: hashlib.sha256(row.encode()).hexdigest() for key, row in cursor}
print(json.dumps(result, sort_keys=True, indent=2))
