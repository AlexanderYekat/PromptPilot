"""A daily checker must be silent, quota-free and fail visibly on unknown state."""
import copy
import json
from pathlib import Path

import pytest


@pytest.fixture
def checker(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "tools"))
    import check_updates
    manifest = json.loads((root / "fork-sources.json").read_text(encoding="utf-8"))
    return check_updates, manifest


def fake_github(manifest):
    from urllib.parse import unquote

    def fetch(path):
        if path.endswith("/commits/custom"):
            return {"sha": "a" * 40}
        if path.endswith("/commits/main"):
            return {"sha": manifest["accepted_upstream_commit"]}
        for feature in manifest["features"]:
            if unquote(path).endswith("/commits/" + feature["branch"]):
                return {"sha": feature["accepted_commit"]}
            if path.endswith(f"/pulls/{feature['pr']}"):
                return {"head": {"sha": feature["accepted_commit"]},
                        "state": feature["pr_state"], "merged": False}
        raise AssertionError(path)
    return fetch


def test_unchanged_never_notifies_or_starts_a_process(checker, tmp_path, monkeypatch):
    check, manifest = checker
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: pytest.fail("Unexpected process"))
    report = check.run_check(tmp_path, manifest=manifest, fetch=fake_github(manifest))
    assert report["status"] == "up_to_date" and not report["notified"]
    assert not (tmp_path / "pending.md").exists()


def test_source_change_notified_once_then_cleared_after_acceptance(checker, tmp_path):
    check, manifest = checker
    changed = copy.deepcopy(manifest)
    changed["features"][0]["accepted_commit"] = "b" * 40
    messages = []
    for _ in range(2):
        report = check.run_check(tmp_path, manifest=manifest, fetch=fake_github(changed), notify=messages.append)
        assert report["status"] == "updates_available"
    assert len(messages) == 1
    assert "tools/promote_custom.py" in (tmp_path / "pending.md").read_text(encoding="utf-8")
    report = check.run_check(tmp_path, manifest=changed, fetch=fake_github(changed), notify=messages.append)
    assert report["status"] == "up_to_date" and not report["notified"]
    assert len(messages) == 1
    assert "соответствует" in (tmp_path / "pending.md").read_text(encoding="utf-8")


def test_pr_state_change_and_network_failure_are_actionable(checker, tmp_path):
    check, manifest = checker
    changed = copy.deepcopy(manifest)
    changed["features"][0]["pr_state"] = "closed"
    messages = []
    report = check.run_check(tmp_path, manifest=manifest, fetch=fake_github(changed), notify=messages.append)
    assert report["changes"][0]["state"] == "closed"

    def offline(path):
        raise TimeoutError("network unavailable")

    for _ in range(2):
        assert check.run_check(tmp_path, fetch=offline, notify=messages.append)["status"] == "error"
    assert len(messages) == 2
    assert check.run_check(tmp_path, manifest=manifest, fetch=fake_github(manifest), notify=messages.append)["status"] == "up_to_date"
    assert len(messages) == 3


def test_notification_failure_is_not_marked_delivered(checker, tmp_path):
    check, manifest = checker
    changed = copy.deepcopy(manifest)
    changed["accepted_upstream_commit"] = "c" * 40

    def unavailable(path):
        raise RuntimeError("Windows notifications disabled")

    with pytest.raises(RuntimeError, match="disabled"):
        check.run_check(tmp_path, manifest=manifest, fetch=fake_github(changed), notify=unavailable)
    assert not (tmp_path / "state.json").exists()
    assert json.loads((tmp_path / "last-check.json").read_text())["status"] == "updates_available"
