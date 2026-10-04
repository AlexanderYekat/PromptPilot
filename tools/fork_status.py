"""Inspect selected upstream revisions and PR states without changing the checkout."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True, encoding="utf-8").strip()


def revision(ref):
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", ref],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def validate_remotes(sources):
    for remote in ("origin", "upstream", "contributor"):
        for option in ([], ["--push"]):
            urls = git("remote", "get-url", "--all", *option, remote).splitlines()
            if urls != [sources[remote]]:
                raise RuntimeError(f"Unexpected {remote} URL(s): {urls}")


def inspect(fetch=False, target="custom", pr_status=True):
    sources = json.loads((ROOT / "fork-sources.json").read_text(encoding="utf-8"))
    validate_remotes(sources)
    if fetch:
        for remote in ("origin", "upstream", "contributor"):
            subprocess.run(["git", "-C", str(ROOT), "fetch", remote], check=True)
    accepted = revision(target)
    if not accepted:
        raise RuntimeError(f"Missing target {target}; do not assume a clean baseline")
    rows = []
    features = [f for f in sources["features"] if f.get("maintained", True)]
    for ref in ["upstream/main", *["contributor/" + f["branch"] for f in features]]:
        sha = revision(ref)
        rows.append({"source": ref, "commit": sha,
                     "not_in_custom": int(git("rev-list", "--count", f"{accepted}..{sha}")) if sha else None})
    prs = []
    if pr_status:
        for feature in features:
            pr = json.loads(subprocess.check_output([
                "gh", "api", f"repos/ivanarama/PromptPilot/pulls/{feature['pr']}"],
                text=True, encoding="utf-8"))
            prs.append({"number": feature["pr"], "state": pr["state"],
                        "merged": pr["merged"], "head": pr["head"]["sha"],
                        "changed": pr["state"] != feature.get("pr_state", "open")
                        or pr["head"]["sha"] != feature["accepted_commit"]})
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "target": target,
            "custom_commit": accepted, "branch": git("branch", "--show-current"),
            "tracked_tree_clean": not bool(git("status", "--porcelain", "--untracked-files=no")),
            "origin_main": revision("origin/main"), "origin_custom": revision("origin/custom"),
            "main": revision("main"), "sources": rows, "pull_requests": prs,
            "updates_available": any(r["not_in_custom"] != 0 for r in rows)
            or any(p["changed"] for p in prs)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--target", default="custom")
    parser.add_argument("--offline", action="store_true", help="Skip PR API; not a maintenance decision")
    args = parser.parse_args()
    print(json.dumps(inspect(args.fetch, args.target, not args.offline), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
