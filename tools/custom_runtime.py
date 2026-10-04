"""Prepare an immutable trial release and safe, local demonstrations."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def prepare(ref):
    sha = git("rev-parse", "--verify", ref + "^{commit}")
    local = ROOT / "_local"
    release = local / "releases" / sha
    marker = release / "release.json"
    if release.exists() and not marker.exists():
        raise SystemExit(f"Incomplete release, inspect before retrying: {release}")
    if not release.exists():
        release.mkdir(parents=True)
        archive = local / f"{sha}.zip"
        subprocess.run(["git", "-C", str(ROOT), "archive", "--format=zip",
                        f"--output={archive}", sha], check=True)
        with zipfile.ZipFile(archive) as source:
            source.extractall(release)
        marker.write_text(json.dumps({"commit": sha, "source_ref": ref,
                          "created_at": datetime.now(timezone.utc).isoformat()}, indent=2),
                          encoding="utf-8")
    data = local / "data"
    data.mkdir(exist_ok=True)
    envfile = local / "trial.env"
    if not envfile.exists():
        envfile.write_text(
            f"PP_DATA_DIR={data}\nPP_FLOWS_DIR={data / 'flows'}\n"
            "PP_HOST=127.0.0.1\nPP_PORT=8421\nPP_DEFAULT_CLI=codex\n"
            "PP_TG_TOKEN=\nPP_API_TOKEN=\nPP_TASK_PASSWORD=\n"
            "PP_PIPELINE_SNAPSHOT_INTERVAL=0\nPP_HERDR_WATCH=0\n"
            "PP_VERDICT_REPAIR=0\n", encoding="utf-8")
    pointer = {"commit": sha, "release": str(release), "env_file": str(envfile),
               "data": str(data), "python": sys.executable}
    (local / "trial-release.json").write_text(json.dumps(pointer, indent=2), encoding="utf-8")
    print(json.dumps(pointer, indent=2))


def seed():
    # Called with the pinned release on sys.path by the launcher.
    from promptpilot import config, db, flows, workflows
    from promptpilot.models import WorkflowCreate, WorkflowStartRequest

    config.DB_DIR.mkdir(parents=True, exist_ok=True)
    initialized = config.DB_DIR / "trial-initialized.json"
    if initialized.exists():
        print(initialized.read_text(encoding="utf-8"))
        return
    db.init_db()
    db.set_setting("worker_paused", "1")
    workspace = config.DB_DIR / "demo-workspace"
    workspace.mkdir(exist_ok=True)
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    (workspace / "README.txt").write_text("Isolated PromptPilot trial. No production files.\n",
                                          encoding="utf-8")
    flowdir = flows.flows_dir()
    flowdir.mkdir(parents=True, exist_ok=True)
    route = {
        "name": "demo-local", "title": "Локальная заявка: подсчёт слов и решение",
        "trust": "owner", "steps": [
            {"id": "count", "kind": "command",
             "run": [sys.executable, "-c",
                     "import json,sys; s=sys.stdin.read(); print(json.dumps({'words':len(s.split())}))"],
             "stdin": "{{input.body}}", "output_json": True},
            {"id": "approve", "kind": "human", "notify": False,
             "text": "Принять «{{item.title}}»? Слов: {{steps.count.words}}",
             "show": ["steps.count.words"], "on_reject": "reject"},
            {"id": "done", "kind": "finish", "status": "done", "notify": False},
        ],
    }
    path = flowdir / "demo-local.json"
    if path.exists():
        raise SystemExit(f"Refusing to replace existing route: {path}")
    path.write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
    definition = flows.load_flow_file(path)
    item = flows.create_item(definition, {"body": "Это локальная тестовая заявка"},
                             title="Проверка локального маршрута", dedup_key="custom-demo-v1", by="setup")
    flows.advance_item(item["id"])
    wf = db.get_workflow_by_ref("demo-external")
    if wf is None:
        wf = db.create_workflow(WorkflowCreate(
            slug="demo-external", objective="Подготовить короткую инструкцию резервного копирования тестового файла. "
            "Указать: исходный файл, отдельную копию, проверку совпадения SHA256 и восстановление. "
            "Reviewer должен потребовать доработку, если отсутствует любой из этих четырёх пунктов.",
            repository_path=str(workspace), candidate_branch="main",
            config={"automation": {"enabled": True}, "roles": {
                "executor": {"provider": "codex", "rights": "read"},
                "reviewer": {"provider": "codex", "rights": "read", "task_timeout": 180}},
                "gate": {"commands": []}, "limits": {"max_rounds": 6}}))
        wf = workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=wf.state_version))
        workflows.request_external(wf.id, wf.state_version)
    result = {"workflow": wf.id, "flow_item": item["id"],
              "worker_paused": True, "agent_calls": 0, "simulated_agent_responses": False}
    initialized.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--ref", default="custom")
    sub.add_parser("seed")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.ref)
    else:
        # Source comes from this release, regardless of any editable install.
        sys.path.insert(0, str(ROOT))
        if "PP_ENV_FILE" not in os.environ:
            raise SystemExit("Set PP_ENV_FILE to the isolated trial configuration")
        seed()


if __name__ == "__main__":
    main()
