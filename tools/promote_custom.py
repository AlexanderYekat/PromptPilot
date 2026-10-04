"""Publish only a fully checked candidate, preserving remote history atomically."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from fork_status import ROOT, git, revision, validate_remotes


EXPECTED_JOBS = {"javascript", "lint", *[
    f"pytest ({system}, {version})" for system in ("ubuntu-latest", "windows-latest")
    for version in ("3.10", "3.12")]}


def successful_matrix(jobs):
    completed = {job["name"] for job in jobs
                 if job["status"] == "completed" and job["conclusion"] == "success"}
    return EXPECTED_JOBS <= completed


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True, encoding="utf-8"))


def promote(candidate, evidence, dry_run=False):
    sources = json.loads((ROOT / "fork-sources.json").read_text(encoding="utf-8"))
    validate_remotes(sources)
    sha = git("rev-parse", "--verify", candidate + "^{commit}")
    receipt = json.loads(Path(evidence).read_text(encoding="utf-8-sig"))
    if receipt.get("commit") != sha or receipt.get("result") != "passed":
        raise RuntimeError("Local check receipt must pass for this exact candidate commit")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Tracked checkout changes remain; commit and revalidate")
    runs = api(f"repos/AlexanderYekat/PromptPilot/actions/workflows/test.yml/runs?head_sha={sha}&per_page=30")["workflow_runs"]
    passing = [r for r in runs if r["head_sha"] == sha and r["status"] == "completed"
               and r["conclusion"] == "success"]
    if not passing:
        raise RuntimeError("Tests CI has not passed for the candidate")
    run = passing[0]
    jobs = api(f"repos/AlexanderYekat/PromptPilot/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
    if not successful_matrix(jobs):
        raise RuntimeError("Required Windows/Linux Python 3.10/3.12, lint and JS jobs are missing or failed")
    for remote in ("origin", "upstream"):
        subprocess.run(["git", "-C", str(ROOT), "fetch", remote], check=True)
    previous = revision("origin/custom")
    upstream = revision("upstream/main")
    for old, new in [(previous, sha), (revision("origin/main"), upstream), (upstream, sha)]:
        if old and subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", old, new]).returncode:
            raise RuntimeError(f"Non-fast-forward or outdated candidate: {old} -> {new}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    refs = [f"{sha}:refs/heads/custom", f"{upstream}:refs/heads/main"]
    if previous:
        refs.append(f"{previous}:refs/heads/archive/custom-{stamp}-{previous[:10]}")
    args = ["git", "-C", str(ROOT), "push", "--atomic"]
    if dry_run:
        args.append("--dry-run")
    subprocess.run([*args, "origin", *refs], check=True)
    print(json.dumps({"commit": sha, "previous_custom": previous, "ci": run["html_url"],
                      "dry_run": dry_run, "published": not dry_run}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    promote(args.candidate, args.evidence, args.dry_run)
