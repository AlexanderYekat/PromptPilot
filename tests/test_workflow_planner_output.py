"""A usable plan from the planner is not lost; output directories are checked.

Planner answers are written by hand into an isolated database: no provider runs.
"""
import asyncio
import hashlib
import json
import subprocess

import httpx
import pytest

from promptpilot import api, db, workflows, workflow_candidates as candidates
from promptpilot.models import (
    WorkflowCreate,
    WorkflowPlanApproval,
    WorkflowPlanDispatch,
    WorkflowStageSpec,
    WorkflowStartRequest,
    WorkflowVersionRequest,
)

HUMAN = "НУЖЕН ЧЕЛОВЕК"


def stage(code="FINAL", title="Итог", **fields):
    return {"code": code, "title": title, "objective": "Проверить результат",
            "stage_type": "integration", **fields}


def answer(stages, verdict="ГОТОВО", text="Изменения: S1 сокращён.\nНерешённое: нужен доступ к данным.\n"):
    block = "" if stages is None else (
        "WORKFLOW_PLAN_JSON_BEGIN\n" + json.dumps({"stages": stages}, ensure_ascii=False)
        + "\nWORKFLOW_PLAN_JSON_END\n")
    return text + block + f"ИТОГ: {verdict}"


def create(isolated_db, require_approval=False, max_stages=3, slug="planner-output", **config):
    cfg = {
        "planning": {"enabled": True, "require_approval": require_approval,
                     "max_stages": max_stages},
        "automation": {"enabled": True},
        "roles": {"planner": {"provider": None}, "executor": {"provider": None},
                  "reviewer": {"provider": None}},
        "gate": {"commands": []},
    }
    cfg.update(config)
    return isolated_db.create_workflow(WorkflowCreate(
        slug=slug, objective="Подготовить замеры",
        repository_path=str(isolated_db.DB_DIR), candidate_branch="feature/bench", config=cfg))


def plan_task(isolated_db, workflow_id, result, verdict, **dispatch):
    workflow = isolated_db.get_workflow(workflow_id)
    plan = workflows.dispatch_planner(workflow_id, WorkflowPlanDispatch(
        expected_version=workflow.state_version, **dispatch))
    isolated_db.mark_completed(plan.planner_task_id, result, exit_code=0)
    isolated_db.set_verdict(plan.planner_task_id, verdict)
    return plan.planner_task_id


def settle(task_id):
    workflows.sync_planner_task(task_id)
    workflows.advance_linked_task(task_id)
    workflows.sync_all_tasks()


def waiting_for_approval(isolated_db, **config):
    """A first plan awaiting approval; its gate keeps it from auto-approval."""
    workflow = create(isolated_db, **config)
    task = plan_task(isolated_db, workflow.id,
                     answer([stage(title="Исходный", acceptance_gates=["git diff --check"])]), "ГОТОВО")
    settle(task)
    assert isolated_db.get_workflow(workflow.id).status.value == "awaiting_plan_approval"
    return workflow


def revise(isolated_db, workflow_id, result, verdict):
    return plan_task(isolated_db, workflow_id, result, verdict,
                     feedback="Сократите этапы", stages=[stage(title="Правка оператора")])


def snapshot(isolated_db, workflow_id):
    return (isolated_db.get_workflow(workflow_id), isolated_db.list_workflow_events(workflow_id, limit=1000),
            isolated_db.list_workflow_stages(workflow_id), isolated_db.get_workflow_plan(workflow_id),
            isolated_db.list_tasks())


