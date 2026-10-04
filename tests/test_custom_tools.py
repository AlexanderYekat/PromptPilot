"""Trial setup and fail-closed publication safeguards."""
import json
from pathlib import Path

import pytest


@pytest.fixture
def custom_tools(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    import custom_runtime
    import promote_custom
    return custom_runtime, promote_custom


def test_matrix_rejects_failed_or_missing_jobs(custom_tools):
    _, promote = custom_tools
    jobs = [{"name": name, "status": "completed", "conclusion": "success"}
            for name in promote.EXPECTED_JOBS]
    assert promote.successful_matrix(jobs)
    assert not promote.successful_matrix(jobs[:-1])
    jobs[0]["conclusion"] = "failure"
    assert not promote.successful_matrix(jobs)
    jobs[0]["conclusion"] = "skipped"
    assert not promote.successful_matrix(jobs)


def test_trial_seed_is_idempotent_and_starts_no_agent(custom_tools, isolated_db, monkeypatch):
    from promptpilot import config, flows
    runtime, _ = custom_tools
    monkeypatch.setattr(config, "DB_DIR", isolated_db.DB_DIR)
    monkeypatch.setenv("PP_FLOWS_DIR", str(isolated_db.DB_DIR / "flows"))
    runtime.seed()
    receipt = json.loads((isolated_db.DB_DIR / "trial-initialized.json").read_text())
    item = flows.get_item(receipt["flow_item"])
    assert item["status"] == "waiting_human"
    assert item["data"]["steps"]["count"]["words"] == 4
    assert isolated_db.get_workflow(receipt["workflow"]).status.value == "awaiting_external"
    assert isolated_db.list_tasks() == []
    runtime.seed()
    assert isolated_db.list_tasks() == []


def test_cli_workflow_dispatch_preserves_rights(isolated_db):
    from click.testing import CliRunner
    from promptpilot.cli import cli
    from promptpilot.models import WorkflowCreate, WorkflowStartRequest
    from promptpilot import workflows
    wf = isolated_db.create_workflow(WorkflowCreate(
        slug="cli-rights", objective="Read demo", repository_path=str(isolated_db.DB_DIR),
        candidate_branch="main"))
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    result = CliRunner().invoke(cli, ["workflow", "dispatch", wf.id, "executor", "Read only",
                                   "--rights", "read", "-c", "codex"])
    assert result.exit_code == 0, result.output
    assert isolated_db.list_tasks()[0].rights == "read"
