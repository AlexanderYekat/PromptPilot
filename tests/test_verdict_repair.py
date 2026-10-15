import json
import subprocess as stdlib_subprocess
from types import SimpleNamespace

import pytest

import promptpilot.worker as worker
from promptpilot import workflows
from promptpilot.herdr_exec import ensure_closing_verdict_contract
from promptpilot.worker import VERDICT_REPAIR_PROMPT, _repair_verdict

TASK = SimpleNamespace(id=1, model="glm-5.3-flash", effort=None)


def _fake_run(stdout="", stderr="", returncode=0, capture=None):
    calls = []

    def run(cmd, **kwargs):
        if capture is not None:
            capture.append((cmd, kwargs))
        return stdlib_subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run, calls


def test_repair_prompt_carries_report_tail_and_contract():
    prompt = VERDICT_REPAIR_PROMPT.format(tail="...body of the report")

    assert "...body of the report" in prompt
    assert "ИТОГ: ГОТОВО" in prompt
    assert "Не запускай никаких инструментов" in prompt


def test_repair_verdict_parses_plain_output(monkeypatch):
    run, _ = _fake_run(stdout="Отчёт готов.\nИТОГ: ГОТОВО — сделано\n")
    monkeypatch.setattr(worker.subprocess, "run", run)

    verdict = _repair_verdict(TASK, "claude", {}, "хвост отчёта", "/tmp")

    assert verdict == "ГОТОВО"


def test_repair_verdict_parses_stream_json_output(monkeypatch):
    stream = "\n".join([
        json.dumps({"type": "system", "subtype": "init", "session_id": "s-9"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "ИТОГ: УЖЕ СДЕЛАНО — уже исправлено"}]}}),
        json.dumps({"type": "result", "subtype": "success", "is_error": False}),
    ])
    run, _ = _fake_run(stdout=stream)
    monkeypatch.setattr(worker.subprocess, "run", run)

    verdict = _repair_verdict(TASK, "claude", {}, "хвост", "/tmp")

    assert verdict == "УЖЕ СДЕЛАНО"


def test_repair_verdict_uses_stdin_transport_for_stdin_providers(monkeypatch):
    calls = []
    run, _ = _fake_run(stdout="ИТОГ: НЕ СМОГ — не вышло\n", capture=calls)
    monkeypatch.setattr(worker.subprocess, "run", run)

    verdict = _repair_verdict(TASK, "codex", {"prompt_stdin": True}, "хвост", "/tmp")

    assert verdict == "НЕ СМОГ"
    cmd, kwargs = calls[0]
    assert cmd[-1] == "-"
    assert kwargs["input"].startswith("Предыдущий ответ не содержал")
    assert "хвост" in kwargs["input"]


def test_repair_verdict_swallows_provider_failures(monkeypatch):
    def run(cmd, **kwargs):
        raise stdlib_subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(worker.subprocess, "run", run)

    assert _repair_verdict(TASK, "claude", {}, "хвост", "/tmp") == ""


def test_repair_verdict_returns_empty_when_model_stays_silent(monkeypatch):
    run, _ = _fake_run(stdout="Никакой строки вердикта, просто болтовня.\n")
    monkeypatch.setattr(worker.subprocess, "run", run)

    assert _repair_verdict(TASK, "claude", {}, "хвост", "/tmp") == ""


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("note", ["", "Проверь также граничный случай."])
def test_worker_preserves_reviewer_contract_after_operator_note(monkeypatch, required, note):
    monkeypatch.setattr(worker, "VERDICT_REQUIRED", required)
    task = SimpleNamespace(prompt="Проверь код." + workflows.WORKFLOW_REVIEWER_VERDICT_INSTRUCTION,
                           note=note)
    prompt = ensure_closing_verdict_contract(worker.effective_prompt(task))
    assert prompt.count('<promptpilot-workflow-contract') == 1
    assert prompt.endswith(workflows.WORKFLOW_REVIEWER_VERDICT_INSTRUCTION)
    assert "ИТОГ: УЖЕ СДЕЛАНО" not in prompt
    if note:
        assert prompt.index(note) < prompt.index('<promptpilot-workflow-contract')


def test_repair_verdict_keeps_auditor_wording(monkeypatch):
    calls = []
    run, _ = _fake_run(stdout="ИТОГ: ГОТОВО — АУДИТ ЗАВЕРШЁН: замечаний не найдено",
                       capture=calls)
    monkeypatch.setattr(worker.subprocess, "run", run)
    task = SimpleNamespace(**vars(TASK),
                           prompt="Проверь код." + workflows.WORKFLOW_REVIEWER_VERDICT_INSTRUCTION)
    assert _repair_verdict(task, "codex", {"prompt_stdin": True},
                           "AUDIT_FINDINGS_JSON: []\nAUDIT_VERDICT: PASS", "/tmp") == "ГОТОВО"
    prompt = calls[0][1]["input"]
    assert "AUDIT_VERDICT: PASS" in prompt
    assert "ИТОГ: ГОТОВО — АУДИТ ЗАВЕРШЁН: замечаний не найдено" in prompt
    assert "ИТОГ: УЖЕ СДЕЛАНО" not in prompt