def call(method, path, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=api.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8420") as client:
            return await client.request(method, path, **kwargs)
    return asyncio.run(run())


# --- 5.1: «НУЖЕН ЧЕЛОВЕК» with a usable plan -----------------------------------------

@pytest.mark.parametrize("revision", [True, False])
def test_plan_with_reservations_waits_for_a_person(isolated_db, revision):
    revised = [stage("S1", "Подготовка", stage_type="implementation"), stage(dependencies=["S1"])]
    if revision:
        workflow = waiting_for_approval(isolated_db)
        task = revise(isolated_db, workflow.id, answer(revised, HUMAN), HUMAN)
    else:
        workflow = create(isolated_db)
        task = plan_task(isolated_db, workflow.id, answer(revised, HUMAN), HUMAN)
    settle(task)
    workflows.advance_workflow(workflow.id)  # require_approval=false and no gates

    current = isolated_db.get_workflow(workflow.id)
    assert current.status.value == "awaiting_plan_approval"
    assert [s.code for s in isolated_db.list_workflow_stages(workflow.id)] == ["S1", "FINAL"]
    plan = isolated_db.get_workflow_plan(workflow.id)
    assert plan.status == "awaiting_approval"
    assert plan.output["manual_approval"] == "planner_verdict"
    assert plan.output["verdict"] == HUMAN
    event = isolated_db.list_workflow_events(workflow.id, limit=1000)[-1]
    assert event.event_type == "planner.completed"
    assert event.payload["task_id"] == task
    assert event.payload["verdict"] == HUMAN
    assert event.payload["output_sha256"] == plan.output_sha256
    assert isolated_db.get_next_runnable() is None  # nothing was approved or started


def test_revised_plan_is_never_auto_approved_by_automation(isolated_db):
    workflow = waiting_for_approval(isolated_db)
    settle(revise(isolated_db, workflow.id, answer([stage(title="Доработан")]), "ГОТОВО"))
    workflows.advance_workflow(workflow.id)
    assert isolated_db.get_workflow(workflow.id).status.value == "awaiting_plan_approval"
    assert isolated_db.get_next_runnable() is None


def test_first_plan_still_auto_approves_when_allowed(isolated_db):
    workflow = create(isolated_db)
    settle(plan_task(isolated_db, workflow.id, answer([stage()]), "ГОТОВО"))
    assert isolated_db.get_workflow(workflow.id).status.value == "executing"


# --- 5.2: other outcomes are handled as before -----------------------------------------

@pytest.mark.parametrize("result,verdict", [
    (answer(None, HUMAN), HUMAN),
    (answer([stage()], "НЕ СМОГ"), "НЕ СМОГ"),
    (answer([stage(f"S{i}", stage_type="implementation") for i in range(3)] + [stage()], HUMAN), HUMAN),
])
def test_other_unsuccessful_answers_keep_the_previous_stages(isolated_db, result, verdict):
    workflow = waiting_for_approval(isolated_db)
    task = revise(isolated_db, workflow.id, result, verdict)
    settle(task)

    assert isolated_db.get_workflow(workflow.id).status.value == "awaiting_human"
    assert [s.title for s in isolated_db.list_workflow_stages(workflow.id)] == ["Правка оператора"]
    plan = isolated_db.get_workflow_plan(workflow.id)
    assert plan.status == "failed"
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
        assert plan.output["failure"] == workflows._planner_failure(row)
    assert "manual_approval" not in plan.output
    event = isolated_db.list_workflow_events(workflow.id, limit=1000)[-1]
    assert event.event_type == "planner.unsuccessful_result"
    assert event.payload["verdict"] == verdict


# --- 5.3/5.4: a failure recorded by an older release -------------------------------------

def legacy_failure(isolated_db, result, verdict=HUMAN):
    """Project an answer exactly the way release 307d32c did: always a failure."""
    workflow = waiting_for_approval(isolated_db)
    task_id = revise(isolated_db, workflow.id, result, verdict)
    with db._connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        output = {"task_status": task["status"], "result": task["result"], "error": task["error"],
                  "exit_code": task["exit_code"], "verdict": task["verdict"],
                  "model_used": task["model_used"], "failure": workflows._planner_failure(task)}
        output_json = db._json_dump(output)
        output_sha = hashlib.sha256(output_json.encode("utf-8")).hexdigest()
        conn.execute("UPDATE workflow_plans SET status='failed', output_sha256=?, output_json=?, "
                     "updated_at=? WHERE workflow_id=?", (output_sha, output_json, db._now(), workflow.id))
        workflows._transition(conn, workflows._workflow_row(conn, workflow.id),
                              workflows.WorkflowStatus.AWAITING_HUMAN, "planner.unsuccessful_result",
                              {"task_id": task_id, "output_sha256": output_sha, "task_status": "completed",
                               "error": None, "exit_code": 0, **output["failure"]})
    return workflow, task_id


REVISED = [stage("S1", "Замер", stage_type="implementation"), stage(dependencies=["S1"])]


def test_resync_after_upgrade_changes_nothing_and_old_data_reads(isolated_db):
    workflow, task_id = legacy_failure(isolated_db, answer(REVISED, HUMAN))
    before = snapshot(isolated_db, workflow.id)

    workflows.sync_planner_task(task_id)
    workflows.sync_all_tasks()
    workflows.sync_all_tasks(workflow.id)
    workflows.advance_workflow(workflow.id)

    assert snapshot(isolated_db, workflow.id) == before
    response = call("GET", f"/api/workflows/{workflow.id}/plan")
    assert response.status_code == 200
    hint = response.json()["planner_output_plan"]
    assert hint["applicable"] is True and hint["task_id"] == task_id
    assert hint["stage_codes"] == ["S1", "FINAL"] and hint["verdict"] == HUMAN
    assert call("GET", f"/api/workflows/{workflow.id}/stages").json()[0]["title"] == "Правка оператора"


def test_apply_planner_output_puts_the_plan_up_for_approval(isolated_db):
    workflow, task_id = legacy_failure(isolated_db, answer(REVISED, HUMAN))
    stopped = isolated_db.get_workflow(workflow.id)
    previous_sha = isolated_db.get_workflow_plan(workflow.id).output_sha256

    response = call("POST", f"/api/workflows/{workflow.id}/plan/apply-planner-output",
                    json={"expected_version": stopped.state_version})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_plan_approval"

    stages = isolated_db.list_workflow_stages(workflow.id)
    assert [(s.code, s.title) for s in stages] == [("S1", "Замер"), ("FINAL", "Итог")]
    plan = isolated_db.get_workflow_plan(workflow.id)
    assert plan.status == "awaiting_approval" and plan.planner_task_id == task_id
    assert plan.output["manual_approval"] == "planner_output_applied"
    assert plan.output["previous_failure"]["code"] == "unsuccessful_result"
    event = isolated_db.list_workflow_events(workflow.id, limit=1000)[-1]
    assert event.event_type == "planner.output_applied"
    assert event.payload["task_id"] == task_id and event.payload["verdict"] == HUMAN
    assert event.payload["output_sha256"] == plan.output_sha256
    assert event.payload["previous_output_sha256"] == previous_sha
    assert event.payload["from"] == "awaiting_human"

    # Neither automation nor a repeated sync approves or reverts it.
    workflows.sync_all_tasks()
    workflows.advance_workflow(workflow.id)
    assert isolated_db.get_workflow(workflow.id).status.value == "awaiting_plan_approval"
    assert isolated_db.get_next_runnable() is None
    again = call("POST", f"/api/workflows/{workflow.id}/plan/apply-planner-output",
                 json={"expected_version": isolated_db.get_workflow(workflow.id).state_version})
    assert again.status_code == 409


@pytest.mark.parametrize("result,message", [
    (answer(None, HUMAN), "no WORKFLOW_PLAN_JSON block"),
    ("WORKFLOW_PLAN_JSON_BEGIN\n{broken}\nWORKFLOW_PLAN_JSON_END\nИТОГ: НУЖЕН ЧЕЛОВЕК", "no valid stage plan"),
    (answer([stage(f"S{i}", stage_type="implementation") for i in range(3)] + [stage()], HUMAN),
     "max_stages=3"),
])
def test_apply_refuses_an_answer_without_a_usable_plan(isolated_db, result, message):
    workflow, _ = legacy_failure(isolated_db, result)
    before = snapshot(isolated_db, workflow.id)
    with pytest.raises(db.WorkflowConflictError, match=message):
        workflows.apply_planner_output(workflow.id, WorkflowVersionRequest(
            expected_version=before[0].state_version))
    assert snapshot(isolated_db, workflow.id) == before
    assert call("GET", f"/api/workflows/{workflow.id}/plan").json()["planner_output_plan"]["applicable"] is False


def test_apply_refuses_stale_version_wrong_status_and_started_workflow(isolated_db):
    workflow, _ = legacy_failure(isolated_db, answer(REVISED, HUMAN))
    stopped = isolated_db.get_workflow(workflow.id)
    with pytest.raises(db.WorkflowConflictError, match="expected"):
        workflows.apply_planner_output(workflow.id, WorkflowVersionRequest(
            expected_version=stopped.state_version - 1))

    waiting = waiting_for_approval(isolated_db, slug="second")
    current = isolated_db.get_workflow(waiting.id)
    with pytest.raises(db.WorkflowConflictError, match="awaits a human"):
        workflows.apply_planner_output(waiting.id, WorkflowVersionRequest(
            expected_version=current.state_version))

    # After execution started the planner answer is history, not a plan.
    approved = workflows.approve_plan(waiting.id, WorkflowPlanApproval(
        expected_version=current.state_version))
    with db._connect() as conn:
        workflows._transition(conn, workflows._workflow_row(conn, waiting.id),
                              workflows.WorkflowStatus.AWAITING_HUMAN, "automation.paused",
                              {"reason": "test"})
        conn.execute("UPDATE workflow_plans SET status='failed' WHERE workflow_id=?", (waiting.id,))
    paused = isolated_db.get_workflow(waiting.id)
    assert paused.current_round == approved.current_round == 1
    with pytest.raises(db.WorkflowConflictError, match="before stage execution"):
        workflows.apply_planner_output(waiting.id, WorkflowVersionRequest(
            expected_version=paused.state_version))
    assert call("POST", f"/api/workflows/{waiting.id}/plan/apply-planner-output",
                json={"expected_version": paused.state_version}).status_code == 409


def test_planner_is_told_stage_prompts_only_supplement_the_role(isolated_db):
    workflow = waiting_for_approval(isolated_db)
    assert workflows.STAGE_PROMPTS_NOTE in workflows.DEFAULT_PLANNER_PROMPT
    task = revise(isolated_db, workflow.id, answer([stage()]), "ГОТОВО")
    assert isolated_db.get_task(task).prompt.count(workflows.STAGE_PROMPTS_NOTE) == 2


# --- 6: allowed_paths against output directories ----------------------------------------

OUTPUTS = ["results/verification/simple", "results/runs/simple", ".pytest_cache"]


def candidate_workflow(stage_config=None):
    config = {"candidate": {"enabled": True, "output_directories": OUTPUTS},
              "roles": {"executor": {"provider": None}, "reviewer": {"provider": None}},
              "stage": stage_config or {}}
    return type("Workflow", (), {"config": config, "repository_path": "."})()


@pytest.mark.parametrize("entry", [
    "results/verification/simple", "results/verification/simple/", "results/verification/simple/**",
    "results/**", "results", "results/verification/simple/run.json", "results/*/simple/**",
    "results/verification/sim*", "./results/verification/simple/x.txt",
    "results\\verification\\simple\\x.txt", "**/*.json", ".pytest_cache/v/cache",
])
def test_allowed_path_reaching_an_output_directory_is_rejected(entry):
    spec = WorkflowStageSpec(code="S1", title="t", objective="o", allowed_paths=["src/**", entry])
    with pytest.raises(candidates.CandidateError, match="output_path_conflict") as caught:
        candidates.readiness(candidate_workflow(), [spec], check_output_paths=True)
    assert f"Этап S1: путь {entry} пересекается с выходным каталогом" in str(caught.value)
    candidates.readiness(candidate_workflow(), [spec])  # in-flight checks are unchanged


@pytest.mark.parametrize("entry", [
    "src/**", "results/summary.md", "results/verification/simple-notes/**",
    "results/verification/simpler.md", "docs/results/verification/simple/**", "results/runs/other/*",
])
def test_paths_outside_output_directories_pass(entry):
    spec = WorkflowStageSpec(code="S1", title="t", objective="o", allowed_paths=[entry])
    candidates.readiness(candidate_workflow(), [spec], check_output_paths=True)


def test_conflict_message_and_disabled_candidate():
    spec = WorkflowStageSpec(code="S1", title="t", objective="o",
                             allowed_paths=["results/verification/simple/**"])
    with pytest.raises(candidates.CandidateError) as caught:
        candidates.readiness(candidate_workflow(), [spec], check_output_paths=True)
    assert str(caught.value) == (
        "output_path_conflict: Этап S1: путь results/verification/simple/** пересекается с "
        "выходным каталогом results/verification/simple. Выходные каталоги не коммитятся — "
        "уберите путь из allowed_paths")
    disabled = candidate_workflow()
    disabled.config = {**disabled.config, "candidate": {"enabled": False, "output_directories": OUTPUTS}}
    candidates.readiness(disabled, [spec], check_output_paths=True)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                            encoding="utf-8")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "bench"
    root.mkdir()
    git(root, "init", "-b", "feature/bench")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text("/results/runs/*\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    return root


def test_approval_and_readiness_report_the_conflict(isolated_db, repo):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="bench", objective="Замеры", repository_path=str(repo), candidate_branch="feature/bench",
        config={"planning": {"enabled": True}, "candidate": {"enabled": True, "output_directories": OUTPUTS},
                "roles": {"planner": {"provider": None}, "executor": {"provider": None},
                          "reviewer": {"provider": None}}}))
    task = plan_task(isolated_db, workflow.id, answer([
        stage("S1", "Замер", stage_type="implementation",
              allowed_paths=["bench/**", "results/verification/simple/**"]),
        stage(dependencies=["S1"], allowed_paths=["results/runs/simple/**", "results/comparisons/simple/**"]),
    ]), "ГОТОВО")
    settle(task)
    waiting = isolated_db.get_workflow(workflow.id)
    assert waiting.status.value == "awaiting_plan_approval"

    readiness = call("GET", f"/api/workflows/{workflow.id}/candidate-readiness").json()
    assert readiness["ready"] is False and readiness["code"] == "output_path_conflict"
    assert "Этап S1: путь results/verification/simple/**" in readiness["reason"]
    assert "Этап FINAL: путь results/runs/simple/**" in readiness["reason"]
    assert "results/comparisons/simple/**" not in readiness["reason"]
    assert "уберите эти пути" in readiness["reason"]
    # Advisory: only the directory Git does not ignore is reported.
    assert readiness["warnings"] == [
        "Выходной каталог results/verification/simple не исключён в .gitignore: "
        "git add -A добавит результаты проверок в коммит",
        "Выходной каталог .pytest_cache не исключён в .gitignore: "
        "git add -A добавит результаты проверок в коммит",
    ]

    response = call("POST", f"/api/workflows/{workflow.id}/plan/approve",
                    json={"expected_version": waiting.state_version})
    assert response.status_code == 409
    assert response.json()["detail"] == readiness["reason"]
    assert isolated_db.get_workflow(workflow.id).status.value == "awaiting_plan_approval"


def test_planless_start_checks_the_configured_stage(isolated_db, repo):
    workflow = isolated_db.create_workflow(WorkflowCreate(
        slug="planless", objective="Замеры", repository_path=str(repo), candidate_branch="feature/bench",
        config={"candidate": {"enabled": True, "output_directories": OUTPUTS},
                "roles": {"executor": {"provider": None}, "reviewer": {"provider": None}},
                "stage": {"allowed_paths": ["results/**"]}}))
    with pytest.raises(candidates.CandidateError, match="Этап: путь results/\\*\\* пересекается"):
        workflows.start_workflow(workflow.id, WorkflowStartRequest(expected_version=0))
    assert isolated_db.get_workflow(workflow.id).status.value == "draft"
