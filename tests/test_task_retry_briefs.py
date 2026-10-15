from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from promptpilot import task_briefs, workflows
from promptpilot.models import TaskCreate, TaskStatus
from test_workflow_api_cli import request
from test_workflow_stage_planner import (
    finish_task, pause_after, reviewer_pass, start_two_stages,
)


def waiting(db, task=None):
    if task is None:
        task = db.create_task(TaskCreate(
            prompt="Исправить отчёт", session_id="keep-session", provider="claude",
            model="opus", rights="read", max_retries=0,
        ))
        task = db.get_next_runnable()
    db.set_note(task.id, "Сохранить ограничения")
    db.mark_rate_limited(task.id, datetime.now(timezone.utc) + timedelta(hours=5),
                         "Лимит сессии", count_retry=False)
    return db.get_task(task.id)


def retry(task):
    return request("POST", f"/api/tasks/{task.id}/retry", json={
        "expected_next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
    })


def test_retry_preserves_task_session_and_other_waits(isolated_db):
    task = waiting(isolated_db)
    other = waiting(isolated_db)
    response = retry(task)
    assert response.status_code == 200, response.text
    pending = isolated_db.get_task(task.id)
    before = task.model_dump(exclude={"status", "next_run_at", "scheduled_at"})
    assert pending.model_dump(exclude={"status", "next_run_at", "scheduled_at"}) == before
    assert pending.status == TaskStatus.PENDING
    assert pending.next_run_at is None
    assert isolated_db.get_task(other.id) == other
    assert len(isolated_db.list_tasks()) == 2
    assert retry(task).status_code == 409
    claimed = isolated_db.get_next_runnable()
    assert claimed.id == task.id and claimed.session_id == "keep-session"
    assert isolated_db.get_next_runnable() is None


def test_stale_retry_cannot_override_a_new_quota_window(isolated_db):
    old = waiting(isolated_db)
    assert retry(old).status_code == 200
    task = isolated_db.get_next_runnable()
    isolated_db.mark_rate_limited(task.id, old.next_run_at + timedelta(hours=5))
    newer = isolated_db.get_task(task.id)
    assert retry(old).status_code == 409
    assert isolated_db.get_task(task.id) == newer


@pytest.mark.parametrize("status", ["pending", "running", "completed", "failed", "cancelled"])
def test_retry_rejects_non_waiting_tasks(isolated_db, status):
    task = waiting(isolated_db)
    with isolated_db._connect() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task.id))
    before = isolated_db.get_task(task.id)
    assert retry(task).status_code == 409
    assert isolated_db.get_task(task.id) == before


def test_retry_requires_observed_deadline_and_existing_task(isolated_db):
    task = waiting(isolated_db)
    assert request("POST", f"/api/tasks/{task.id}/retry", json={}).status_code == 422
    assert request("POST", "/api/tasks/999999/retry", json={"expected_next_run_at": None}).status_code == 404
    assert isolated_db.get_task(task.id) == task


def test_concurrent_clicks_enqueue_only_once(isolated_db):
    task = waiting(isolated_db)

    def click():
        try:
            isolated_db.retry_rate_limited_task(task.id, task.next_run_at)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: click(), range(2))) == [False, True]
    assert len(isolated_db.list_tasks()) == 1
    assert isolated_db.get_next_runnable().id == task.id
    assert isolated_db.get_next_runnable() is None


def test_retry_does_not_resume_global_pause_or_bypass_maintenance(isolated_db):
    from promptpilot import maintenance

    task = waiting(isolated_db)
    isolated_db.set_setting("worker_paused", "1")
    maintenance.begin("retry-test")
    response = retry(task)
    assert response.status_code == 200
    assert response.json()["worker_paused"] is True
    assert isolated_db.is_paused()
    assert isolated_db.get_next_runnable() is None
    maintenance.finish("retry-test")
    assert isolated_db.is_paused()


