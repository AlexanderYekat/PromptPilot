"""The UI resume request must continue automation using the stored role config."""

import asyncio

import httpx
import pytest

from promptpilot import workflows
from promptpilot.api import app
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowGateDecision,
    WorkflowHumanInput,
    WorkflowStartRequest,
    WorkflowUpdate,
)


def post(workflow, *, resume=True, note="", path="human-input"):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.post(f"/api/workflows/{workflow.id}/{path}", json={
                "expected_version": workflow.state_version,
                "text": "Продолжить работу", "note": note, "resume": resume,
            })

    return asyncio.run(send())


def stopped_workflow(db, *, revision=False, max_rounds=3):
    workflow = db.create_workflow(WorkflowCreate(
        slug="resume-api", objective="Проверить восстановление запуска",
        repository_path=str(db.DB_DIR), candidate_branch="feature/resume",
        config={
            "automation": {"enabled": True, "auto_resume_revision": False},
            "roles": {"executor": {
                "provider": "claude", "model": "opus", "effort": "high",
                "rights": "write", "task_timeout": 1800, "max_retries": 0,
                "prompt_template": "Задание: {{objective}}",
            }},
            "gate": {"commands": []}, "limits": {"max_rounds": max_rounds},
        },
    ))
    workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    workflows.advance_workflow(workflow.id)
    task = db.list_tasks(status=TaskStatus.PENDING)[0]
    if revision:
        db.mark_completed(task.id, "Изменения подготовлены", exit_code=0)
        db.set_verdict(task.id, "ГОТОВО")
        workflows.sync_task(task.id)
        current = db.get_workflow(workflow.id)
        workflows.record_gate(workflow.id, WorkflowGateDecision(
            expected_version=current.state_version, verdict="FAIL",
            gate_id="test-gate", summary="Нужны исправления",
        ))
    else:
        db.mark_failed(task.id, "Слишком длинная командная строка.")
        workflows.sync_task(task.id)
    return db.get_workflow(workflow.id), task


@pytest.mark.parametrize("revision", [False, True])
def test_resume_starts_configured_executor_once(isolated_db, revision):
    stopped, old_task = stopped_workflow(isolated_db, revision=revision)
    note = "Проверь сохранение Unicode"

    response = post(stopped, note=note)

    assert response.status_code == 200
    assert response.json()["status"] == "executing"
    assert response.json()["current_round"] == (2 if revision else 1)
    pending = isolated_db.list_tasks(status=TaskStatus.PENDING)
    assert len(pending) == 1
    task = pending[0]
    assert task.id != old_task.id
    assert (task.provider, task.model, task.effort) == ("claude", "opus", "high")
    assert task.rights == "write"
    assert task.task_timeout == 1800
    assert task.working_dir == stopped.repository_path
    assert task.herdr_target is None
    assert "Задание: Проверить восстановление запуска" in task.prompt
    assert note in task.prompt
    assert isolated_db.get_task(old_task.id).status == (
        TaskStatus.COMPLETED if revision else TaskStatus.FAILED)
    rounds = isolated_db.list_workflow_rounds(stopped.id)
    assert len(rounds) == (2 if revision else 1)
    runs = isolated_db.list_workflow_runs(rounds[-1].id)
    assert runs[-1].attempt_no == (1 if revision else 2)

    # A repeated click with the old version must not create another attempt.
    assert post(stopped, note=note).status_code == 409
    assert len(isolated_db.list_tasks(status=TaskStatus.PENDING)) == 1
    workflows.advance_workflow(stopped.id)
    assert len(isolated_db.list_tasks(status=TaskStatus.PENDING)) == 1


@pytest.mark.parametrize("automation", [
    {"enabled": False},
    {"enabled": True, "auto_dispatch_executor": False},
])
def test_resume_respects_manual_dispatch_settings(isolated_db, automation):
    stopped, _ = stopped_workflow(isolated_db)
    config = {**stopped.config, "automation": automation}
    stopped = isolated_db.update_workflow(stopped.id, WorkflowUpdate(
        expected_version=stopped.state_version, config=config,
    ))

    response = post(stopped)

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING) == []


@pytest.mark.parametrize("resume,revision,max_rounds", [
    (False, False, 3), (True, True, 1),
])
def test_note_or_refused_resume_does_not_advance(
        isolated_db, monkeypatch, resume, revision, max_rounds):
    stopped, _ = stopped_workflow(isolated_db, revision=revision, max_rounds=max_rounds)

    def forbidden(*args):
        raise AssertionError("A note or refused resume must not advance automation")

    monkeypatch.setattr(workflows, "advance_workflow", forbidden)
    response = post(stopped, resume=resume, note="Сохранить указания")

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_human"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING) == []
    assert len(isolated_db.list_workflow_rounds(stopped.id)) == 1


def test_queued_workflow_can_continue_with_saved_settings(isolated_db):
    stopped, _ = stopped_workflow(isolated_db)
    queued = workflows.human_input(stopped.id, WorkflowHumanInput(
        expected_version=stopped.state_version, text="Продолжить", resume=True,
    ))

    response = post(queued, path="advance")

    assert response.status_code == 200
    assert response.json()["status"] == "executing"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING)[0].provider == "claude"


def test_resume_reports_dispatch_failure_without_losing_note(isolated_db):
    stopped, _ = stopped_workflow(isolated_db)
    config = {**stopped.config, "roles": {"executor": {"provider": "herdr-session"}}}
    stopped = isolated_db.update_workflow(stopped.id, WorkflowUpdate(
        expected_version=stopped.state_version, config=config,
    ))

    response = post(stopped, note="Не потерять указания")

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_human"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING) == []
    events = isolated_db.list_workflow_events(stopped.id)
    assert any(e.payload.get("note") == "Не потерять указания" for e in events)
    assert "herdr_target" in str(events[-1].payload)
