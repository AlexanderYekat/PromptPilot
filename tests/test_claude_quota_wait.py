"""Claude quota waits survive restarts and resume without spending retries."""

from datetime import datetime, timedelta, timezone
import json

import pytest

from promptpilot import db, herdr_exec, worker, workflows
from promptpilot.models import TaskCreate, WorkflowCreate, WorkflowStartRequest
from test_claude_prompt_transport import fake_claude


@pytest.fixture
def clock(monkeypatch):
    current = [datetime(2026, 10, 15, 10, tzinfo=timezone.utc)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz)

    monkeypatch.setattr(worker, "datetime", Clock)
    monkeypatch.setattr(db, "_now", lambda: current[0].isoformat())
    return current


@pytest.mark.parametrize("output_kind", ["stderr", "stdout", "result", "stream"])
def test_headless_limit_waits_five_hours_even_with_no_retries(
        isolated_db, tmp_path, monkeypatch, clock, output_kind):
    banner = "You've hit your limit · resets 6pm"
    outputs = {
        "stderr": f"print({banner!r}, file=sys.stderr); sys.exit(1)",
        "stdout": f"print({banner!r}); sys.exit(1)",
        "result": "print(json.dumps({'type': 'result', 'is_error': True, "
                  f"'result': {banner!r}, 'session_id': 'quota-session'}})); sys.exit(1)",
        "stream": "print(json.dumps({'type': 'rate_limit_event', "
                  "'rate_limit_info': {'status': 'rejected'}}))",
    }
    fake_claude(tmp_path, monkeypatch,
                "import json, sys\nsys.stdin.read()\n" + outputs[output_kind] + "\n")
    created = isolated_db.create_task(TaskCreate(
        prompt="continue the work", provider="claude", working_dir=str(tmp_path),
        max_retries=0, session_id="quota-session",
    ))
    # The result-only case must capture the session without a system event.
    if output_kind == "result":
        with isolated_db._connect() as conn:
            conn.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (created.id,))
    worker.execute_task(isolated_db.get_next_runnable())

    waiting = isolated_db.get_task(created.id)
    assert waiting.status.value == "rate_limited", waiting.error
    assert waiting.next_run_at == clock[0] + timedelta(hours=5)
    assert waiting.retry_count == 0
    assert waiting.session_id == "quota-session"
    assert waiting.completed_at is None
    assert isolated_db.get_next_runnable() is None

    # Reopening and worker startup recovery must not erase the deadline.
    isolated_db.init_db()
    isolated_db.recover_running()
    assert isolated_db.get_task(created.id).next_run_at == waiting.next_run_at
    clock[0] = waiting.next_run_at - timedelta(seconds=1)
    assert isolated_db.get_next_runnable() is None
    clock[0] = waiting.next_run_at
    resumed = isolated_db.get_next_runnable()
    assert resumed.id == created.id
    assert resumed.session_id == "quota-session"


