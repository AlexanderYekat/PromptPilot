import asyncio
import json

import httpx
import pytest

from promptpilot import workflows
from promptpilot.api import app
from promptpilot.models import (
    TaskStatus,
    WorkflowCreate,
    WorkflowPlanApproval,
    WorkflowPlanDispatch,
    WorkflowStagePauseRequest,
    WorkflowVersionRequest,
)


def config(max_revisions=3, max_rounds=10, require_approval=True):
    return {
        "planning": {
            "enabled": True,
            "require_approval": require_approval,
            "max_stages": 10,
            "max_revisions_per_stage": max_revisions,
        },
        "automation": {"enabled": True},
        "roles": {
            "planner": {"provider": None},
            "executor": {"provider": None},
            "reviewer": {"provider": None},
        },
        "gate": {"commands": []},
        "limits": {"max_rounds": max_rounds},
    }


def create(isolated_db, slug="planned", **kwargs):
    return isolated_db.create_workflow(WorkflowCreate(
        slug=slug,
        objective="Сделать сложную задачу маленькими этапами",
        repository_path=str(isolated_db.DB_DIR),
        candidate_branch="feature/planned",
        config=config(**kwargs),
    ))


def plan_result(stages):
    return (
        "WORKFLOW_PLAN_JSON_BEGIN\n"
        + json.dumps({"stages": stages}, ensure_ascii=False)
        + "\nWORKFLOW_PLAN_JSON_END\n"
        + "ИТОГ: ГОТОВО — план сформирован"
    )


def finish_task(isolated_db, result):
    task = isolated_db.get_next_runnable()
    assert task is not None
    workflows.sync_task(task.id)
    isolated_db.mark_completed(task.id, result, exit_code=0)
    isolated_db.set_verdict(task.id, "ГОТОВО")
    workflows.sync_task(task.id)
    workflows.advance_linked_task(task.id)
    return task


def reviewer_pass():
    return (
        "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS\n"
        "ИТОГ: ГОТОВО — этап принят"
    )


def reviewer_revision():
    return (
        'AUDIT_FINDINGS_JSON: [{"fingerprint":"needs-fix","severity":"medium",'
        '"category":"test","title":"Нужно исправление"}]\n'
        "AUDIT_VERDICT: REVISION_REQUIRED\n"
        "ИТОГ: ГОТОВО — нужны исправления"
    )


def start_two_stages(db):
    wf = create(db)
    workflows.dispatch_planner(wf.id, WorkflowPlanDispatch(expected_version=wf.state_version))
    finish_task(db, plan_result([
        {'code': 'S1', 'title': 'First', 'objective': 'First'},
        {'code': 'S2', 'title': 'Second', 'objective': 'Second', 'stage_type': 'integration'},
    ]))
    wf = db.get_workflow(wf.id)
    workflows.approve_plan(wf.id, WorkflowPlanApproval(expected_version=wf.state_version))
    return workflows.advance_workflow(wf.id)


def pause_after(db, wf, enabled=True):
    wf = db.get_workflow(wf.id)
    return workflows.set_stage_pause(wf.id, WorkflowStagePauseRequest(
        expected_version=wf.state_version, expected_pause_version=wf.pause_version,
        enabled=enabled))


def test_stage_pause_finishes_audit_and_survives_sync_until_explicit_resume(isolated_db):
    wf = start_two_stages(isolated_db)
    paused = pause_after(isolated_db, wf)
    assert paused.state_version == wf.state_version
    finish_task(isolated_db, 'S1 executor')
    assert isolated_db.get_workflow(wf.id).status.value == 'reviewing'
    finish_task(isolated_db, reviewer_pass())
    waiting = isolated_db.get_workflow(wf.id)
    assert waiting.status.value == 'paused_between_stages'
    assert waiting.current_stage_id == wf.current_stage_id
    assert waiting.current_round == 1
    assert [s.status.value for s in isolated_db.list_workflow_stages(wf.id)] == ['completed', 'pending']
    for _ in range(2):
        workflows.sync_all_tasks()
        workflows.advance_workflow(wf.id)
    assert not isolated_db.list_tasks(status=TaskStatus.PENDING)
    with pytest.raises(isolated_db.WorkflowConflictError):
        workflows.resume_next_stage(wf.id, WorkflowVersionRequest(expected_version=0))
    resumed = workflows.resume_next_stage(wf.id, WorkflowVersionRequest(expected_version=waiting.state_version))
    assert resumed.current_round == 2
    assert resumed.pause_after_stage_id is None
    workflows.advance_workflow(wf.id)
    assert len(isolated_db.list_tasks(status=TaskStatus.PENDING)) == 1
    with pytest.raises(isolated_db.WorkflowConflictError):
        workflows.resume_next_stage(wf.id, WorkflowVersionRequest(expected_version=waiting.state_version))


