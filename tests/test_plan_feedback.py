import json

import pytest
from pydantic import ValidationError

from promptpilot import workflows
from promptpilot.models import (
    WorkflowCreate, WorkflowPlanApproval, WorkflowPlanDispatch, WorkflowPlanReplace,
)


def stage(title='Original', objective='Original objective'):
    return {'code': 'FINAL', 'title': title, 'objective': objective,
            'stage_type': 'integration', 'allowed_paths': ['src/**']}


def finish(db, result):
    task = db.get_next_runnable()
    assert task is not None
    db.mark_completed(task.id, result, exit_code=0)
    db.set_verdict(task.id, 'ГОТОВО')
    workflows.sync_planner_task(task.id)
    return task


def output(value):
    return 'WORKFLOW_PLAN_JSON_BEGIN\n' + json.dumps({'stages': [value]}) + '\nWORKFLOW_PLAN_JSON_END\nИТОГ: ГОТОВО'


def waiting(db):
    workflow = db.create_workflow(WorkflowCreate(
        slug='feedback', objective='Make the benchmark', repository_path=str(db.DB_DIR),
        candidate_branch='candidate', config={
            'planning': {'enabled': True, 'require_approval': False},
            'roles': {'planner': {'provider': 'codex', 'rights': 'read'},
                      'executor': {'provider': 'claude', 'rights': 'full'}},
        }))
    workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(expected_version=workflow.state_version))
    # A generated command forces the initial human approval even with auto-approve configured.
    finish(db, output({**stage(), 'acceptance_gates': ['git diff --check']}))
    return db.get_workflow(workflow.id)


def revision(db, workflow, **kwargs):
    return workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(
        expected_version=workflow.state_version,
        feedback='Use handoff.json; preserve manual edits',
        stages=[stage('Manual title', 'Edited in browser')], **kwargs))


def test_feedback_preserves_versions_and_forces_reapproval(isolated_db):
    db = isolated_db
    workflow = waiting(db)
    original = db.list_workflow_stages(workflow.id)[0]
    plan = revision(db, workflow)
    prompt = db.get_task(plan.planner_task_id).prompt
    assert 'Use handoff.json' in prompt and 'Manual title' in prompt
    assert 'Edited in browser' in prompt and '"provider": "claude"' in prompt
    assert '"rights": "full"' in prompt and workflow.repository_path in prompt
    assert db.list_workflow_stages(workflow.id)[0].title == 'Manual title'
    events = db.list_workflow_events(workflow.id, limit=1000)
    archive = events[-1].payload['plan_revision']
    assert archive['previous_stages'][0]['id'] == original.id
    assert archive['previous_stages'][0]['title'] == 'Original'
    assert archive['stages'][0]['title'] == 'Manual title'
    assert archive['previous_plan']['status'] == 'awaiting_approval'
    assert archive['previous_plan']['output']['result']
    assert archive['feedback'] == 'Use handoff.json; preserve manual edits'

    # Revised command-free plans must not start the executor automatically.
    finish(db, output(stage('Revised', 'Reviewed objective')))
    revised = db.get_workflow(workflow.id)
    assert revised.status.value == 'awaiting_plan_approval'
    assert revised.current_round == 0 and revised.current_stage_id is None
    assert db.get_next_runnable() is None
    assert db.list_workflow_stages(workflow.id)[0].title == 'Revised'
    report = workflows.workflow_report(workflow.id)
    assert any(e['payload'].get('plan_revision') for e in report['events'])


def test_revision_failure_preserves_editor_plan_and_retry_context(isolated_db):
    db = isolated_db
    workflow = waiting(db)
    revision(db, workflow)
    finish(db, 'Invalid plan output')
    stopped = db.get_workflow(workflow.id)
    assert stopped.status.value == 'awaiting_human'
    assert db.list_workflow_stages(workflow.id)[0].title == 'Manual title'
    plan = workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(
        expected_version=stopped.state_version, feedback='Try again with handoff.json',
        stages=[stage('Manual title', 'Edited in browser')]))
    assert 'Manual title' in db.get_task(plan.planner_task_id).prompt
    finish(db, output(stage('Fixed')))
    assert db.get_workflow(workflow.id).status.value == 'awaiting_plan_approval'


def test_feedback_stale_version_and_queue_failure_are_atomic(isolated_db, monkeypatch):
    db = isolated_db
    workflow = waiting(db)
    stages = db.list_workflow_stages(workflow.id)
    events = db.list_workflow_events(workflow.id)
    tasks = db.list_tasks()
    with pytest.raises(db.WorkflowConflictError):
        workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(
            expected_version=workflow.state_version - 1, feedback='stale', stages=[stage('Stale')]))
    assert db.list_workflow_stages(workflow.id) == stages
    assert db.list_tasks() == tasks

    def fail(*args, **kwargs):
        raise RuntimeError('cannot enqueue')

    monkeypatch.setattr(db, '_insert_task', fail)
    with pytest.raises(RuntimeError, match='cannot enqueue'):
        revision(db, workflow)
    assert db.list_workflow_stages(workflow.id) == stages
    assert db.list_workflow_events(workflow.id) == events
    assert db.get_workflow(workflow.id).state_version == workflow.state_version


@pytest.mark.parametrize('fields', [
    {'feedback': '   ', 'stages': [stage()]},
    {'feedback': 'Fix this'}, {'stages': [stage()]},
    {'feedback': 'Fix this', 'stages': [stage(), stage()]},
    {'feedback': 'Fix this', 'stages': [{**stage(), 'dependencies': ['MISSING']}]},
    {'feedback': 'Fix this', 'stages': [{**stage(), 'stage_type': 'implementation'}]},
])
def test_invalid_revision_request(fields):
    with pytest.raises(ValidationError):
        WorkflowPlanDispatch(expected_version=0, **fields)


def test_revision_cannot_run_after_approval(isolated_db):
    db = isolated_db
    workflow = waiting(db)
    approved = workflows.approve_plan(workflow.id, WorkflowPlanApproval(expected_version=workflow.state_version))
    tasks = db.list_tasks()
    with pytest.raises(db.WorkflowConflictError):
        revision(db, approved)
    assert db.list_tasks() == tasks


def test_legacy_regeneration_also_includes_current_plan(isolated_db):
    db = isolated_db
    workflow = waiting(db)
    workflows.replace_plan(workflow.id, WorkflowPlanReplace(
        expected_version=workflow.state_version, stages=[stage('Saved manually')]))
    fresh = db.get_workflow(workflow.id)
    plan = workflows.dispatch_planner(workflow.id, WorkflowPlanDispatch(expected_version=fresh.state_version))
    assert 'Saved manually' in db.get_task(plan.planner_task_id).prompt
    finish(db, output(stage('Replanned')))
    assert db.get_workflow(workflow.id).status.value == 'awaiting_plan_approval'
