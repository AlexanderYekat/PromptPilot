"""Planner failures use isolated data and never call a paid provider."""
import hashlib
import sqlite3

import pytest

from promptpilot import db, worker, workflows
from promptpilot.models import WorkflowPlanDispatch
import test_workflow_stage_planner as planner


@pytest.mark.parametrize("status,exit_code,verdict,result,code", [
    ("failed", 1, None, None, "task_failed"),
    ("cancelled", None, None, None, "cancelled"),
    ("completed", 1, "ГОТОВО", "partial output", "task_failed"),
    ("completed", 0, None, "no verdict", "unsuccessful_result"),
    ("completed", 0, "ГОТОВО", "not a stage contract", "invalid_output"),
])
def test_planner_classifies_terminal_outcome(
        isolated_db, monkeypatch, status, exit_code, verdict, result, code):
    wf = planner.create(isolated_db)
    plan = workflows.dispatch_planner(wf.id, WorkflowPlanDispatch(expected_version=0))
    task = isolated_db.get_next_runnable()
    error = "OperationalError: disk I/O error" if code == "task_failed" else None
    with isolated_db._connect() as conn:
        conn.execute("""UPDATE tasks SET status=?, exit_code=?, verdict=?, result=?,
                        error=?, completed_at=? WHERE id=?""",
                     (status, exit_code, verdict, result, error, db._now(), task.id))
    if code != "invalid_output":
        def forbidden_parse(*args):
            pytest.fail("Execution failures must not be validated as model contracts")
        monkeypatch.setattr(workflows, "parse_workflow_plan", forbidden_parse)
    settled = workflows.sync_planner_task(task.id)
    assert settled.status == ("cancelled" if status == "cancelled" else "failed")
    assert settled.output["failure"]["code"] == code
    assert settled.output["failure"]["next_action"]
    assert settled.output["error"] == error
    assert settled.output["result"] == result
    assert isolated_db.list_workflow_stages(wf.id) == []
    events = isolated_db.list_workflow_events(wf.id)
    assert events[-1].event_type == f"planner.{code}"
    assert events[-1].payload["task_id"] == plan.planner_task_id
    assert events[-1].payload["error"] == error
    version = isolated_db.get_workflow(wf.id).state_version
    workflows.sync_all_tasks(wf.id)
    assert isolated_db.get_workflow(wf.id).state_version == version
    assert isolated_db.list_workflow_events(wf.id) == events
    assert len(isolated_db.list_tasks()) == 1
    # A deliberate retry creates a new task and keeps the old failure intact.
    retry = workflows.dispatch_planner(wf.id, WorkflowPlanDispatch(expected_version=version))
    assert retry.planner_task_id != task.id
    assert isolated_db.get_task(task.id).error == error
    assert isolated_db.get_workflow_plan(wf.id).output is None
    workflows.sync_planner_task(task.id)  # late callback from old task
    assert isolated_db.get_workflow_plan(wf.id) == retry


def test_legacy_failed_projection_reconciles_once_without_erasing_history(isolated_db):
    wf = planner.create(isolated_db)
    plan = workflows.dispatch_planner(wf.id, WorkflowPlanDispatch(expected_version=0))
    task = isolated_db.get_next_runnable()
    isolated_db.mark_failed(task.id, "OperationalError: disk I/O error", exit_code=1)
    raw = {"task_status": "failed", "error": "OperationalError: disk I/O error",
           "result": None, "exit_code": 1, "verdict": None, "model_used": None}
    raw_json = db._json_dump(raw)
    with db._connect() as conn:
        conn.execute("UPDATE workflow_plans SET status='failed', output_json=?, output_sha256=? WHERE workflow_id=?",
                     (raw_json, hashlib.sha256(raw_json.encode()).hexdigest(), wf.id))
        workflows._transition(conn, workflows._workflow_row(conn, wf.id),
                              workflows.WorkflowStatus.AWAITING_HUMAN,
                              "planner.invalid_output", {"reason": "old misleading reason"})
    old_events = isolated_db.list_workflow_events(wf.id)
    workflows.sync_all_tasks(wf.id)
    assert isolated_db.get_workflow_plan(wf.id).output["failure"]["code"] == "task_failed"
    assert isolated_db.get_workflow_plan(wf.id).planner_task_id == plan.planner_task_id
    assert isolated_db.list_workflow_events(wf.id)[:-1] == old_events
    assert isolated_db.list_workflow_events(wf.id)[-1].event_type == "planner.task_failed"
    workflows.sync_all_tasks(wf.id)
    assert len(isolated_db.list_workflow_events(wf.id)) == len(old_events) + 1
    assert len(isolated_db.list_tasks()) == 1


def test_cancelled_workflow_is_not_revived_by_planner_callback(isolated_db):
    wf = planner.create(isolated_db)
    plan = workflows.dispatch_planner(wf.id, WorkflowPlanDispatch(expected_version=0))
    active = isolated_db.get_workflow(wf.id)
    workflows.cancel_workflow(wf.id, active.state_version)
    cancelled = isolated_db.get_workflow(wf.id)
    events = isolated_db.list_workflow_events(wf.id)
    workflows.sync_planner_task(plan.planner_task_id)
    workflows.sync_all_tasks(wf.id)
    assert isolated_db.get_workflow(wf.id) == cancelled
    assert isolated_db.list_workflow_events(wf.id) == events


def test_sqlite_diagnostics_preserve_original_error_on_rollback_failure(
        isolated_db, monkeypatch, caplog):
    original = sqlite3.OperationalError("disk I/O error")
    original.sqlite_errorcode = 1546
    original.sqlite_errorname = "SQLITE_IOERR_TRUNCATE"
    class Connection(sqlite3.Connection):
        def rollback(self):
            raise sqlite3.OperationalError("secondary rollback failure")
    conn = sqlite3.connect(":memory:", factory=Connection)
    monkeypatch.setattr(db.sqlite3, "connect", lambda *args, **kwargs: conn)
    with pytest.raises(sqlite3.OperationalError) as caught:
        with db._connect():
            raise original
    assert caught.value is original
    assert "SQLITE_IOERR_TRUNCATE" in caplog.text
    assert "1546" in caplog.text
    assert str(isolated_db.DB_PATH).replace("\\", "\\\\") in caplog.text
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_worker_keeps_idle_wal_attachment_and_closes_on_error(isolated_db, monkeypatch):
    original_connect = db.sqlite3.connect
    connections = []
    def connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        connections.append(conn)
        return conn
    monkeypatch.setattr(db.sqlite3, "connect", connect)
    def body():
        assert len(connections) == 1
        anchor = connections[0]
        assert not anchor.in_transaction
        isolated_db.set_setting("peer_write", "works")
        assert isolated_db.get_setting("peer_write") == "works"
        assert not anchor.in_transaction
        assert isolated_db.DB_PATH.with_name("promptpilot.db-shm").exists()
        with db._connect() as peer:
            # An idle attachment must not block checkpointing.
            checkpoint = peer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            assert checkpoint[0] == 0
        raise RuntimeError("stop worker")
    monkeypatch.setattr(worker, "_run_worker", body)
    with pytest.raises(RuntimeError, match="stop worker"):
        worker.run_worker()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
