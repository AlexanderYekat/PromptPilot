"""Real disposable Git repos and subprocesses; no model or external service calls."""
import json
import base64
import asyncio
import os
import sqlite3
import subprocess
import sys

import pytest
import httpx

from promptpilot import workflows, workflow_candidates as candidates
from promptpilot.models import (
    WorkflowCreate, WorkflowStartRequest, WorkflowRole, WorkflowHumanInput,
    WorkflowGateDecision, WorkflowUpdate, TaskCreate, WorkflowTaskDispatch,
)


def git(repo, *args):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'проект с пробелами'
    root.mkdir()
    git(root, 'init', '-b', 'feature/candidate')
    git(root, 'config', 'user.email', 'test@example.invalid')
    git(root, 'config', 'user.name', 'Test')
    (root / 'code.py').write_text('value = 1\n')
    (root / 'test_code.py').write_text('assert True\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'base')
    return root


def create(db, repo, slug='candidate', **config):
    cfg = {'candidate': {'enabled': True}, 'roles': {'executor': {'provider': None}, 'reviewer': {'provider': None}}}
    cfg.update(config)
    wf = db.create_workflow(WorkflowCreate(slug=slug, objective='implement', repository_path=str(repo),
                                         candidate_branch='feature/candidate', config=cfg))
    return workflows.start_workflow(wf.id, WorkflowStartRequest(expected_version=0))


def dispatch(db, wf, role=WorkflowRole.EXECUTOR):
    wf = db.get_workflow(wf.id)
    return workflows._dispatch_configured_role(wf, role)


def finish(db, task, text):
    db.mark_completed(task.id, text, exit_code=0)
    db.set_verdict(task.id, 'ГОТОВО')
    workflows.sync_task(task.id)


def handoff(db, wf, repo, change=True):
    result = dispatch(db, wf)
    task = db.get_next_runnable()
    assert task.id == result.task.id
    candidates.preflight_task(task)
    if change:
        (repo / 'code.py').write_text('value = 2\n')
        git(repo, 'add', 'code.py')
        git(repo, 'commit', '-m', 'implementation')
    sha = git(repo, 'rev-parse', 'HEAD')
    finish(db, task, 'CANDIDATE_JSON: ' + json.dumps({'candidate_revision': sha, 'no_changes': not change}))
    assert db.get_workflow(wf.id).status.value == 'gating'
    return task, candidates.list_candidates(wf.id)[-1]


def gate(db, wf):
    decision = workflows._run_gate_commands(db.get_workflow(wf.id))
    workflows.record_gate(wf.id, decision)
    return decision


def shell_python(script):
    if os.name == 'nt':
        encoded = base64.b64encode(script.encode()).decode()
        return "& '" + sys.executable.replace("'", "''") + "' -c \"exec(__import__('base64').b64decode('" + encoded + "'))\""
    import shlex
    return shlex.quote(sys.executable) + ' -c ' + shlex.quote(script)


def test_full_handoff_gate_auditor_acceptance(isolated_db, repo, monkeypatch):
    command = shell_python('import os,json; print(json.dumps({k:v for k,v in os.environ.items() if k.startswith("PROMPTPILOT_CANDIDATE_")}))')
    monkeypatch.setenv('PROMPTPILOT_CANDIDATE_SHA', 'stale-parent-sha')
    wf = create(isolated_db, repo, gate={'commands': [command]})
    task, candidate = handoff(isolated_db, wf, repo)
    decision = gate(isolated_db, wf)
    assert decision.verdict.value == 'PASS', decision.evidence
    assert candidate['candidate_revision'] in decision.evidence[0]
    assert candidate['candidate_id'] in decision.evidence[0]
    assert 'stale-parent-sha' not in decision.evidence[0]
    assert os.environ['PROMPTPILOT_CANDIDATE_SHA'] == 'stale-parent-sha'
    reviewer = dispatch(isolated_db, wf, WorkflowRole.REVIEWER)
    assert reviewer.run.input['candidate_id'] == candidate['candidate_id']
    assert candidate['candidate_revision'] in reviewer.task.prompt
    assert candidate['repository_path'] in reviewer.task.prompt
    candidates.preflight_task(reviewer.task)
    finish(isolated_db, reviewer.task, 'AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS')
    decision = workflows.parse_reviewer_report('AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS')
    decision.expected_version = isolated_db.get_workflow(wf.id).state_version
    assert workflows.record_review(wf.id, decision).status.value == 'completed'
    report = workflows.workflow_report(wf.id)
    assert report['candidates'] == [candidate]
    assert report['rounds'][0]['audit_sha'] == candidate['candidate_revision']
    workflows.sync_task(task.id)
    isolated_db.init_db()
    workflows.sync_all_tasks(wf.id)
    assert candidates.list_candidates(wf.id) == [candidate]
    with isolated_db._connect() as conn:
        assert not conn.execute('SELECT 1 FROM workflow_candidate_locks').fetchone()
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            conn.execute('UPDATE workflow_candidates SET payload_json=?', ('{}',))


@pytest.mark.parametrize('mutation', ['head', 'code', 'test', 'untracked', 'ignored', 'assume_unchanged'])
def test_changed_inputs_block_gate_before_process(isolated_db, repo, mutation):
    wf = create(isolated_db, repo, gate={'commands': [shell_python('raise RuntimeError("must not run")')]})
    handoff(isolated_db, wf, repo)
    if mutation == 'head':
        git(repo, 'commit', '--allow-empty', '-m', 'other commit')
    elif mutation in {'code', 'assume_unchanged'}:
        if mutation == 'assume_unchanged':
            git(repo, 'update-index', '--assume-unchanged', 'code.py')
        (repo / 'code.py').write_text('changed')
    elif mutation == 'test':
        (repo / 'test_code.py').write_text('changed')
    else:
        if mutation == 'ignored':
            (repo / '.git' / 'info' / 'exclude').write_text('rogue.py\n')
        (repo / 'rogue.py').write_text('changed')
    decision = gate(isolated_db, wf)
    assert decision.verdict.value == 'HUMAN_REQUIRED'
    assert not decision.evidence
    assert isolated_db.get_workflow(wf.id).status.value == 'awaiting_human'


@pytest.mark.parametrize('mutates_input', [False, True])
def test_gate_postcheck_and_explicit_output_directory(isolated_db, repo, mutates_input):
    script = ('from pathlib import Path; Path("code.py").write_text("broken")' if mutates_input else
              'from pathlib import Path; Path("results").mkdir(exist_ok=True); Path("results/log.txt").write_text("ok")')
    wf = create(isolated_db, repo, candidate={'enabled': True, 'output_directories': ['results']},
                gate={'commands': [shell_python(script)]})
    handoff(isolated_db, wf, repo)
    assert gate(isolated_db, wf).verdict.value == ('HUMAN_REQUIRED' if mutates_input else 'PASS')


@pytest.mark.parametrize('report', ['готово', 'CANDIDATE_JSON: {}', 'CANDIDATE_JSON: {"candidate_revision":"' + '0' * 40 + '"}',
                                  'CANDIDATE_JSON: not-json'])
def test_invalid_structured_handoff(isolated_db, repo, report):
    wf = create(isolated_db, repo)
    result = dispatch(isolated_db, wf)
    finish(isolated_db, result.task, report)
    assert isolated_db.get_workflow(wf.id).status.value == 'awaiting_human'
    assert not candidates.list_candidates(wf.id)
    assert isolated_db.list_workflow_events(wf.id)[-1].event_type == 'candidate.blocked'


def test_no_change_handoff_requires_explicit_flag(isolated_db, repo):
    wf = create(isolated_db, repo)
    result = dispatch(isolated_db, wf)
    finish(isolated_db, result.task, 'CANDIDATE_JSON: ' + json.dumps({'candidate_revision': git(repo, 'rev-parse', 'HEAD')}))
    assert isolated_db.get_workflow(wf.id).status.value == 'awaiting_human'
    assert not candidates.list_candidates(wf.id)


def test_user_document_inventory_preserved(isolated_db, repo):
    (repo / 'requirements.md').write_text('user input')
    wf = create(isolated_db, repo, candidate={'enabled': True, 'additional_inputs': ['requirements.md']})
    _, candidate = handoff(isolated_db, wf, repo, change=False)
    assert (repo / 'requirements.md').read_text() == 'user input'
    assert 'requirements.md' in candidate['additional_inputs']
    (repo / 'requirements.md').write_text('new requirement')
    assert gate(isolated_db, wf).verdict.value == 'HUMAN_REQUIRED'


def test_scope_branch_and_user_files_are_not_repaired(isolated_db, repo):
    wf = create(isolated_db, repo, stage={'allowed_paths': ['test_code.py']})
    result = dispatch(isolated_db, wf)
    (repo / 'code.py').write_text('outside scope')
    git(repo, 'add', 'code.py')
    git(repo, 'commit', '-m', 'outside')
    finish(isolated_db, result.task, 'CANDIDATE_JSON: ' + json.dumps({'candidate_revision': git(repo, 'rev-parse', 'HEAD')}))
    assert not candidates.list_candidates(wf.id)
    assert 'outside_scope' in isolated_db.list_workflow_events(wf.id)[-1].payload['reason']
    assert (repo / 'code.py').read_text() == 'outside scope'


def test_repository_reservation_blocks_other_tasks_and_workflows(isolated_db, repo):
    wf = create(isolated_db, repo)
    handoff(isolated_db, wf, repo)
    other = isolated_db.create_task(TaskCreate(prompt='other', working_dir=str(repo)))
    assert isolated_db.get_next_runnable() is None
    with pytest.raises(candidates.CandidateError, match='repository_busy'):
        candidates.preflight_task(other)
    second = create(isolated_db, repo, slug='second')
    with pytest.raises(candidates.CandidateError, match='repository_busy'):
        dispatch(isolated_db, second)
    different = isolated_db.create_task(TaskCreate(prompt='elsewhere', working_dir=str(repo.parent / 'elsewhere')))
    assert isolated_db.get_next_runnable().id == different.id
    gate(isolated_db, wf)
    reviewer = dispatch(isolated_db, wf, WorkflowRole.REVIEWER)
    assert isolated_db.get_next_runnable().id == reviewer.task.id


def test_same_head_other_checkout_is_rejected(isolated_db, repo):
    other = repo.parent / 'other checkout'
    git(repo, 'worktree', 'add', '-b', 'other', str(other))
    wf = create(isolated_db, repo)
    with pytest.raises(candidates.CandidateError, match='repository_mismatch'):
        workflows.dispatch_task(wf.id, WorkflowTaskDispatch(expected_version=wf.state_version,
            role='executor', prompt='run', working_dir=str(other)))
    handoff(isolated_db, wf, repo)
    gate(isolated_db, wf)
    with pytest.raises(candidates.CandidateError, match='repository_mismatch'):
        workflows.dispatch_task(wf.id, WorkflowTaskDispatch(expected_version=isolated_db.get_workflow(wf.id).state_version,
            role='reviewer', prompt='review', working_dir=str(other)))


def test_auditor_change_and_stale_completion_do_not_accept_new_candidate(isolated_db, repo):
    wf = create(isolated_db, repo)
    handoff(isolated_db, wf, repo)
    gate(isolated_db, wf)
    reviewer = dispatch(isolated_db, wf, WorkflowRole.REVIEWER)
    (repo / 'code.py').write_text('changed during audit')
    finish(isolated_db, reviewer.task, 'AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS')
    paused = isolated_db.get_workflow(wf.id)
    assert paused.status.value == 'awaiting_human'
    decision = workflows.parse_reviewer_report('AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS')
    decision.expected_version = paused.state_version
    with pytest.raises(candidates.CandidateError):
        workflows.record_review(wf.id, decision)
    git(repo, 'add', 'code.py')
    git(repo, 'commit', '-m', 'second')
    resumed = workflows.human_input(wf.id, WorkflowHumanInput(expected_version=paused.state_version, text='retry', resume=True))
    assert resumed.current_round == 2
    handoff(isolated_db, wf, repo, change=False)
    finish(isolated_db, reviewer.task, 'late response\nAUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS')
    assert isolated_db.get_workflow(wf.id).status.value == 'gating'
    assert len(candidates.list_candidates(wf.id)) == 2


def test_forged_manual_gate_pass_is_rejected(isolated_db, repo):
    wf = create(isolated_db, repo)
    _, candidate = handoff(isolated_db, wf, repo)
    with pytest.raises(candidates.CandidateError, match='receipt'):
        workflows.record_gate(wf.id, WorkflowGateDecision(expected_version=isolated_db.get_workflow(wf.id).state_version,
            verdict='PASS', candidate_id=candidate['candidate_id']))


def test_completed_gate_is_not_reexecuted_after_restart(isolated_db, repo):
    wf = create(isolated_db, repo)
    handoff(isolated_db, wf, repo)
    decision = workflows._run_gate_commands(isolated_db.get_workflow(wf.id))
    isolated_db.init_db()
    with pytest.raises(candidates.CandidateError, match='gate_already_started'):
        workflows._run_gate_commands(isolated_db.get_workflow(wf.id))
    workflows.record_gate(wf.id, decision)
    assert isolated_db.get_workflow(wf.id).status.value == 'reviewing'


def test_failure_without_stop_on_failure_never_becomes_pass(isolated_db, repo):
    wf = create(isolated_db, repo, gate={'commands': [shell_python('raise SystemExit(2)'), shell_python('print("ok")')], 'stop_on_failure': False})
    handoff(isolated_db, wf, repo)
    decision = gate(isolated_db, wf)
    assert decision.verdict.value == 'FAIL'
    assert len(decision.evidence) == 2


def test_policy_cannot_be_disabled_after_dispatch(isolated_db, repo):
    wf = create(isolated_db, repo)
    with pytest.raises(candidates.CandidateError, match='candidate_disabled'):
        candidates.readiness(type('Workflow', (), {'config': {'gate': {'commands': ['echo $PROMPTPILOT_CANDIDATE_SHA']}}})())
    with pytest.raises(isolated_db.WorkflowConflictError, match='policy'):
        isolated_db.update_workflow(wf.id, WorkflowUpdate(expected_version=wf.state_version, config={'candidate': {'enabled': False}}))


@pytest.mark.skipif(os.name != 'nt', reason='real PowerShell quoting on Windows')
def test_powershell_expected_revision_and_unicode_path(isolated_db, repo):
    (repo / 'check.py').write_text('import argparse,os\np=argparse.ArgumentParser()\np.add_argument("--expected-revision",required=True)\na=p.parse_args()\nassert len(a.expected_revision)==40\nassert a.expected_revision==os.environ["PROMPTPILOT_CANDIDATE_SHA"]\nassert os.path.samefile(os.getcwd(),os.environ["PROMPTPILOT_CANDIDATE_REPOSITORY"])\nprint(a.expected_revision)\n')
    git(repo, 'add', 'check.py')
    git(repo, 'commit', '-m', 'checker')
    command = "& '" + sys.executable + "' './check.py' --expected-revision \"$env:PROMPTPILOT_CANDIDATE_SHA\""
    wf = create(isolated_db, repo, gate={'commands': [command]})
    _, candidate = handoff(isolated_db, wf, repo)
    decision = gate(isolated_db, wf)
    assert decision.verdict.value == 'PASS', decision.summary
    assert candidate['candidate_revision'] in decision.evidence[0]


def test_external_handoff_uses_same_candidate_contract(isolated_db, repo):
    from promptpilot.models import WorkflowExternalResult
    wf = create(isolated_db, repo, stage={'execution_mode': 'external'})
    waiting = workflows.request_external(wf.id, wf.state_version)
    assignment = workflows.external_assignment(wf.id)
    assert 'CANDIDATE_JSON' in assignment.assignment
    assert not isolated_db.list_tasks()
    text = 'CANDIDATE_JSON: ' + json.dumps({'candidate_revision': git(repo, 'rev-parse', 'HEAD'), 'no_changes': True})
    handed = workflows.submit_external_result(wf.id, WorkflowExternalResult(expected_version=waiting.state_version, result=text))
    assert handed.status.value == 'gating'
    candidate = candidates.list_candidates(wf.id)[0]
    assert candidate['executor_task_id'] is None
    assert candidate['executor_run_id'] == assignment.run_id
    assert gate(isolated_db, wf).verdict.value == 'PASS'


def test_concurrent_workflows_have_independent_child_environments(isolated_db, repo, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    other = repo.parent / 'second repo'
    git(repo, 'clone', str(repo), str(other))
    git(other, 'config', 'user.name', 'Test')
    git(other, 'config', 'user.email', 'test@example.invalid')
    git(other, 'commit', '--allow-empty', '-m', 'different base')
    command = shell_python('import os; print(os.environ["PROMPTPILOT_CANDIDATE_SHA"]); print(os.environ["PROMPTPILOT_CANDIDATE_ID"])')
    first = create(isolated_db, repo, gate={'commands': [command]})
    second = create(isolated_db, other, slug='second', gate={'commands': [command]})
    _, a = handoff(isolated_db, first, repo, change=False)
    _, b = handoff(isolated_db, second, other, change=False)
    monkeypatch.setenv('PROMPTPILOT_CANDIDATE_SHA', 'stale')
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda wf: workflows._run_gate_commands(isolated_db.get_workflow(wf.id)), [first, second]))
    for decision, expected, other in [(decisions[0], a, b), (decisions[1], b, a)]:
        assert decision.verdict.value == 'PASS', decision.evidence
        assert expected['candidate_revision'] in decision.evidence[0]
        assert expected['candidate_id'] in decision.evidence[0]
        assert other['candidate_id'] not in decision.evidence[0]
    assert os.environ['PROMPTPILOT_CANDIDATE_SHA'] == 'stale'