def test_stage_pause_allows_revision_rounds_and_cancel_before_completion(isolated_db):
    wf = start_two_stages(isolated_db)
    pause_after(isolated_db, wf)
    finish_task(isolated_db, 'executor')
    finish_task(isolated_db, reviewer_revision())
    revising = isolated_db.get_workflow(wf.id)
    assert revising.status.value == 'executing'
    assert revising.current_stage_id == wf.current_stage_id
    assert revising.pause_after_stage_id == wf.current_stage_id
    pause_after(isolated_db, revising, False)
    finish_task(isolated_db, 'corrected executor')
    finish_task(isolated_db, reviewer_pass())
    assert isolated_db.get_workflow(wf.id).current_stage_id != wf.current_stage_id


def test_pause_during_gate_does_not_invalidate_receipt(isolated_db, monkeypatch):
    wf = start_two_stages(isolated_db)
    run_gate = workflows._run_gate_commands

    def with_pause(workflow):
        pause_after(isolated_db, workflow)
        return run_gate(workflow)

    monkeypatch.setattr(workflows, '_run_gate_commands', with_pause)
    finish_task(isolated_db, 'executor')
    assert isolated_db.get_workflow(wf.id).status.value == 'reviewing'
    finish_task(isolated_db, reviewer_pass())
    assert isolated_db.get_workflow(wf.id).status.value == 'paused_between_stages'


def test_pause_request_is_concurrent_safe_and_last_stage_finishes(isolated_db):
    wf = start_two_stages(isolated_db)
    requested = pause_after(isolated_db, wf)
    with pytest.raises(isolated_db.WorkflowConflictError, match='pause changed'):
        workflows.set_stage_pause(wf.id, WorkflowStagePauseRequest(
            expected_version=wf.state_version, expected_pause_version=0, enabled=False))
    pause_after(isolated_db, requested, False)
    finish_task(isolated_db, 'executor S1')
    finish_task(isolated_db, reviewer_pass())
    pause_after(isolated_db, isolated_db.get_workflow(wf.id))
    finish_task(isolated_db, 'executor S2')
    finish_task(isolated_db, reviewer_pass())
    assert isolated_db.get_workflow(wf.id).status.value == 'completed'


def test_stage_pause_api_only_sets_intent_and_next_stage_requires_acceptance(isolated_db):
    wf = start_two_stages(isolated_db)
    tasks_before = [t.id for t in isolated_db.list_tasks()]

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/workflows/{wf.id}/stage-pause', json={
                'expected_version': wf.state_version, 'expected_pause_version': 0, 'enabled': True})
            assert response.status_code == 200
            assert response.json()['status'] == 'executing'
            assert response.json()['state_version'] == wf.state_version
            assert response.json()['pause_after_stage_id'] == wf.current_stage_id
            assert (await client.post(f'/api/workflows/{wf.id}/next-stage', json={
                'expected_version': wf.state_version})).status_code == 409
    asyncio.run(scenario())
    assert [t.id for t in isolated_db.list_tasks()] == tasks_before


