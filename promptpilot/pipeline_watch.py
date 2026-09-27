"""Bounded, read-only pipeline watch; no agent launches or GitHub mutations."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.request import urlopen


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def read_api(base, path):
    with urlopen(base.rstrip("/") + path, timeout=20) as response:
        return json.load(response)


def summarize(tasks, series, started_at, now):
    completed = [t for t in tasks if t.get("completed_at")
                 and t["completed_at"] >= started_at]
    usage = {"input": 0, "cached_input": 0, "uncached_input": 0, "output": 0}
    for task in completed:
        result = task.get("result") or ""
        tokens = re.search(r"Tokens: (\d+) in / (\d+) out", result)
        cached = re.search(r"Cached input: (\d+)", result)
        if tokens:
            usage["input"] += int(tokens[1])
            usage["output"] += int(tokens[2])
        if cached:
            usage["cached_input"] += int(cached[1])
    usage["uncached_input"] = usage["input"] - usage["cached_input"]
    alerts = []
    running = []
    now_dt = datetime.fromisoformat(now)
    for task in tasks:
        if task.get("status") != "running" or not task.get("started_at"):
            continue
        age = (now_dt - datetime.fromisoformat(task["started_at"])).total_seconds()
        running.append({"id": task["id"], "series_id": task.get("series_id"),
                        "age_minutes": round(age / 60, 1)})
        timeout = task.get("task_timeout") or 3600
        if age > timeout:
            alerts.append(f"task #{task['id']} exceeded timeout")
    for item in series:
        if item.get("paused") and item.get("auto_pause_reason"):
            alerts.append(f"series #{item['id']} auto-paused: {item['auto_pause_reason']}")
    return {
        "completed_runs": len(completed),
        "productive_runs": sum(t.get("verdict") == "ГОТОВО" for t in completed),
        "human_handoffs": sum(t.get("verdict") == "НУЖЕН ЧЕЛОВЕК" for t in completed),
        "empty_runs": sum(t.get("verdict") == "ПУСТО" for t in completed),
        "completed_run_tokens": usage, "running": running, "alerts": alerts,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8420")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=3600)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--gh", default=os.environ.get("PP_GH_EXE", "gh"))
    args = parser.parse_args(argv)
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository)
            or not 1 <= args.duration <= 86400 or not 10 <= args.interval <= 60):
        parser.error("invalid repository, duration or interval")
    started = utcnow()
    project_prefix = args.repository.split("/", 1)[1].lower()
    deadline = time.monotonic() + args.duration
    seen = {}
    merges = {}
    next_github_read = 0
    report = {"started_at": started, "repository": args.repository,
              "planned_seconds": args.duration, "read_only": True}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    while True:
        now = utcnow()
        errors = []
        try:
            tasks = read_api(args.url, "/api/tasks?limit=100")
            # Avoid reading the worker's real DB or executing project health.
            tasks = [t for t in tasks if (t.get("series_title") or "").lower().startswith(project_prefix)]
            seen.update({t["id"]: t for t in tasks})
            series = read_api(args.url, "/api/schedule")
            series = [s for s in series if str(s.get("title") or "").lower().startswith(project_prefix)]
            report.update(summarize(list(seen.values()), series, started, now))
            report["worker"] = read_api(args.url, "/api/worker/status")
        except Exception as exc:
            errors.append(f"API read failed: {exc}")
        if time.monotonic() >= next_github_read:
            try:
                result = subprocess.run([
                    args.gh, "api", f"repos/{args.repository}/pulls?state=closed&sort=updated&direction=desc&per_page=50",
                ], capture_output=True, text=True, encoding="utf-8", timeout=45)
                if result.returncode:
                    raise RuntimeError(result.stderr.strip()[:400])
                for pr in json.loads(result.stdout):
                    if pr.get("merged_at") and pr["merged_at"] >= started:
                        merges[pr["number"]] = {"number": pr["number"], "merged_at": pr["merged_at"]}
                report["github_checked_at"] = now
            except Exception as exc:
                errors.append(f"GitHub read failed: {exc}")
            next_github_read = time.monotonic() + 600
        finished = time.monotonic() >= deadline
        report.update(last_updated_at=now, finished=finished,
                      confirmed_merges=list(merges.values()), read_errors=errors)
        temporary = args.report.with_suffix(args.report.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(args.report)
        print(json.dumps({"at": now, "merges": list(merges),
                          "alerts": report.get("alerts", []), "errors": errors}), flush=True)
        if finished:
            return 0
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
