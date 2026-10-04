"""Migrate an offline SQLite backup and verify every original column/row survives."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys


def snapshot(conn, schema=None):
    if schema is None:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        schema = {name: [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
                  for name in names}
    result = {}
    for table, columns in schema.items():
        selection = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
        rows = list(conn.execute(f'SELECT {selection} FROM "{table.replace(chr(34), chr(34)*2)}"'))
        encoded = sorted(json.dumps(list(row), ensure_ascii=False, default=str) for row in rows)
        result[table] = {"count": len(rows), "sha256": hashlib.sha256(
            "\n".join(encoded).encode()).hexdigest()}
    return schema, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    source, destination = args.source.resolve(), args.destination.resolve()
    if source == destination or destination.exists():
        raise SystemExit("Destination must be a new, separate database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            schema, before = snapshot(dst)
    os.environ["PP_ENV_FILE"] = ""
    os.environ["PP_DATA_DIR"] = str(destination.parent)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from promptpilot import db
    db.DB_PATH = destination
    db.init_db()
    db.init_db()
    with closing(sqlite3.connect(destination)) as conn:
        _, after = snapshot(conn, schema)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        columns = {table: [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                   for table in ("tasks", "notifications", "workflow_runs")}
    # New migration markers are expected; all user tables must remain identical.
    changed = [table for table in before if before[table] != after[table]
               and table != "schema_migrations"]
    passed = (not changed and integrity == "ok" and not foreign_keys
              and "rights" in columns["tasks"] and "flow_ref" in columns["notifications"]
              and "input_json" in columns["workflow_runs"])
    print(json.dumps({"result": "passed" if passed else "failed", "source": str(source),
                      "copy": str(destination), "before": before, "after": after,
                      "changed_user_tables": changed, "integrity": integrity,
                      "foreign_key_errors": foreign_keys, "columns": columns}, indent=2))
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
