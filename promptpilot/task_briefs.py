"""Short task descriptions from saved objectives, without calling an agent."""

import re

from . import db, workflows
from .models import ReviewVerdict, TaskStatus


def _short(text, limit):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def _prompt_goal(prompt):
    # Prefer an explicit objective over setup instructions and long preambles.
    labeled = re.search(
        r"(?im)^\s*(?:#{1,6}\s*)?(?:цель(?: задачи| этапа)?|задача|objective|goal|task)\s*:\s*([^\n]+)",
        prompt,
    )
    if labeled:
        return labeled[1].strip()
    lines = [line.strip().lstrip("# ") for line in prompt.splitlines() if line.strip()]
    return lines[0] if lines else ""


def _review_outcome(task):
    """Describe the audit without rewriting the agent's historical report."""
    if task.status is not TaskStatus.COMPLETED:
        return None
    decision = workflows.parse_reviewer_report(task.result or "")
    if task.verdict == "НЕ СМОГ":
        return {"label": "АУДИТ НЕ ЗАВЕРШЁН", "icon": "❌",
                "summary": "Аудит не завершён: блокер указан в отчёте."}
    if task.verdict == "НУЖЕН ЧЕЛОВЕК" or (decision and decision.verdict is ReviewVerdict.HUMAN_REQUIRED):
        return {"label": "АУДИТ: НУЖЕН ЧЕЛОВЕК", "icon": "🟡",
                "summary": "Для завершения аудита требуется решение или доступ человека."}
    if not decision:
        return {"label": "ИТОГ АУДИТА НЕ РАСПОЗНАН", "icon": "🟡",
                "summary": "Нет корректного решения AUDIT_VERDICT: проверьте полный отчёт."}
    if decision.verdict is ReviewVerdict.REVISION_REQUIRED:
        return {"label": "ТРЕБУЕТСЯ ДОРАБОТКА", "icon": "🟡",
                "summary": "АУДИТ ЗАВЕРШЁН: требуется доработка, замечания перечислены в отчёте."}
    if decision.findings:
        summary = "АУДИТ ЗАВЕРШЁН: аудитор сообщил PASS, замечания перечислены в отчёте."
    elif re.search(r"(?mi)^AUDIT_FINDINGS_JSON:[ \t]*\[[ \t]*\][ \t]*\r?$", task.result or ""):
        summary = "АУДИТ ЗАВЕРШЁН: замечаний не найдено"
    else:
        summary = "АУДИТ ЗАВЕРШЁН: аудитор сообщил PASS; список замечаний не указан."
    return {"label": "АУДИТ ЗАВЕРШЁН", "icon": "✅", "summary": summary}


def for_tasks(tasks):
    """Batch lookup uses each run's stage, including historical task cards."""
    if not tasks:
        return {}
    ids = [task.id for task in tasks]
    tasks_by_id = {task.id: task for task in tasks}
    marks = ",".join("?" for _ in ids)
    with db._connect() as conn:
        runs = conn.execute(
            f"""SELECT r.task_id, r.role, w.slug, w.objective AS workflow_goal,
                       n.round_no, s.code, s.title, s.objective
                FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id
                JOIN workflow_rounds n ON n.id=r.round_id
                LEFT JOIN workflow_stages s ON s.id=n.stage_id
                WHERE r.task_id IN ({marks})""", ids,
        ).fetchall()
        planners = conn.execute(
            f"""SELECT p.planner_task_id AS task_id, w.slug, w.objective
                FROM workflow_plans p JOIN workflows w ON w.id=p.workflow_id
                WHERE p.planner_task_id IN ({marks})""", ids,
        ).fetchall()
        series = conn.execute(
            f"""SELECT t.id, s.title FROM tasks t JOIN task_series s ON s.id=t.series_id
                WHERE t.id IN ({marks})""", ids,
        ).fetchall()
    series_titles = {row["id"]: row["title"] for row in series}
    briefs = {}
    for task in tasks:
        goal = _prompt_goal(task.prompt)
        briefs[task.id] = {
            "title": _short(series_titles.get(task.id) or goal or f"Задача №{task.id}", 100),
            "goal": _short(goal, 300), "context": "", "source": "prompt",
        }
    for row in planners:
        briefs[row["task_id"]] = {
            "title": "Составить план работы", "goal": _short(row["objective"], 300),
            "context": row["slug"] + " · Планировщик", "source": "workflow",
        }
    for row in runs:
        reviewer = row["role"] == "reviewer"
        title = row["title"] or row["workflow_goal"]
        if row["code"]:
            title = row["code"] + " · " + title
        goal = row["objective"] or row["workflow_goal"]
        if reviewer:
            title = "Проверка: " + title
            goal = "Независимо проверить результат: " + goal
        role = "Аудитор" if reviewer else "Исполнитель"
        briefs[row["task_id"]] = {
            "title": _short(title, 120), "goal": _short(goal, 300),
            "context": f"{row['slug']} · {role} · Раунд {row['round_no']}", "source": "workflow",
        }
        if reviewer:
            outcome = _review_outcome(tasks_by_id[row["task_id"]])
            if outcome:
                briefs[row["task_id"]]["outcome"] = outcome
    return briefs
