# OneBase: delivery-first operation

This is an operator deployment/configuration policy, not a replacement for the
canonical OneBase skills, independent review or GitHub protection.

## Release unit

Deploy server, worker and bot from the **same frozen `pp` binary**. A release
manifest records the PromptPilot commit, OneBase procedure commit, health-tool
commit and SHA256 of every artifact. Different repositories necessarily have
different commit IDs; the release ID is their explicit pairing.

`tools/install_onebase_release.py prepare` records immutable artifacts.
`install` requires a paused and idle worker, saves configuration and exact series
prompts, uses transactional prompt CAS, and switches the three LaunchAgents.
It preserves unrelated profiles, manual/automatic series pauses, lease keys,
identity rules and required checks. Pending occurrences receive the same prompt
version. Clean project health checkouts are not patched with overlay files.
Installation leaves the worker paused for verification; the operator resumes it
only after checking the new heartbeat, paired configuration and capabilities.
Installation failures restore touched files/prompts/services, unless a concurrent
edit makes rollback unsafe. Backups stay in the release's `before` directory.
There is no destructive whole-database restore.

Stage prompts bind the canonical `go run ./tools/pipelinehealth -json` check to
the full `health_command` in the installed `pipelinectl-onebase.json`, including
its contract, transport and cache flags. This prevents a stage from silently
running an older checker from its clean working checkout. Global owner/allowlist
checks and local mutation gates remain mandatory.

Procedures taken from an unmerged PR are an explicitly installed operator hotfix,
**not** evidence that the PR is approved, shipped or merged. Relative skill
references are resolved inside the paired snapshot. Its CLAUDE.md is paired too,
so a new skill cannot silently conflict with an old project-policy clause.

## Intake pressure

Initial intake threshold: **10 active PRs**, including review backlog, reviewed
PRs waiting for ship, merge candidates and PR reworks. Count unique PR numbers;
issues, hold/decision-only items and duplicate diagnostic memberships do not
inflate that count. Existing backlog is neither closed nor rewritten.

Above the threshold, ordinary FIX/PLAN intake waits without launching an agent.
PR rework (`fix_candidates.stage=review`) and critical candidates (`priority=0`)
remain available, but the executor is restricted to those exact candidate
numbers. The candidate still needs fresh canonical eligibility/auth checks.
Disappearing exceptions do not reopen ordinary intake during the same run.
REVIEW and MERGE continue; TRIAGE may classify incoming issues without creating
new implementation PRs.

This is **admission backpressure, not an atomic hard WIP limit**: cached state,
already running tasks and external authors can exceed the threshold. Missing,
stale or incomplete snapshots defer intake. An expired snapshot is refreshed
through the existing shared GitHub scan lease and resource budget, not via a
model or a new unbudgeted API route. The threshold is operator-owned and can be
changed after observing delivery throughput.

## GitHub budget fairness

An aged waiter receives a scheduling opportunity, not an unlimited reservation.
If its own admission still fails because another running task occupies the
required GitHub budget, it yields its aging baton for that scope. Its waiter,
priority and resource floors remain in place; only the fairness age restarts.
The ordinary retry delay is respected and cheaper eligible delivery work can
proceed. A future starvation window provides another opportunity. Priority,
scan-lease and fairness deferrals do not themselves reset the baton.

## Outcome and maintenance budget (#1545)

`pipeline_watch` distinguishes agent-reported DONE from GitHub-confirmed merges.
It reads all closed-PR pages needed to cover the observation window, compares
UTC instants rather than timestamp strings, and reports completed runs and
observed uncached-input/output tokens per confirmed merge. These ratios describe
the same observation window: they are **not** per-PR causal attribution, money
spent or subscription quota. Missing usage stays explicitly unmeasured. A failed
GitHub refresh makes delivery ratios unknown instead of pretending the queue
delivered nothing. The legacy `productive_runs` field is retained for consumers;
it is only a DONE-verdict count, not a delivery counter.

Monthly methodology: record confirmed merges, classify each into product,
pipeline maintenance, documentation/planning or unresolved classification;
record queue ages, human interventions and repeated unchanged target/HEAD
handoffs separately. Keep raw evidence links and classification decisions.
Do not infer maintenance from loose title matching or count base-sync churn as
product delivery. A maintenance share **over 25% of classified monthly merges**
is a review signal, not a reason to bypass gates or block urgent infrastructure
fixes. With incomplete classification, publish coverage and do not call the
budget met. Compare run/token trends and actual shipped product outcomes after
one month before making further architectural changes.

This first increment implements release pairing, intake backpressure and watch
delivery ratios. Automatic monthly classification, target/HEAD churn accounting
and their dashboard presentation remain separate work; this document does not
claim those metrics already exist or that #1545 is fully implemented.
