"""Regression coverage at the boundaries of the selected fork features."""
import gc
import sqlite3
from contextlib import closing

import pytest

from promptpilot import config, db, workflows
from promptpilot.models import TaskStatus, WorkflowExternalResult, WorkflowHumanInput
import test_workflow_external as external


def test_external_revision_keeps_rights_notes_and_previous_result(isolated_db, monkeypatch):
    settings = external.config()
    settings["automation"]["auto_resume_revision"] = False
    settings["roles"]["planner"] = {"provider": "codex", "rights": "read"}
    settings["roles"]["reviewer"] = {"provider": "codex", "rights": "read"}
    monkeypatch.setattr(external, "config", lambda **kwargs: settings)
    wf = external.planned(isolated_db, [
        dict(external.THREE_STAGES[1], dependencies=[]), external.THREE_STAGES[2]])
    assert isolated_db.list_tasks()[0].rights == "read"  # planner too
    workflows.advance_workflow(wf.id)
    assignment = workflows.external_assignment(wf.id)
    isolated_db.init_db()  # restart/migration preserves the pending assignment
    assert workflows.external_assignment(wf.id) == assignment
    assert isolated_db.get_next_runnable() is None

    waiting = isolated_db.get_workflow(wf.id)
    submission = WorkflowExternalResult(expected_version=waiting.state_version,
                                       result="Original external report", performer="Researcher")
    workflows.submit_external_result(wf.id, submission)
    with pytest.raises(db.WorkflowConflictError):
        workflows.submit_external_result(wf.id, submission)
    workflows.advance_workflow(wf.id)
    reviewer = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert reviewer.rights == "read" and reviewer.provider == "codex"
    external.finish_task(isolated_db, external.REVISION)
    revision = isolated_db.get_workflow(wf.id)
    assert revision.status.value == "revision_required"
    workflows.human_input(wf.id, WorkflowHumanInput(
        expected_version=revision.state_version, text="Continue", resume=True,
        note="Check sources published after 2025"))
    workflows.advance_workflow(wf.id)
    revised = workflows.external_assignment(wf.id)
    assert "Original external report" in revised.assignment
    assert "Check sources published after 2025" in revised.assignment
    assert "Нет источников" in revised.assignment
    workflows.cancel_workflow(wf.id, isolated_db.get_workflow(wf.id).state_version)
    with pytest.raises(db.WorkflowConflictError):
        external.submit(wf.id, "too late")


def test_all_fork_columns_migrate_together(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    legacy = db.SCHEMA.replace("    ,rights TEXT\n", "").replace(
        "    flow_ref TEXT\n", "").replace("    machine TEXT,\n);", "    machine TEXT\n);")
    # Match the actual source schema instead of silently testing a modern DB.
    legacy = legacy.replace("    input_json TEXT,\n", "")
    assert "input_json" not in legacy and "flow_ref" not in legacy and ",rights" not in legacy
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(legacy)
        conn.execute("INSERT INTO tasks(prompt,created_at) VALUES('retained','2026-01-01')")
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.init_db()
    with db._connect() as conn:
        for table, field in [("tasks", "rights"), ("notifications", "flow_ref"),
                             ("workflow_runs", "input_json")]:
            assert field in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert conn.execute("SELECT prompt FROM tasks").fetchone()[0] == "retained"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_connection_is_closed_when_begin_fails(tmp_path, monkeypatch):
    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                raise sqlite3.OperationalError("database is locked")
            return super().execute(sql, *args)

    connection = sqlite3.connect(":memory:", factory=FailingConnection)
    monkeypatch.setattr(db, "DB_DIR", tmp_path)
    monkeypatch.setattr(db.sqlite3, "connect", lambda *a, **k: connection)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        with db._connect(immediate=True):
            pytest.fail("unreachable")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    gc.collect()


def test_explicit_environment_does_not_load_production(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("PP_TG_TOKEN=production-secret\n")
    isolated = tmp_path / "trial.env"
    isolated.write_text("PP_PORT=8421\n")
    monkeypatch.setenv("PP_ENV_FILE", str(isolated))
    monkeypatch.delenv("PP_TG_TOKEN", raising=False)
    monkeypatch.delenv("PP_PORT", raising=False)
    config._load_dotenv()
    import os
    assert "PP_TG_TOKEN" not in os.environ
    assert os.environ["PP_PORT"] == "8421"
