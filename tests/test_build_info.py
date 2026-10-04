"""The custom build tells which build and installation is running."""
import json

from promptpilot import build_info


def test_trial_snapshot_reports_commit_date_and_instance(tmp_path, monkeypatch):
    (tmp_path / "release.json").write_text(json.dumps(
        {"commit": "a" * 40, "commit_date": "2026-10-04T10:48:02+05:00"}), encoding="utf-8")
    monkeypatch.setattr(build_info, "ROOT", tmp_path)
    build_info._code_version.cache_clear()
    monkeypatch.setenv("PP_INSTANCE", "trial")
    info = build_info.build_info()
    build_info._code_version.cache_clear()
    assert info["build"] == "custom" and info["commit"] == "a" * 40
    assert info["date"].startswith("2026-10-04") and info["instance_title"].startswith("Стенд")


def test_unknown_instance_is_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info, "ROOT", tmp_path)  # no release.json, not a git checkout
    monkeypatch.setattr(build_info, "_git", lambda *args: "")
    build_info._code_version.cache_clear()
    monkeypatch.delenv("PP_INSTANCE", raising=False)
    info = build_info.build_info()
    build_info._code_version.cache_clear()
    assert info["commit"] == "" and info["instance"] == "" and info["instance_title"] == ""
