"""Durable deployment admission and gate drain; never expires active work."""

from contextlib import contextmanager
import os

from . import db

DRAIN_KEY = 'deployment_drain'
GATE_PREFIX = 'deployment_gate:'


class MaintenanceActive(db.WorkflowConflictError):
    pass


def is_draining():
    return db.get_setting(DRAIN_KEY) is not None


def _status(conn):
    drain = conn.execute('SELECT value FROM settings WHERE key=?', (DRAIN_KEY,)).fetchone()
    gates = [db._json_load(r[0]) for r in conn.execute(
        "SELECT value FROM settings WHERE key LIKE 'deployment_gate:%'")]
    tasks = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status='running'")]
    return {'drain': db._json_load(drain[0]) if drain else None,
            'running_tasks': tasks, 'active_gates': gates,
            'safe_to_restart': bool(drain) and not tasks and not gates}


def status():
    with db._connect() as conn:
        return _status(conn)


def begin(token):
    with db._connect(immediate=True) as conn:
        existing = conn.execute('SELECT value FROM settings WHERE key=?', (DRAIN_KEY,)).fetchone()
        if existing:
            if db._json_load(existing[0])['token'] != token:
                raise db.WorkflowConflictError('another deployment owns the drain')
        else:
            paused = conn.execute("SELECT value FROM settings WHERE key='worker_paused'").fetchone()
            conn.execute('INSERT INTO settings(key,value) VALUES(?,?)', (
                DRAIN_KEY, db._json_dump({'token': token, 'started_at': db._now(),
                                         'was_paused': bool(paused and paused[0] == '1')})))
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('worker_paused','1')")
        return _status(conn)


def finish(token):
    with db._connect(immediate=True) as conn:
        current = _status(conn)
        if not current['drain'] or current['drain']['token'] != token:
            raise db.WorkflowConflictError('deployment drain token does not match')
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('worker_paused',?)",
                     ('1' if current['drain']['was_paused'] else '0',))
        conn.execute('DELETE FROM settings WHERE key=?', (DRAIN_KEY,))
        return _status(conn)


@contextmanager
def gate_activity(workflow_id):
    key = GATE_PREFIX + workflow_id
    with db._connect(immediate=True) as conn:
        if conn.execute('SELECT 1 FROM settings WHERE key=?', (DRAIN_KEY,)).fetchone():
            raise MaintenanceActive('deployment is draining; new gates are deferred')
        if conn.execute('SELECT 1 FROM settings WHERE key=?', (key,)).fetchone():
            raise MaintenanceActive('a gate is already active; wait for its receipt')
        conn.execute('INSERT INTO settings(key,value) VALUES(?,?)', (
            key, db._json_dump({'workflow_id': workflow_id, 'pid': os.getpid(), 'started_at': db._now()})))
    try:
        yield
    finally:
        with db._connect(immediate=True) as conn:
            conn.execute('DELETE FROM settings WHERE key=?', (key,))