def test_workflow_resumes_same_session_after_repeated_quota_windows(
        isolated_db, tmp_path, monkeypatch, clock):
    fake_claude(tmp_path, monkeypatch,
                "import json, pathlib, sys\n"
                "sys.stdin.read()\n"
                "pathlib.Path('args.json').write_text(json.dumps(sys.argv), encoding='utf-8')\n"
                "print(json.dumps({'type': 'system', 'session_id': 'workflow-session'}))\n"
                "if not pathlib.Path('quota-reset').exists():\n"
                "    print(json.dumps({'type': 'result', 'is_error': True, "
                "'result': 'You have reached your usage limit'}))\n"
                "    sys.exit(1)\n"
                "print(json.dumps({'type': 'result', 'result': 'ИТОГ: ГОТОВО'}))\n")
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="quota-test", objective="finish the stage", repository_path=str(tmp_path),
        candidate_branch="test/quota", config={
            "automation": {"enabled": True},
            "roles": {"executor": {"provider": "claude", "max_retries": 0}},
            "gate": {"commands": []},
        },
    ))
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    first = isolated_db.get_next_runnable()
    isolated_db.set_note(first.id, "keep the operator note across quota waits")

    for attempt in range(2):
        task = first if attempt == 0 else isolated_db.get_next_runnable()
        assert task.id == first.id
        worker.execute_task(task)
        waiting = isolated_db.get_task(task.id)
        assert waiting.status.value == "rate_limited", waiting.error
        assert waiting.retry_count == 0
        assert waiting.next_run_at == clock[0] + timedelta(hours=5)
        assert waiting.note == "keep the operator note across quota waits"
        current = isolated_db.get_workflow(workflow.id)
        assert current.status.value == "executing"
        assert current.current_round == 1
        assert len(isolated_db.list_tasks()) == 1
        # Reconciliation can replay without duplicating an attempt event.
        workflows.sync_all_tasks()
        clock[0] = waiting.next_run_at

    (tmp_path / "quota-reset").touch()
    worker.execute_task(isolated_db.get_next_runnable())

    completed = isolated_db.get_task(first.id)
    assert completed.status.value == "completed", completed.error
    args = json.loads((tmp_path / "args.json").read_text(encoding="utf-8"))
    assert args[args.index("--resume") + 1] == "workflow-session"
    assert isolated_db.get_workflow(workflow.id).status.value == "reviewing"
    assert len(isolated_db.list_workflow_rounds(workflow.id)) == 1
    events = isolated_db.list_workflow_events(workflow.id)
    assert sum(event.event_type == "run.requeued" for event in events) == 2
    assert sum(event.event_type == "run.started" for event in events) == 3


def test_herdr_claude_uses_same_quota_policy(isolated_db, monkeypatch, clock):
    created = isolated_db.create_task(TaskCreate(prompt="work", max_retries=0))
    monkeypatch.setattr(herdr_exec, "run_in_herdr", lambda *args, **kwargs: {
        "ok": False, "rate_limited": True, "retry_reason": "rate_limit",
        "error": "You reached your usage limit", "output": "",
    })
    worker._execute_herdr_task(isolated_db.get_next_runnable(), {"kind": "claude"})
    waiting = isolated_db.get_task(created.id)
    assert waiting.next_run_at == clock[0] + timedelta(hours=5)
    assert waiting.retry_count == 0
    assert waiting.status.value == "rate_limited"


@pytest.mark.parametrize("provider,reason", [
    ({"kind": "claude"}, "overload"),
    ({"kind": "codex"}, "rate_limit"),
])
def test_other_limits_and_overload_keep_bounded_backoff(
        isolated_db, monkeypatch, clock, provider, reason):
    created = isolated_db.create_task(TaskCreate(prompt="work", max_retries=1))
    monkeypatch.setattr(worker.random, "random", lambda: 0.5)
    monkeypatch.setattr(worker, "BASE_DELAY", 60)
    monkeypatch.setattr(worker, "MAX_DELAY", 3600)
    task = isolated_db.get_next_runnable()
    worker._requeue_provider_limit(task, reason, "provider unavailable", provider)
    waiting = isolated_db.get_task(created.id)
    assert waiting.retry_count == 1
    assert waiting.next_run_at == clock[0] + timedelta(minutes=1)
    clock[0] = waiting.next_run_at
    worker._requeue_provider_limit(
        isolated_db.get_next_runnable(), reason, "still unavailable", provider)
    assert isolated_db.get_task(created.id).status.value == "failed"


def test_stale_quota_callback_cannot_delay_resumed_attempt(isolated_db, clock):
    created = isolated_db.create_task(TaskCreate(prompt="work"))
    first = isolated_db.get_next_runnable()
    worker._requeue_provider_limit(first, "rate_limit", "limit", {"kind": "claude"})
    clock[0] += timedelta(hours=5)
    resumed = isolated_db.get_next_runnable()
    assert resumed.id == first.id
    assert worker._requeue_provider_limit(
        first, "rate_limit", "stale limit", {"kind": "claude"}) is False
    current = isolated_db.get_task(created.id)
    assert current.status.value == "running"
    assert current.started_at == resumed.started_at
    assert current.next_run_at is None


def test_herdr_recognizes_current_limit_banner():
    assert herdr_exec._looks_rate_limited("You've hit your limit · resets 6pm")