def test_planner_approval_and_two_stage_autonomous_lifecycle(isolated_db):
    workflow = create(isolated_db)
    plan = workflows.dispatch_planner(
        workflow.id,
        WorkflowPlanDispatch(expected_version=workflow.state_version),
    )
    assert plan.status == "planning"
    assert isolated_db.get_workflow(workflow.id).status.value == "planning"

    finish_task(isolated_db, plan_result([
        {
            "code": "S1", "title": "Контракт", "objective": "Зафиксировать контракт",
            "allowed_paths": ["promptpilot/models.py"],
            "deliverables": ["schema"], "acceptance_gates": [],
        },
        {
            "code": "FINAL", "title": "Интеграция",
            "objective": "Проверить результат целиком", "stage_type": "integration",
            "dependencies": ["S1"], "acceptance_gates": [],
        },
    ]))
    waiting = isolated_db.get_workflow(workflow.id)
    assert waiting.status.value == "awaiting_plan_approval"
    stages = isolated_db.list_workflow_stages(workflow.id)
    assert [stage.code for stage in stages] == ["S1", "FINAL"]
    assert all(stage.status.value == "draft" for stage in stages)

    approved = workflows.approve_plan(
        workflow.id,
        WorkflowPlanApproval(expected_version=waiting.state_version),
    )
    assert approved.status.value == "queued"
    assert approved.current_stage_id == stages[0].id
    workflows.advance_workflow(workflow.id)

    executor = isolated_db.list_tasks(status=TaskStatus.PENDING)[0]
    assert "Текущий этап: S1 Контракт" in executor.prompt
    assert "promptpilot/models.py" in executor.prompt
    finish_task(isolated_db, "executor S1")
    finish_task(isolated_db, reviewer_pass())

    second = isolated_db.get_workflow(workflow.id)
    assert second.status.value == "executing"
    assert second.current_round == 2
    current_stage = isolated_db.get_workflow_stage(second.current_stage_id)
    assert current_stage.code == "FINAL"
    assert isolated_db.list_workflow_stages(workflow.id)[0].status.value == "completed"

    finish_task(isolated_db, "executor FINAL")
    finish_task(isolated_db, reviewer_pass())
    completed = isolated_db.get_workflow(workflow.id)
    assert completed.status.value == "completed"
    assert all(
        stage.status.value == "completed"
        for stage in isolated_db.list_workflow_stages(workflow.id)
    )
    assert [
        event.event_type
        for event in isolated_db.list_workflow_events(workflow.id, limit=1000)
    ].count("stage.completed") == 2
    report = workflows.workflow_report(workflow.id)
    assert report["schema_version"] == 2
    assert report["metrics"]["planner_attempts"] == 1
    assert report["metrics"]["stages_by_status"] == {"completed": 2}
    assert [item["code"] for item in report["stages"]] == ["S1", "FINAL"]
    assert "## Stage plan" in workflows.workflow_report_markdown(workflow.id)


def test_next_stage_executor_gets_no_report_of_the_previous_stage(isolated_db):
    workflow = create(isolated_db, slug="stage-handoff")
    workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version))
    finish_task(isolated_db, plan_result([
        {"code": "S1", "title": "Контракт", "objective": "Зафиксировать контракт",
         "acceptance_gates": []},
        {"code": "FINAL", "title": "Интеграция", "objective": "Проверить результат целиком",
         "stage_type": "integration", "dependencies": ["S1"], "acceptance_gates": []},
    ]))
    waiting = isolated_db.get_workflow(workflow.id)
    workflows.approve_plan(
        workflow.id, WorkflowPlanApproval(expected_version=waiting.state_version))
    workflows.advance_workflow(workflow.id)
    finish_task(isolated_db, "S1-ONLY-REPORT: правил только контракт")
    finish_task(isolated_db, reviewer_pass())

    final = isolated_db.list_tasks(status=TaskStatus.PENDING)[0].prompt

    assert "Текущий этап: FINAL" in final
    # Another stage is not a session to resume.
    assert "<предыдущий-запуск-исполнителя>" not in final
    assert "S1-ONLY-REPORT" not in final


