"""Quota-free WAL connection churn probe; always creates a NEW isolated database.

Example: python tools/probe_sqlite_wal.py --seconds 35 --anchor
Evidence stays under _local/sqlite-probes. No user database or provider is used.
"""
import argparse
from contextlib import closing, nullcontext
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def churn(path, number, seconds, queue):
    deadline = time.monotonic() + seconds
    count = 0
    errors = []
    while time.monotonic() < deadline:
        phase = "connect"
        try:
            with closing(sqlite3.connect(path, timeout=10)) as conn, conn:
                conn.execute("PRAGMA foreign_keys=ON")
                phase = "read"
                conn.execute("SELECT value FROM settings WHERE key=?", ("cancel_task:24",)).fetchone()
                if number:
                    phase = "write"
                    conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (str(number), str(count)))
                phase = "commit/close"
        except sqlite3.Error as exc:
            errors.append({"phase": phase, "error": str(exc),
                           "code": getattr(exc, "sqlite_errorcode", None),
                           "name": getattr(exc, "sqlite_errorname", None)})
        count += 1
    queue.put({"process": number, "connections": count,
               "error_count": len(errors), "errors": errors[:20]})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=35)
    parser.add_argument("--anchor", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("seconds must be between 1 and 300")
    base = ROOT / "_local" / "sqlite-probes"
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="wal-", dir=base))
    path = directory / "promptpilot.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
    lifetime = nullcontext()
    if args.anchor:
        sys.path.insert(0, str(ROOT))
        from promptpilot import db
        db.DB_DIR, db.DB_PATH = directory, path
        lifetime = db.wal_connection_lifetime()
    queue = multiprocessing.Queue()
    children = [multiprocessing.Process(target=churn, args=(str(path), i, args.seconds, queue))
                for i in range(3)]
    rows = []
    try:
        with lifetime:
            for child in children:
                child.start()
            rows = [queue.get(timeout=args.seconds + 20) for _ in children]
    finally:
        for child in children:
            if child.pid is None:
                continue
            child.join(timeout=2)
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)
        queue.close()
        queue.join_thread()
    with closing(sqlite3.connect(path)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    report = {"sqlite": sqlite3.sqlite_version, "database": str(path),
              "anchor": args.anchor, "seconds": args.seconds,
              "integrity": integrity, "results": rows,
              "exit_codes": [child.exitcode for child in children]}
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
