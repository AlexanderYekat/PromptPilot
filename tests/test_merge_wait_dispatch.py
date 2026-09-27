"""A coherent integration REVIEW wait must not launch a MERGE provider."""

import pytest

from promptpilot import pipeline_insights, project_pipeline as pp
from promptpilot.models import TaskCreate


HEAD = "3e7be634fba4504b0a768662122d3cc12d5572d9"


def rest_only_waiting_health(stage="integration-review", executable=None):
    """Mirror a coherent REST view that may still hide malformed lineage."""
    owner = {"number": 1232, "head": HEAD, "stage": stage}
    waiting_code = (
        "base_sync_waiting_review"
        if stage == "integration-review"
        else "legacy_ship_waiting_review_validation"
    )
    return {
        "state": "yellow",
        "integration_owner": owner,
        "review_candidates": [dict(owner)],
        "content_review_candidates": [],
        "merge_executable": executable,
        "findings": [
            {"code": waiting_code, "severity": "yellow", "pr": 1232},
            {"code": "single_flight_barrier", "severity": "yellow", "pr": 1232},
        ],
    }


@pytest.mark.parametrize("stage", ["integration-review", "legacy-integration-review"])
@pytest.mark.parametrize("executable", [None, []])
def test_rest_only_integration_wait_does_not_bypass_owner(
        monkeypatch, stage, executable):
    calls = []
    health = rest_only_waiting_health(stage, executable)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])

    def fresh(config, **kwargs):
        calls.append(kwargs)
        return health

    monkeypatch.setattr(pp, "run_health", fresh)
    monkeypatch.setattr(
        pp, "list_ship",
        lambda *_: pytest.fail("single-flight owner must be handled before ordinary ship"),
    )

    result = pp.next_merge(object(), {}, config_path="pipelinectl.json")

    assert calls == [{"config_path": "pipelinectl.json"}]
    assert result == {
        "action": "wait", "number": 1232,
        "reason": "single-flight owner is waiting for integration REVIEW",
    }


@pytest.mark.parametrize("stage", ["integration-review", "legacy-integration-review"])
@pytest.mark.parametrize("executable", [None, []])
def test_signed_handoff_opt_in_waits_without_authorizing_merge(
        monkeypatch, stage, executable):
    health = rest_only_waiting_health(stage, executable)
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(pp, "run_health", lambda *_args, **_kwargs: health)
    monkeypatch.setattr(
        pp, "list_ship",
        lambda *_: pytest.fail("integration REVIEW owner must retain full MERGE fallback"),
    )

    result = pp.next_merge(object(), {"fallback_handoff": "target-v1"})

    assert result == {
        "action": "wait", "number": 1232,
        "reason": "single-flight owner is waiting for integration REVIEW",
    }
    assert "handoff" not in result


def test_auto_execution_completes_review_wait_without_agent(isolated_db, monkeypatch):
    task = isolated_db.create_task(TaskCreate(
        prompt="OneBase - MERGE\n/merge-shepherd", recurrence="4h",
    ))
    queue = {
        "id": "merge",
        "execution": {
            "mode": "auto",
            "command": ["project-pipelinectl", "next", "merge"],
        },
    }
    monkeypatch.setattr(
        pipeline_insights, "_matching_queue", lambda _: ("onebase", {}, queue),
    )
    monkeypatch.setattr(
        pipeline_insights, "_tool_available", lambda *_: (True, ""),
    )
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [])
    monkeypatch.setattr(
        pp, "run_health", lambda *_args, **_kwargs: rest_only_waiting_health(),
    )
    monkeypatch.setattr(
        pipeline_insights, "_tool_preflight",
        lambda *_: pp.next_merge(object(), {}),
    )

    route = pipeline_insights.execution_route(task, task.prompt)

    assert route["action"] == "complete_empty"
    assert route["reason"] == (
        "single-flight owner is waiting for integration REVIEW"
    )


def test_pending_cleanup_still_precedes_rest_only_integration_hint(monkeypatch):
    intent = {"number": 7}
    monkeypatch.setattr(pp, "pending_merge_intents", lambda *_: [intent])
    monkeypatch.setattr(
        pp, "run_health", lambda *_args, **_kwargs: pytest.fail("cleanup precedes health"),
    )
    monkeypatch.setattr(
        pp, "pending_merge_action",
        lambda _gh, _config, found: {"action": "cleanup", "target": found},
    )

    assert pp.next_merge(object(), {}) == {"action": "cleanup", "target": intent}
