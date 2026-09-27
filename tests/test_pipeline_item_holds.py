import copy
from types import SimpleNamespace

from promptpilot import pipeline_item_holds as holds, pipeline_insights, project_pipeline as pp
from promptpilot.models import TaskCreate


def snapshot(*numbers):
    return {"cache": {"complete": True, "stale": False},
            "queues": [{"id": "triage", "membership_complete": True,
                        "backlog": len(numbers), "items": [
                {"number": n, "kind": "issue", "updated_at": "2026-09-27T12:00:00Z"} for n in numbers]}]}


def test_public_dispatch_parks_item_and_keeps_series_and_new_work(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(prompt="Example - TRIAGE", recurrence="15m"))
    queue = {"id": "triage", "series_contains": "Example - TRIAGE", "item_blockers": True,
             "dispatch_gate": {"skip_when_empty": True}}
    monkeypatch.setattr(pipeline_insights, "_profiles", lambda: {"example": {"queues": [queue]}})
    data = snapshot(7)
    monkeypatch.setattr(pipeline_insights, "read_cached", lambda *_: data)
    assert pipeline_insights.dispatch_gate(task) is None
    isolated_db.mark_completed(task.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК (#7 — invalid route)", verdict="НУЖЕН ЧЕЛОВЕК")
    state = isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
    assert not state["suppress_recurrence"]
    assert not isolated_db.list_series()[0]["paused"]
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id, prompt=task.prompt)
    assert pipeline_insights.dispatch_gate(successor)["action"] == "defer"
    data = snapshot(7, 8)
    assert pipeline_insights.dispatch_gate(successor) is None
    assert holds.prepare(successor, queue, data) == [7]
    data["queues"][0]["items"][0]["updated_at"] = "2026-09-27T13:00:00Z"
    assert holds.prepare(successor, queue, data) == []


def test_partial_snapshot_never_excludes_and_manual_run_clears_hold(isolated_db):
    task = isolated_db.create_task(TaskCreate(prompt="Triage", recurrence="15m"))
    queue = {"item_blockers": True}
    data = snapshot(7)
    holds.prepare(task, queue, data)
    isolated_db.mark_completed(task.id, "ИТОГ: НУЖЕН ЧЕЛОВЕК (#7 — blocked)", verdict="НУЖЕН ЧЕЛОВЕК")
    isolated_db.pause_pipeline_series_on_repeated_blocker(task.series_id, task.id)
    successor = SimpleNamespace(id=task.id + 1, series_id=task.series_id)
    assert holds.prepare(successor, queue, data) == [7]
    partial = copy.deepcopy(data)
    partial["cache"]["complete"] = False
    assert holds.prepare(successor, queue, partial) == []
    assert isolated_db.series_action(task.series_id, "run_now")
    assert holds.prepare(successor, queue, data) == []


def test_excluded_integration_owner_is_not_skipped(monkeypatch):
    monkeypatch.setenv("PP_PIPELINE_EXCLUDED_NUMBERS", "[7]")
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_a, **_kw: {
        "state": "yellow", "integration_owner": {"number": 7, "stage": "integration-merge-ready"},
        "findings": [{"code": "single_flight_barrier", "pr": 7}]})
    result = pp.next_merge(object(), {})
    assert result["action"] == "wait"
    assert result["number"] == 7