def test_interrupted_gate_claim_survives_version_changes(isolated_db, repo):
    from promptpilot.models import WorkflowEventCreate
    wf = create(isolated_db, repo)
    _, candidate = handoff(isolated_db, wf, repo)
    isolated_db.append_workflow_event(WorkflowEventCreate(workflow_id=wf.id, round_id=candidate['round_id'],
        event_type='candidate.gate_started', idempotency_key='candidate.gate_started:' + candidate['candidate_id']))
    current = isolated_db.get_workflow(wf.id)
    isolated_db.update_workflow(wf.id, WorkflowUpdate(expected_version=current.state_version, config=current.config))
    isolated_db.init_db()
    with pytest.raises(candidates.CandidateError, match='gate_already_started'):
        workflows._run_gate_commands(isolated_db.get_workflow(wf.id))


def test_observed_integrity_failure_cannot_be_undone_by_restoring_files(isolated_db, repo):
    wf = create(isolated_db, repo)
    _, candidate = handoff(isolated_db, wf, repo)
    gate(isolated_db, wf)
    original = (repo / 'code.py').read_bytes()
    (repo / 'code.py').write_text('broken')
    with pytest.raises(candidates.CandidateError):
        dispatch(isolated_db, wf, WorkflowRole.REVIEWER)
    (repo / 'code.py').write_bytes(original)
    with pytest.raises(candidates.CandidateError, match='candidate_invalidated'):
        candidates.validate(candidate)


