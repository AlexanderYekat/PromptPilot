"""Deployment must see gates after tasks have already left the running queue."""

import asyncio

import httpx
import pytest

from promptpilot import maintenance, workflows
from promptpilot.api import app
from promptpilot.models import TaskCreate, WorkflowCreate, WorkflowStartRequest


TOKEN = 'a' * 32


def test_active_gate_blocks_restart_even_without_running_tasks(isolated_db):
    with maintenance.gate_activity('workflow-1'):
        result = maintenance.begin(TOKEN)
        assert result['running_tasks'] == []
        assert not result['safe_to_restart']
        assert result['active_gates'][0]['workflow_id'] == 'workflow-1'
        with pytest.raises(maintenance.MaintenanceActive):
            with maintenance.gate_activity('workflow-2'):
                pytest.fail('new gate started during drain')
    assert maintenance.status()['safe_to_restart']


def test_drain_blocks_task_claim_and_restores_previous_pause(isolated_db):
    isolated_db.set_setting('worker_paused', '0')
    isolated_db.create_task(TaskCreate(prompt='pending task'))
    maintenance.begin(TOKEN)
    assert isolated_db.get_next_runnable() is None
    assert isolated_db.is_paused()
    with pytest.raises(isolated_db.WorkflowConflictError):
        maintenance.finish('b' * 32)
    maintenance.finish(TOKEN)
    assert not isolated_db.is_paused()
    task = isolated_db.get_next_runnable()
    assert task
    isolated_db.set_setting('worker_paused', '1')
    assert not maintenance.begin(TOKEN)['safe_to_restart']
    maintenance.finish(TOKEN)
    assert isolated_db.is_paused()


def test_timeout_release_does_not_cancel_gate_or_allow_restart(isolated_db):
    with maintenance.gate_activity('workflow-1'):
        maintenance.begin(TOKEN)
        maintenance.finish(TOKEN)
        assert maintenance.status()['active_gates']
        assert not maintenance.status()['safe_to_restart']
    assert not maintenance.status()['active_gates']


def test_gate_receipt_is_applied_before_activity_is_released(isolated_db, monkeypatch):
    wf = isolated_db.create_workflow(WorkflowCreate(
        slug='drain-receipt', objective='gate', repository_path=str(isolated_db.DB_DIR),
        candidate_branch='candidate'))

    def record(workflow_id, decision):
        assert not maintenance.begin(TOKEN)['safe_to_restart']
        assert maintenance.status()['active_gates'][0]['workflow_id'] == workflow_id
        return wf

    monkeypatch.setattr(workflows, '_run_gate_commands', lambda w: object())
    monkeypatch.setattr(workflows, 'record_gate', record)
    assert workflows.run_gate(wf) == wf
    assert maintenance.status()['safe_to_restart']


def test_drain_defers_automation_without_error_pause(isolated_db):
    wf = isolated_db.create_workflow(WorkflowCreate(
        slug='drain-defer', objective='gate', repository_path=str(isolated_db.DB_DIR),
        candidate_branch='candidate', config={'automation': {'enabled': True}}))
    workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))
    maintenance.begin(TOKEN)
    assert workflows.advance_workflow(wf.id).status.value == 'queued'
    assert not isolated_db.list_tasks()


def test_maintenance_api_is_token_scoped_and_blocks_resume(isolated_db):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/maintenance/begin', json={'token': TOKEN})
            assert response.status_code == 200
            assert response.json()['safe_to_restart']
            assert (await client.post('/api/worker/resume')).status_code == 409
            assert (await client.post('/api/maintenance/begin', json={'token': 'b' * 32})).status_code == 409
            assert (await client.post('/api/maintenance/finish', json={'token': TOKEN})).status_code == 200
    asyncio.run(scenario())
