# Planner task failure and SQLite WAL recovery

## Incident and evidence

On 2026-10-10 task 24 for `cost-benchmark-pilot` ran from 14:49:38 to
14:50:24 Asia/Yekaterinburg on release `ca73b75b9b84065f3980df1f228165d4aa7af79e`.
The traceback ends in `is_cancel_requested` → `get_setting` → `SELECT value
FROM settings`, with `sqlite3.OperationalError: disk I/O error`.
`proc.wait(timeout=2)` is the polling interval. The task's actual timeout was
3600 seconds; the worker's global default was unlimited. This was not a timeout
or an established model contract failure.

Read-only inspection of the live installation confirmed:

- Server, flows and worker use the same absolute `PP_DATA_DIR` and explicit
  `PP_ENV_FILE`: `C:\MyProjects\PromptPilot\_local\data` and `_local\trial.env`.
  Their current directories are the immutable release. They run as Enduro;
  directory and database ACLs grant Modify. No credentials were recorded.
- SQLite 3.53.1, local NTFS, WAL, normal locking, synchronous FULL, separate
  connections with a ten-second busy timeout. `_connect` closes each connection.
  There is no shared Python connection between worker threads or processes.
- `integrity_check` returned `ok`; `foreign_key_check` returned no violations.
  The volume reported Healthy and about 58 GB free. The inspected System event
  interval 14:40–15:00 contained no disk/NTFS/storage error evidence. These checks
  do not prove that a transient OS or storage failure can never occur.
- Task 24 is durably `failed`, with the original error, no result, exit code 1,
  zero retries and a completion time. There is no active provider descendant
  of the worker. The original session ID is retained. The queue was empty.
- Workflow remains `awaiting_human`, round 0, no stages. The plan already stored
  the real task error, but event `planner.invalid_output` replaced its meaning
  with a generic contract message.

Local evidence, including a consistent SQLite backup, is in
`_local/evidence/planner-io-20261010` in the original checkout. These private
files are not versioned. The user's three uncommitted specification documents
were copied there with hashes; the original checkout was not changed.

## Reproduction and its limits

Three processes on a NEW disposable WAL database repeatedly opened, read and
closed connections; two also wrote separate settings keys. No provider was
started. The same Python/SQLite runtime produced `SQLITE_IOERR_TRUNCATE` (1546)
on the settings SELECT: twice in 5,317 connections and twice in a separate
2,936-connection run. Keeping one idle WAL connection open produced zero errors
in the initial 2,120-connection comparison.

This reproduces a Windows WAL attachment/teardown failure under connection
churn. It does not establish a damaged database or disk. The incident log did
not retain an extended SQLite code or a Windows error code, so it cannot prove
that task 24 hit precisely the same low-level operation. A Windows-level cause
such as a particular sharing violation remains unproven.

[SQLite WAL documentation](https://www.sqlite.org/wal.html) describes last-client
cleanup and the shared-memory file. [SQLite error codes](https://www.sqlite.org/rescode.html)
distinguish I/O subtypes. A [SQLite forum report](https://sqlite.org/forum/forumpost/1a035dd803)
describes the same extended error during Windows SHM truncation; it is context,
not proof of this incident's Windows error code.

Repeat the isolated probe from the feature checkout:

```powershell
python tools/probe_sqlite_wal.py --seconds 35
python tools/probe_sqlite_wal.py --seconds 45 --anchor
```

Each invocation creates a fresh directory under `_local/sqlite-probes` and
retains its database and JSON report. It never opens the user database. Churn
failure is probabilistic; zero errors is supporting evidence, not a universal
guarantee. The anchor option uses the actual production helper.

## Changes

- Worker holds an idle connection for its lifetime so WAL/SHM are not repeatedly
  torn down between short task/heartbeat connections. Its SELECT is exhausted;
  it holds no transaction and does not block writes or checkpointing. It is
  closed when the worker exits, including an exceptional exit. WAL, timeouts,
  model settings and planner permissions are unchanged.
- SQLite errors log their extended code/name, runtime version, absolute database
  path, process and thread. Rollback errors cannot replace the original error.
  Worker failure text retains the extended code when available (Python 3.11+).
  There is no blanket retry of failed writes or provider invocations.
- Unexpected provider cleanup now waits for the terminated root process before
  releasing ownership. The existing Job Object/process-group boundary kills
  descendants; failure persistence remains fenced to the original attempt.
- Planner execution failure, cancellation, unsuccessful verdict and invalid
  successful output have distinct events and structured failure guidance.
  Only a successfully completed task with a successful verdict is parsed as
  a plan. The original error, result and exit code remain in the plan output.
- Legacy failed projections reconcile once, appending a correcting event;
  existing history is retained. Repeated sync creates no task or duplicate
  event. Terminal workflows and approved plans cannot be revived by a late
  callback. The UI can also explain legacy failures before reconciliation.
- For technical failures the UI shows the original error and a deliberate
  retry action, without a misleading request to fix model instructions.

## Validation and continuing the workflow

Regression tests cover all planner outcomes, legacy history, repeated sync,
late callbacks, escaped error rendering, connection lifetime and checkpointing.
A real Python provider substitute spawns a child; an injected SQLite error on
the two-second poll verifies termination of both, survival of an unrelated
process, durable task failure and workflow classification without retry.
Run `tools/check-custom.ps1` and the six required Tests CI jobs for the exact
candidate commit before publication through `tools/promote_custom.py`.

Deployment is a separate action: never edit the old release. Stop the trial,
prepare the verified new immutable snapshot with the normal launcher (which
backs up data), and start without `-Worker`. Keep task 24 and the workflow.
After checking the error and worker readiness, the operator can choose
**Повторно сформировать план** on the same workflow. It creates a new planner
task with existing role settings and retains task 24 as evidence. This action
can consume quota when an unpaused worker is running; sync and page reload do
not dispatch anything. Do not rerun the old task through a generic task retry.

If an I/O failure recurs with the idle connection, stop further paid attempts
and collect the new extended code and a Windows Process Monitor trace for the
database/WAL/SHM paths and recorded process ID. Diagnose that specific operation
before changing storage, security exclusions or SQLite settings. Never delete
the live WAL/SHM files or recreate the user database as a repair.