def test_retry_does_not_resume_a_paused_series(isolated_db):
    created = isolated_db.create_task(TaskCreate(prompt="Recurring", recurrence="6h"))
    task = waiting(isolated_db, isolated_db.get_next_runnable())
    isolated_db.series_action(created.series_id, "pause")
    assert retry(task).status_code == 409
    assert isolated_db.get_task(task.id) == task


def test_workflow_retry_finishes_s1_and_keeps_pause_before_s2(isolated_db):
    wf = start_two_stages(isolated_db)
    paused = pause_after(isolated_db, wf)
    task = waiting(isolated_db, isolated_db.get_next_runnable())
    workflows.sync_task(task.id)
    before = isolated_db.get_workflow(wf.id)
    assert retry(task).status_code == 200
    assert isolated_db.get_workflow(wf.id) == before
    assert len(isolated_db.list_workflow_rounds(wf.id)) == 1
    assert finish_task(isolated_db, "S1 готов").id == task.id
    finish_task(isolated_db, reviewer_pass())
    after = isolated_db.get_workflow(wf.id)
    assert after.status.value == "paused_between_stages"
    assert after.pause_after_stage_id == paused.pause_after_stage_id
    assert [s.status.value for s in isolated_db.list_workflow_stages(wf.id)] == ["completed", "pending"]
    assert sum(e.event_type == "run.retry_requested" for e in isolated_db.list_workflow_events(wf.id)) == 1


@pytest.mark.parametrize("change", ["stopped", "old_round", "superseded"])
def test_retry_refuses_inactive_workflow_attempt(isolated_db, change):
    wf = start_two_stages(isolated_db)
    task = waiting(isolated_db, isolated_db.get_next_runnable())
    with isolated_db._connect() as conn:
        if change == "stopped":
            conn.execute("UPDATE workflows SET status='awaiting_human' WHERE id=?", (wf.id,))
        elif change == "old_round":
            conn.execute("UPDATE workflows SET current_round=current_round+1 WHERE id=?", (wf.id,))
        else:
            conn.execute("UPDATE workflow_runs SET status='completed' WHERE task_id=?", (task.id,))
    assert retry(task).status_code == 409
    assert isolated_db.get_task(task.id) == task


def test_brief_uses_original_stage_not_prompt_preamble_or_current_stage(isolated_db):
    wf = start_two_stages(isolated_db)
    task = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    # A long implementation preamble is not a useful task title.
    prompt = "Прочитать AGENTS.md. " * 300
    with isolated_db._connect() as conn:
        conn.execute("UPDATE tasks SET prompt=? WHERE id=?", (prompt, task.id))
        conn.execute("UPDATE workflow_stages SET title=?, objective=? WHERE id=?", (
            "Проверить окружение", "Подтвердить готовность окружения к измерениям.", wf.current_stage_id,
        ))
    task = isolated_db.get_task(task.id)
    brief = request("GET", f"/api/tasks/{task.id}").json()["brief"]
    assert brief["title"] == "S1 · Проверить окружение"
    assert brief["goal"] == "Подтвердить готовность окружения к измерениям."
    assert "Исполнитель" in brief["context"]
    finish_task(isolated_db, "Ready")
    reviewer = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert "Независимо проверить" in task_briefs.for_tasks([reviewer])[reviewer.id]["goal"]
    finish_task(isolated_db, reviewer_pass())
    assert isolated_db.get_workflow(wf.id).current_stage_id != wf.current_stage_id
    cards = request("GET", "/api/tasks").json()
    assert next(t for t in cards if t["id"] == task.id)["brief"] == brief
    assert isolated_db.get_task(task.id).prompt == prompt


def test_regular_briefs_prefer_labeled_goal_and_bound_long_text(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt=(
        "Прочитать инструкции\nЦель: Уменьшить время загрузки отчёта.\n" + "Детали " * 1000)))
    brief = task_briefs.for_tasks([task])[task.id]
    assert brief["goal"] == "Уменьшить время загрузки отчёта."
    other = isolated_db.create_task(TaskCreate(prompt="Проверить " * 1000))
    brief = task_briefs.for_tasks([other])[other.id]
    assert len(brief["title"]) <= 100 and len(brief["goal"]) <= 300
