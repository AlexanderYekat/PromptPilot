"""Git-backed, durable workflow handoffs (opt-in)."""

import fnmatch
from functools import wraps
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

from . import db
from .models import WorkflowConfig, WorkflowEventCreate


class CandidateError(db.WorkflowConflictError):
    def __init__(self, code, reason):
        self.code = code
        super().__init__(f"{code}: {reason}")


def config(workflow):
    raw = (db._json_load(workflow['config_json'])
           if not hasattr(workflow, 'config') else workflow.config)
    return WorkflowConfig.model_validate(raw)


def canonical(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def overlaps(first, second):
    try:
        return os.path.commonpath([first, second]) in {first, second}
    except ValueError:
        return False


def _git_env():
    # Neither a parent GIT_DIR nor an index override may redirect verification.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    env['GIT_NO_REPLACE_OBJECTS'] = '1'
    return env


def git(path, *args, stdin=None):
    try:
        result = subprocess.run(
            ['git', '-C', path, *args], env=_git_env(), capture_output=True, timeout=30,
            input=stdin.encode('utf-8', 'surrogateescape') if stdin is not None else None)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidateError('repository_unavailable', str(exc)) from exc
    if result.returncode:
        raise CandidateError('git_verification_failed', result.stderr.decode('utf-8', 'replace').strip())
    return result.stdout.decode('utf-8', 'surrogateescape').strip('\n')


def identity(path):
    path = canonical(path)
    if canonical(git(path, 'rev-parse', '--show-toplevel')) != path:
        raise CandidateError('repository_mismatch', 'Нужен корень сдаваемой рабочей папки')
    common = git(path, 'rev-parse', '--git-common-dir')
    return dict(repository_path=path, repository_identity=canonical(os.path.join(path, common)),
                branch=git(path, 'symbolic-ref', '--short', 'HEAD'),
                revision=git(path, 'rev-parse', '--verify', 'HEAD^{commit}'))


def relative(value):
    value = value.replace('\\', '/')
    p = PurePosixPath(value)
    if (not value or p.is_absolute() or ':' in value or '..' in p.parts
            or p.parts[:1] == ('.git',) or str(p) == '.' or any(c in value for c in '*?[')):
        raise CandidateError('invalid_path', f'Нужен конкретный относительный путь: {value}')
    return str(p)


def file_digest(root, name):
    path = Path(root) / name
    if not overlaps(canonical(root), canonical(path)) or canonical(path) == canonical(root):
        raise CandidateError('unsafe_input', name)
    # Reject indirection: hashing the symlink target alone misses link replacement.
    for part in [path, *path.parents]:
        if part == Path(root):
            break
        if part.is_symlink() or (part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise CandidateError('unsupported_symlink', name)
    try:
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
        return digest.hexdigest()
    except OSError as exc:
        raise CandidateError('input_unavailable', f'{name}: {exc}') from exc


def under(name, directory):
    return name == directory or name.startswith(directory + '/')


def snapshot(path, policy):
    observed = identity(path)
    path = observed['repository_path']
    inputs = {relative(n) for n in policy.additional_inputs}
    outputs = {relative(n).rstrip('/') for n in policy.output_directories}
    tracked = {}
    for item in git(path, 'ls-files', '--stage', '-z').split('\0'):
        if not item:
            continue
        metadata, name = item.split('\t', 1)
        mode, oid, stage = metadata.split()
        if stage != '0' or mode in {'160000', '120000'}:
            raise CandidateError('unsupported_index', f'Конфликт, submodule или symlink: {name}')
        tracked[name] = (mode, oid)
    for name in inputs:
        if PurePosixPath(name).suffix.lower() not in {'.md', '.txt', '.rst', '.pdf'}:
            raise CandidateError('invalid_additional_input', f'Дополнительный вход должен быть документом: {name}')
    for directory in outputs:
        output_path = Path(path) / directory
        if output_path.exists() and not output_path.is_dir():
            raise CandidateError('unsafe_output_directory', f'Выходной путь должен быть каталогом: {directory}')
        if any(under(name, directory) for name in set(tracked) | inputs):
            raise CandidateError('unsafe_output_directory', f'Каталог содержит проверяемые входы: {directory}')
        if directory.split('/')[-1].lower() in {'src', 'tests', 'test', 'lib', 'promptpilot', '.git'}:
            raise CandidateError('unsafe_output_directory', directory)
    # --others includes ignored files. Ignoring a source in .gitignore is not an exemption.
    others = set(filter(None, git(path, 'ls-files', '--others', '-z').split('\0')))
    unexpected = sorted(n for n in others if n not in inputs and not any(under(n, o) for o in outputs))
    source_suffixes = {'.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.bsl', '.c', '.h', '.cpp',
                       '.cs', '.java', '.go', '.rs', '.sh', '.ps1', '.bat', '.cmd', '.rb', '.php'}
    unexpected += sorted(n for n in others if any(under(n, o) for o in outputs)
                         and PurePosixPath(n).suffix.lower() in source_suffixes)
    if unexpected:
        raise CandidateError('untracked_inputs', 'Незарегистрированные файлы: ' + ', '.join(unexpected[:30]))
    dirty = set(filter(None, git(path, 'diff', 'HEAD', '--name-only', '-z', '--no-renames').split('\0')))
    if dirty - inputs:
        raise CandidateError('dirty_inputs', 'Незакоммиченные файлы: ' + ', '.join(sorted(dirty - inputs)))
    # diff HEAD can be fooled by skip-worktree/assume-unchanged; hash actual files too.
    committed = {}
    for item in git(path, 'ls-tree', '-r', '-z', 'HEAD').split('\0'):
        if item:
            metadata, name = item.split('\t', 1)
            committed[name] = metadata.split()[2]
    checked = sorted(set(tracked) - inputs)
    attrs = git(path, 'check-attr', '-z', '--stdin', 'filter', stdin=''.join(n + '\0' for n in checked)).split('\0') if checked else []
    for index in range(0, len(attrs) - 1, 3):
        if attrs[index + 2] not in {'unspecified', 'unset'}:
            raise CandidateError('unsupported_filter', 'Нельзя доказать соответствие commit через пользовательский Git filter: ' + attrs[index])
    actual_hashes = git(path, 'hash-object', '--stdin-paths', stdin=''.join(json.dumps(n, ensure_ascii=False) + '\n' for n in checked)).splitlines() if checked else []
    if len(actual_hashes) != len(checked):
        raise CandidateError('git_verification_failed', 'Не получены контрольные суммы всех файлов')
    actual = dict(zip(checked, actual_hashes))
    manifest = {}
    for name, (mode, oid) in tracked.items():
        digest = file_digest(path, name)
        if name not in inputs:
            if oid != committed.get(name) or actual[name] != committed.get(name):
                raise CandidateError('dirty_inputs', name)
        manifest[name] = {'sha256': digest, 'mode': mode}
    extra = {name: file_digest(path, name) for name in sorted(inputs)}
    observed.update(manifest=manifest, additional_inputs=extra,
                    additional_input_index={n: list(tracked[n]) if n in tracked else None for n in sorted(inputs)})
    return observed


def existing_session(provider, target=None):
    from .config import DEFAULT_CLI, load_providers
    provider = provider or DEFAULT_CLI
    return bool(target or provider == 'herdr-session' or load_providers().get(provider, {}).get('session_target'))


def _reaches_output(entry, directory):
    """Whether an allowed_paths entry equals, lies inside or contains an output directory."""
    path = entry.strip().replace('\\', '/')
    while path.startswith('./'):
        path = path[2:]
    if os.name == 'nt':
        path, directory = path.casefold(), directory.casefold()
    wildcards = [index for index in (path.find(c) for c in '*?[') if index >= 0]
    if not wildcards:
        path = path.rstrip('/')
        return bool(path) and (under(path, directory) or under(directory, path))
    # A pattern is judged by its fixed prefix: it can match anything continuing it.
    prefix = path[:min(wildcards)]
    return directory.startswith(prefix) or prefix.startswith(directory + '/')


def output_path_conflicts(cfg, specs):
    """Stage allowed_paths that would commit into output directories, as messages."""
    outputs = [relative(name).rstrip('/') for name in cfg.candidate.output_directories]
    conflicts = []
    for spec in specs:
        label = f"Этап {spec['code']}" if spec.get('code') else 'Этап'
        for entry in spec.get('allowed_paths') or []:
            for directory in outputs:
                if _reaches_output(entry, directory):
                    conflicts.append(f'{label}: путь {entry} пересекается с выходным каталогом {directory}.')
    return conflicts


def unignored_output_directories(workflow):
    """Output directories Git would not ignore: `git add -A` would commit their results."""
    cfg = config(workflow)
    if not cfg.candidate.enabled:
        return []
    found = []
    for name in cfg.candidate.output_directories:
        try:
            directory = relative(name).rstrip('/')
            result = subprocess.run(
                ['git', '-C', workflow.repository_path, 'check-ignore', '-q', '--',
                 directory + '/promptpilot-output-probe'],
                env=_git_env(), capture_output=True, timeout=30)
        except (CandidateError, OSError, subprocess.TimeoutExpired):
            continue
        # 0: ignored, 1: not ignored, anything else: Git could not tell.
        if result.returncode == 1:
            found.append(directory)
    return found


def readiness(workflow, stages=(), check_output_paths=False):
    cfg = config(workflow)
    commands = list(cfg.gate.commands)
    commands.extend(cfg.stage.get('acceptance_gates', []))
    specs = []
    for stage in stages:
        if hasattr(stage, 'acceptance_gates'):
            spec = stage.model_dump()
        else:
            # A stage row keeps its code in a column, not in spec_json.
            spec = {**db._json_load(stage['spec_json']), 'code': stage['code']}
        commands.extend(spec.get('acceptance_gates', []))
        specs.append(spec)
    if not cfg.candidate.enabled:
        if 'PROMPTPILOT_CANDIDATE_' in json.dumps(commands + [cfg.stage]):
            raise CandidateError('candidate_disabled', 'План использует PROMPTPILOT_CANDIDATE_SHA. Включите config.candidate.enabled')
        return
    if not cfg.gate.enabled and commands:
        raise CandidateError('required_gates_disabled', 'Обязательные gate нельзя отключить при фиксации версии')
    for role in (cfg.roles.executor, cfg.roles.reviewer):
        if role.worktree or role.machine or existing_session(role.provider, role.herdr_target):
            raise CandidateError('repository_mismatch', 'Фиксация версии поддерживает локальные роли в единой рабочей папке; worktree, remote и herdr-session требуют отдельной поддержки')
    for command in commands:
        if '\\"$env:PROMPTPILOT_CANDIDATE_' in command:
            raise CandidateError('invalid_candidate_command', 'В PowerShell используйте "$env:PROMPTPILOT_CANDIDATE_SHA" без обратных слешей')
    # Checked when a plan is approved or a planless workflow starts. Committing
    # into an output directory would otherwise surface only at handoff.
    if check_output_paths:
        conflicts = output_path_conflicts(cfg, specs or [cfg.stage])
        if conflicts:
            raise CandidateError('output_path_conflict', ' '.join(conflicts) + (
                ' Выходные каталоги не коммитятся — уберите '
                + ('путь' if len(conflicts) == 1 else 'эти пути') + ' из allowed_paths'))


def current(conn, workflow, required=True):
    row = conn.execute('''SELECT c.payload_json FROM workflow_candidates c
        JOIN workflow_rounds r ON r.id=c.round_id JOIN workflow_runs x ON x.id=c.run_id
        WHERE c.workflow_id=? AND r.round_no=? ORDER BY x.attempt_no DESC LIMIT 1''',
        (workflow['id'], workflow['current_round'])).fetchone()
    if not row and required:
        raise CandidateError('candidate_missing', 'Нет проверенной передачи результата candidate_revision')
    candidate = db._json_load(row['payload_json']) if row else None
    if candidate and required:
        assert_not_invalidated(conn, candidate)
    return candidate


def assert_not_invalidated(conn, candidate):
    if conn.execute('SELECT 1 FROM workflow_events WHERE idempotency_key=?',
                    ('candidate.invalidated:' + candidate['candidate_id'],)).fetchone():
        raise CandidateError('candidate_invalidated', 'Сдача уже потеряла целостность. Нужны новая передача результата и новые проверки')


def invalidate(conn, candidate, reason):
    if conn.execute('SELECT 1 FROM workflow_events WHERE idempotency_key=?',
                    ('candidate.invalidated:' + candidate['candidate_id'],)).fetchone():
        return
    db._append_workflow_event(conn, WorkflowEventCreate(
        workflow_id=candidate['workflow_id'], round_id=candidate['round_id'], event_type='candidate.invalidated',
        idempotency_key='candidate.invalidated:' + candidate['candidate_id'],
        payload={'candidate_id': candidate['candidate_id'], 'reason': reason}))


def guard_boundary(action):
    """Persist observed integrity failures after the failed transaction rolls back."""
    @wraps(action)
    def guarded(workflow_id, *args, **kwargs):
        try:
            return action(workflow_id, *args, **kwargs)
        except CandidateError as exc:
            if exc.code in {'candidate_changed', 'dirty_inputs', 'untracked_inputs', 'input_unavailable',
                            'unsafe_input', 'unsupported_symlink', 'unsupported_index', 'git_verification_failed'}:
                with db._connect(immediate=True) as conn:
                    workflow = conn.execute('SELECT * FROM workflows WHERE id=?', (workflow_id,)).fetchone()
                    candidate = current(conn, workflow, required=False) if workflow else None
                    if candidate:
                        invalidate(conn, candidate, str(exc))
            raise
    return guarded


def list_candidates(workflow_id):
    with db._connect() as conn:
        return [db._json_load(r[0]) for r in conn.execute(
            'SELECT payload_json FROM workflow_candidates WHERE workflow_id=? ORDER BY rowid', (workflow_id,))]


def validate(candidate):
    from .models import WorkflowCandidateConfig
    observed = snapshot(candidate['repository_path'], WorkflowCandidateConfig.model_validate(candidate['policy']))
    with db._connect() as conn:
        assert_not_invalidated(conn, candidate)
    for key in ('repository_path', 'repository_identity', 'branch', 'manifest', 'additional_inputs', 'additional_input_index'):
        if candidate[key] != observed[key]:
            raise CandidateError('candidate_changed', f'Код или входы изменились после передачи на проверку ({key}). Сдайте новую версию; предыдущие результаты сохранены')
    if observed['revision'] != candidate['candidate_revision']:
        raise CandidateError('candidate_changed', 'HEAD изменился после передачи на проверку. Сдайте новую версию; предыдущие результаты сохранены')
    return observed


def task_blocked(conn, task):
    locks = conn.execute('SELECT * FROM workflow_candidate_locks').fetchall()
    if not locks:
        return False
    path = canonical(getattr(task, 'working_dir', None) or os.getcwd())
    for lock in locks:
        if not overlaps(path, lock['repository_path']):
            continue
        own = conn.execute('SELECT 1 FROM workflow_runs WHERE task_id=? AND workflow_id=? AND round_id=?',
                           (task.id, lock['workflow_id'], lock['round_id'])).fetchone()
        if not own:
            return True
    return False


def prepare_dispatch(conn, workflow, round_row, dispatch, working_dir):
    if not config(workflow).candidate.enabled:
        return {}, dispatch.prompt
    readiness(workflow)
    if (dispatch.worktree or dispatch.machine or existing_session(dispatch.provider, dispatch.herdr_target)
            or canonical(working_dir) != canonical(workflow['repository_path'])):
        raise CandidateError('repository_mismatch', 'Исполнитель, gate и аудитор должны работать в одной локальной папке')
    if dispatch.role.value == 'reviewer':
        candidate = current(conn, workflow)
        validate(candidate)
        gates = [db._json_load(r[0]) for r in conn.execute(
            "SELECT payload_json FROM workflow_events WHERE workflow_id=? AND round_id=? AND event_type='candidate.gate_finished' ORDER BY seq",
            (workflow['id'], candidate['round_id']))]
        gates = [g for g in gates if g.get('candidate_id') == candidate['candidate_id']]
        context = {'candidate_id': candidate['candidate_id'], 'candidate': candidate, 'gate_protocols': gates}
        prompt_context = {k: v for k, v in candidate.items() if k not in {'manifest', 'additional_input_index'}}
        prompt_context.update(manifest_files=len(candidate['manifest']), gate_protocols=gates,
                              manifest_api=f"/api/workflows/{workflow['id']}/candidates")
        return context, (dispatch.prompt + '\n\nВерсия: ' + candidate['candidate_revision']
                         + '\nРабочая папка: ' + candidate['repository_path']
                         + '\nПроверяемая передача результата (не изменяйте входы; полный манифест доступен в API):\n' + json.dumps(prompt_context, ensure_ascii=False))
    for lock in conn.execute('SELECT * FROM workflow_candidate_locks'):
        if overlaps(canonical(working_dir), lock['repository_path']):
            raise CandidateError('repository_busy', 'Папка уже зарезервирована для workflow ' + lock['workflow_id'])
    for task in conn.execute("SELECT * FROM tasks WHERE status='running'"):
        if overlaps(canonical(task['working_dir'] or os.getcwd()), canonical(working_dir)):
            raise CandidateError('repository_busy', f"В папке работает задача #{task['id']}")
    baseline = snapshot(working_dir, config(workflow).candidate)
    if baseline['branch'] != workflow['candidate_branch']:
        raise CandidateError('branch_mismatch', 'Текущая ветка не совпадает с candidate_branch workflow')
    stage = conn.execute('SELECT spec_json FROM workflow_stages WHERE id=?', (workflow['current_stage_id'],)).fetchone()
    spec = db._json_load(stage[0]) if stage else config(workflow).stage
    baseline.update(policy=config(workflow).candidate.model_dump(), allowed_paths=spec.get('allowed_paths', []))
    conn.execute('INSERT INTO workflow_candidate_locks VALUES (?, ?, ?)',
                 (baseline['repository_path'], workflow['id'], round_row['id']))
    prompt = dispatch.prompt + '''\n\nПередайте результат отдельной строкой:
CANDIDATE_JSON: {"candidate_revision":"<полный Git commit SHA>","no_changes":false}
Все изменения реализации и тестов закоммитьте в ветку кандидата в указанной рабочей папке.
Если изменений нет, передайте исходный SHA и no_changes:true. Пустой commit не нужен.
Существующие пользовательские документы сохраните без изменений. Не используйте reset/clean/stash.
'''
    return {'candidate_baseline': baseline}, prompt


def capture(conn, workflow, run, task):
    previous = conn.execute('SELECT payload_json FROM workflow_candidates WHERE run_id=?', (run['id'],)).fetchone()
    if previous:
        candidate = db._json_load(previous[0])
        validate(candidate)
        return candidate
    baseline = db._json_load(run['input_json']).get('candidate_baseline')
    if not baseline:
        raise CandidateError('baseline_missing', 'Нет исходной описи репозитория исполнителя')
    matches = re.findall(r'^CANDIDATE_JSON:\s*(\{[^\n]*\})\s*$', task['result'] or '', re.MULTILINE)
    try:
        report = json.loads(matches[0]) if len(matches) == 1 else {}
    except ValueError:
        report = {}
    if not isinstance(report, dict):
        report = {}
    revision = report.get('candidate_revision')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision):
        raise CandidateError('candidate_missing', 'Исполнитель не передал корректный CANDIDATE_JSON с candidate_revision')
    actual_path = task['worktree_path'] or task['working_dir']
    if canonical(actual_path) != baseline['repository_path']:
        raise CandidateError('repository_mismatch', 'Рабочая папка исполнителя отличается от исходной')
    from .models import WorkflowCandidateConfig
    observed = snapshot(actual_path, WorkflowCandidateConfig.model_validate(baseline['policy']))
    for key in ('repository_path', 'repository_identity', 'branch', 'additional_inputs', 'additional_input_index'):
        if observed[key] != baseline[key]:
            raise CandidateError('baseline_changed', f'Исходная идентичность или пользовательские входы изменены: {key}')
    if observed['revision'] != revision:
        raise CandidateError('revision_mismatch', 'candidate_revision не совпадает с HEAD сдаваемой папки')
    git(actual_path, 'merge-base', '--is-ancestor', baseline['revision'], revision)
    changed = set(filter(None, git(actual_path, 'diff', '--name-only', '-z', '--no-renames', baseline['revision'], revision).split('\0')))
    if baseline['additional_inputs'].keys() & changed:
        raise CandidateError('user_inputs_changed', 'Существующие документы нельзя включать в commit исполнителя')
    allowed = baseline['allowed_paths']
    outside = [n for n in changed if allowed and not any(under(n, p.rstrip('/')) or fnmatch.fnmatchcase(n, p) for p in allowed)]
    if outside:
        raise CandidateError('outside_scope', 'Изменения вне разрешённых путей: ' + ', '.join(sorted(outside)))
    if revision == baseline['revision'] and report.get('no_changes') is not True:
        raise CandidateError('no_changes_unconfirmed', 'Для исходного commit нужно явное no_changes:true')
    candidate = {**observed, 'candidate_id': db._new_id('candidate'),
                 'workflow_id': workflow['id'], 'round_id': run['round_id'], 'round_no': workflow['current_round'],
                 'stage_id': workflow['current_stage_id'], 'executor_run_id': run['id'], 'executor_task_id': task['id'],
                 'base_revision': baseline['revision'], 'candidate_revision': revision,
                 'policy': baseline['policy'], 'state_version': workflow['state_version'], 'created_at': db._now()}
    conn.execute('INSERT INTO workflow_candidates VALUES (?, ?, ?, ?, ?)',
                 (candidate['candidate_id'], workflow['id'], run['round_id'], run['id'], db._json_dump(candidate)))
    conn.execute('UPDATE workflow_rounds SET base_sha=?, candidate_sha=? WHERE id=?',
                 (baseline['revision'], revision, run['round_id']))
    db._append_workflow_event(conn, WorkflowEventCreate(
        workflow_id=workflow['id'], round_id=run['round_id'], run_id=run['id'], event_type='candidate.captured',
        idempotency_key='candidate.captured:' + run['id'], payload=candidate))
    return candidate


def preflight_task(task):
    """Worker calls this immediately before launching a provider, including retries."""
    with db._connect(immediate=True) as conn:
        if task_blocked(conn, task):
            raise CandidateError('repository_busy', 'Рабочая папка зарезервирована для приёмки workflow')
        run = conn.execute('SELECT * FROM workflow_runs WHERE task_id=?', (task.id,)).fetchone()
        if not run:
            return
        workflow = conn.execute('SELECT * FROM workflows WHERE id=?', (run['workflow_id'],)).fetchone()
        if not config(workflow).candidate.enabled:
            return
        if existing_session(task.provider, task.herdr_target):
            raise CandidateError('repository_mismatch', 'Нельзя подтвердить папку существующей сессии провайдера')
        context = db._json_load(run['input_json'])
        if task.worktree or task.worktree_path or task.machine or task.herdr_target:
            raise CandidateError('repository_mismatch', 'Неподдерживаемая рабочая папка роли')
        if run['role'] == 'reviewer':
            candidate = current(conn, workflow)
            if context.get('candidate_id') != candidate['candidate_id']:
                raise CandidateError('stale_candidate', 'Задача аудитора относится к другой сдаче')
            if canonical(task.working_dir) != candidate['repository_path']:
                raise CandidateError('repository_mismatch', 'Папка аудитора отличается от кандидата')
            validate(candidate)
        elif run['role'] == 'executor':
            baseline = context.get('candidate_baseline')
            if not baseline or canonical(task.working_dir) != baseline['repository_path']:
                raise CandidateError('baseline_missing', 'Нет исходной описи исполнителя')
            from .models import WorkflowCandidateConfig
            observed = snapshot(task.working_dir, WorkflowCandidateConfig.model_validate(baseline['policy']))
            if any(observed[k] != baseline[k] for k in observed):
                raise CandidateError('baseline_changed', 'Рабочая папка изменилась после постановки исполнителя в очередь')


def run_gates(workflow, windows_runner):
    """Claim once before executing. An interrupted claim requires operator action."""
    from .models import WorkflowGateDecision
    cfg = config(workflow)
    stage = db.get_workflow_stage(workflow.current_stage_id) if workflow.current_stage_id else None
    commands = list(dict.fromkeys(cfg.gate.commands + (stage.acceptance_gates if stage else cfg.stage.get('acceptance_gates', []))))
    with db._connect(immediate=True) as conn:
        row = conn.execute('SELECT * FROM workflows WHERE id=?', (workflow.id,)).fetchone()
        if row['state_version'] != workflow.state_version or row['status'] != 'gating':
            raise db.WorkflowConflictError('workflow changed concurrently')
        candidate = current(conn, row)
        key = f"candidate.gate_started:{candidate['candidate_id']}"
        if conn.execute('SELECT 1 FROM workflow_events WHERE idempotency_key=?', (key,)).fetchone():
            raise CandidateError('gate_already_started', 'Gate уже запускался. Незавершённый запуск не считается PASS; требуется решение оператора')
        db._append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=workflow.id, round_id=candidate['round_id'], event_type='candidate.gate_started',
            idempotency_key=key, payload={'candidate_id': candidate['candidate_id'], 'state_version': workflow.state_version}))
    env = dict(os.environ)
    env.update(PROMPTPILOT_CANDIDATE_SHA=candidate['candidate_revision'],
               PROMPTPILOT_CANDIDATE_ID=candidate['candidate_id'],
               PROMPTPILOT_CANDIDATE_REPOSITORY=candidate['repository_path'])
    evidence, records = [], []
    verdict, summary = 'PASS', f'Пройдено команд gate: {len(commands)}'
    observed_revision = None
    try:
        readiness(workflow, [stage] if stage else [])
        observed_revision = identity(candidate['repository_path'])['revision']
        validate(candidate)
        for command in commands:
            record = {'candidate_id': candidate['candidate_id'], 'expected_revision': candidate['candidate_revision'],
                      'repository_path': candidate['repository_path'], 'command': command, 'exit_code': None,
                      'before_ok': False, 'after_ok': False}
            records.append(record)
            record['observed_before'] = identity(candidate['repository_path'])['revision']
            validate(candidate)
            record['before_ok'] = True
            argv = (['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command]
                    if os.name == 'nt' else ['/bin/sh', '-lc', command])
            try:
                result = (windows_runner(argv, candidate['repository_path'], cfg.gate.timeout_seconds, env=env)
                          if os.name == 'nt' else subprocess.run(argv, cwd=candidate['repository_path'], env=env,
                              capture_output=True, text=True, errors='replace', timeout=cfg.gate.timeout_seconds))
                record['exit_code'] = result.returncode
                record['output'] = ((result.stdout or '') + (result.stderr or ''))[-4000:]
            finally:
                record['observed_after'] = identity(candidate['repository_path'])['revision']
                validate(candidate)
                record['after_ok'] = True
            evidence.append(f"exit={result.returncode}: {command}\n{record['output']}")
            if result.returncode:
                verdict, summary = 'FAIL', f'Команда gate завершилась с кодом {result.returncode}'
                if cfg.gate.stop_on_failure:
                    break
        validate(candidate)
    except CandidateError as exc:
        verdict, summary = 'HUMAN_REQUIRED', str(exc)
    except subprocess.TimeoutExpired:
        verdict, summary = 'HUMAN_REQUIRED', 'Gate превысил timeout; незавершённый процесс и протокол требуют проверки оператора'
    except OSError as exc:
        verdict, summary = 'HUMAN_REQUIRED', f'Не удалось запустить gate: {exc}'
    payload = {'candidate_id': candidate['candidate_id'], 'state_version': workflow.state_version,
               'observed_revision': observed_revision,
               'expected_revision': candidate['candidate_revision'], 'repository_path': candidate['repository_path'],
               'commands': records, 'verdict': verdict, 'summary': summary}
    with db._connect(immediate=True) as conn:
        if verdict == 'HUMAN_REQUIRED':
            invalidate(conn, candidate, summary)
        event = db._append_workflow_event(conn, WorkflowEventCreate(
            workflow_id=workflow.id, round_id=candidate['round_id'], event_type='candidate.gate_finished',
            idempotency_key=key.replace('gate_started', 'gate_finished'), payload=payload))
    return WorkflowGateDecision(expected_version=workflow.state_version, verdict=verdict,
                                candidate_id=candidate['candidate_id'], receipt_id=str(event.seq),
                                gate_id='automatic-gate', summary=summary, evidence=evidence)