def test_invalid_planner_output_stops_for_human(isolated_db):
    workflow = create(isolated_db, slug="invalid-plan")
    workflows.dispatch_planner(
        workflow.id,
        WorkflowPlanDispatch(expected_version=workflow.state_version),
    )
    finish_task(isolated_db, "План в свободном тексте")
    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    assert isolated_db.get_workflow_plan(workflow.id).status == "failed"
    assert isolated_db.list_workflow_stages(workflow.id) == []


def test_plan_parser_rejects_forward_dependency():
    report = plan_result([
        {"code": "S1", "title": "one", "objective": "one", "dependencies": ["S2"]},
        {"code": "S2", "title": "two", "objective": "two"},
    ])
    assert workflows.parse_workflow_plan(report) is None


def test_stage_revision_limit_stops_autonomous_ping_pong(isolated_db):
    workflow = create(isolated_db, slug="revision-limit", max_revisions=1)
    workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version)
    )
    finish_task(isolated_db, plan_result([
        {"code": "FINAL", "title": "Один этап", "objective": "Завершить этап",
         "stage_type": "integration"},
    ]))
    waiting = isolated_db.get_workflow(workflow.id)
    approved = workflows.approve_plan(
        workflow.id, WorkflowPlanApproval(expected_version=waiting.state_version)
    )
    workflows.advance_workflow(approved.id)

    finish_task(isolated_db, "executor report")
    finish_task(isolated_db, reviewer_revision())

    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    assert stopped.current_round == 1
    assert isolated_db.list_workflow_stages(workflow.id)[0].status.value == "executing"
    events = isolated_db.list_workflow_events(workflow.id, limit=1000)
    assert events[-1].event_type == "limit.stage_revisions"
    assert events[-1].payload["max_revision_rounds"] == 1


def test_plan_requires_final_integration_stage():
    report = plan_result([
        {"code": "S1", "title": "only", "objective": "only"},
    ])
    assert workflows.parse_workflow_plan(report) is None


def test_command_free_plan_can_auto_approve_and_dispatch(isolated_db):
    workflow = create(
        isolated_db, slug="auto-plan", require_approval=False
    )
    plan = workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version)
    )
    finish_task(isolated_db, plan_result([{
        "code": "FINAL", "title": "Integration", "objective": "Check all",
        "stage_type": "integration", "acceptance_gates": [],
    }]))

    executing = isolated_db.get_workflow(workflow.id)
    assert executing.status.value == "executing"
    assert isolated_db.get_workflow_plan(workflow.id).status == "approved"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING)[0].id != plan.planner_task_id


def test_llm_gate_commands_force_human_plan_approval(isolated_db):
    workflow = create(
        isolated_db, slug="unsafe-auto-plan", require_approval=False
    )
    workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version)
    )
    finish_task(isolated_db, plan_result([{
        "code": "FINAL", "title": "Integration", "objective": "Check all",
        "stage_type": "integration", "acceptance_gates": ["pytest -q"],
    }]))

    waiting = isolated_db.get_workflow(workflow.id)
    assert waiting.status.value == "awaiting_plan_approval"
    assert isolated_db.get_workflow_plan(workflow.id).status == "awaiting_approval"
    assert isolated_db.list_tasks(status=TaskStatus.PENDING) == []


def test_planned_workflow_honors_global_round_budget(isolated_db):
    workflow = create(isolated_db, slug="plan-round-budget", max_rounds=1)
    workflows.dispatch_planner(
        workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version)
    )
    finish_task(isolated_db, plan_result([{
        "code": "FINAL", "title": "Integration", "objective": "Check all",
        "stage_type": "integration",
    }]))
    waiting = isolated_db.get_workflow(workflow.id)
    approved = workflows.approve_plan(
        workflow.id, WorkflowPlanApproval(expected_version=waiting.state_version)
    )
    workflows.advance_workflow(approved.id)
    finish_task(isolated_db, "executor report")
    finish_task(isolated_db, reviewer_revision())

    stopped = isolated_db.get_workflow(workflow.id)
    assert stopped.status.value == "awaiting_human"
    assert isolated_db.list_workflow_events(workflow.id, limit=1000)[-1].event_type == "limit.max_rounds"
