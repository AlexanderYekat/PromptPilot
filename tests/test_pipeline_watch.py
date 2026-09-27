from promptpilot.pipeline_watch import summarize


def test_watch_counts_cached_input_once_and_uses_completed_runs_not_merges():
    result = summarize([{
        "id": 1, "status": "completed", "verdict": "ГОТОВО",
        "completed_at": "2026-09-27T10:02:00+00:00",
        "result": "Tokens: 100 in / 20 out\nCached input: 90\nReasoning output: 10",
    }], [], "2026-09-27T10:00:00+00:00", "2026-09-27T10:03:00+00:00")
    assert result["productive_runs"] == 1
    assert result["completed_run_tokens"] == {
        "input": 100, "cached_input": 90, "uncached_input": 10, "output": 20}
    assert "confirmed_merges" not in result


def test_watch_reports_timeout_and_automatic_pause():
    result = summarize([{
        "id": 7, "status": "running", "series_id": 4,
        "started_at": "2026-09-27T10:00:00+00:00", "task_timeout": 60,
    }], [{"id": 4, "paused": True, "auto_pause_reason": "proof invalid"}],
        "2026-09-27T10:00:00+00:00", "2026-09-27T10:03:00+00:00")
    assert len(result["alerts"]) == 2
    assert result["running"][0]["age_minutes"] == 3