def test_worker_blocks_provider_before_auditor_start(isolated_db, repo, monkeypatch):
    from promptpilot import worker
    wf = create(isolated_db, repo)
    handoff(isolated_db, wf, repo)
    gate(isolated_db, wf)
    reviewer = dispatch(isolated_db, wf, WorkflowRole.REVIEWER)
    task = isolated_db.get_next_runnable()
    assert task.id == reviewer.task.id
    (repo / 'code.py').write_text('changed in queue')
    launched = []
    monkeypatch.setattr(worker, '_execute_task_inner', lambda *args: launched.append(args))
    monkeypatch.setattr(worker, '_recur_after_run', lambda *args: None)
    worker.execute_task(task)
    assert not launched
    assert isolated_db.get_task(task.id).status.value == 'failed'


def test_plan_candidate_variable_requires_opt_in_before_approval(isolated_db, repo):
    from promptpilot.models import WorkflowPlanReplace, WorkflowPlanApproval
    wf = isolated_db.create_workflow(WorkflowCreate(slug='plan', objective='test', repository_path=str(repo),
        candidate_branch='feature/candidate', config={}))
    with isolated_db._connect() as conn:
        conn.execute("UPDATE workflows SET status='awaiting_plan_approval' WHERE id=?", (wf.id,))
    workflows.replace_plan(wf.id, WorkflowPlanReplace(expected_version=0, stages=[{
        'code': 'FINAL', 'title': 'test', 'objective': 'test', 'stage_type': 'integration',
        'acceptance_gates': ['echo $env:PROMPTPILOT_CANDIDATE_SHA'],
    }]))
    version = isolated_db.get_workflow(wf.id).state_version
    with pytest.raises(candidates.CandidateError, match='candidate_disabled'):
        workflows.approve_plan(wf.id, WorkflowPlanApproval(expected_version=version))
    assert isolated_db.get_workflow(wf.id).status.value == 'awaiting_plan_approval'


def test_candidate_api_exports_and_manual_gate(isolated_db, repo):
    from promptpilot.api import app

    def request(method, path, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    wf = create(isolated_db, repo)
    _, candidate = handoff(isolated_db, wf, repo)
    assert request('GET', f'/api/workflows/{wf.id}/candidates').json() == [candidate]
    assert request('GET', f'/api/workflows/{wf.id}/candidate-readiness').json()['ready']
    response = request('POST', f'/api/workflows/{wf.id}/gate/run', json={'expected_version': isolated_db.get_workflow(wf.id).state_version})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'reviewing'
    assert request('GET', '/api/workflows/missing/candidates').status_code == 404


def test_output_directory_cannot_hide_untracked_source(repo):
    from promptpilot.models import WorkflowCandidateConfig
    (repo / 'results').mkdir()
    (repo / 'results' / 'hidden.py').write_text('implementation = True')
    with pytest.raises(candidates.CandidateError, match='untracked_inputs'):
        candidates.snapshot(str(repo), WorkflowCandidateConfig(enabled=True, output_directories=['results']))
