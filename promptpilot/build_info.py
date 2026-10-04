"""Which build is running, so every page can say it plainly (custom build only).

The original author's version has no such banner: its absence is the sign
that the author's version is open.
"""

from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
INSTANCES = {"trial": "Стенд (тестовые данные)", "production": "Рабочая установка"}


def _git(*args):
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True,
                                timeout=3, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@lru_cache(maxsize=1)
def _code_version():
    # Trial snapshots carry release.json (no .git); installations from a checkout use git.
    try:
        release = json.loads((ROOT / "release.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        release = {}
    commit = release.get("commit") or _git("rev-parse", "HEAD")
    date = release.get("commit_date") or (_git("log", "-1", "--format=%cI", commit) if commit else "")
    return commit, date


def build_info():
    commit, date = _code_version()
    instance = os.environ.get("PP_INSTANCE", "").strip().lower()
    return {"build": "custom", "title": "Моя сборка", "commit": commit, "date": date,
            "instance": instance, "instance_title": INSTANCES.get(instance, "")}
