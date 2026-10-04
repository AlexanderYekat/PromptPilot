# Maintenance of AlexanderYekat/PromptPilot

- `origin` is AlexanderYekat/PromptPilot; only this repository is a publication destination.
- `upstream` is ivanarama/PromptPilot. `main` mirrors upstream/main exactly.
- `contributor` is ivantit66/PromptPilot. Import only branches selected in fork-sources.json.
- `custom` combines upstream with selected contributor features. Preserve source commit ancestry using ordinary merges; never force-push or reset user work.
- Create feature or integrate/* branches for changes. Keep the deployed checkout pinned and separate from integration work.
- Before updating, check status, active app processes and origin refs. Preserve uncommitted changes and existing branches.
- For upstream updates, merge upstream/main first, then selected contributor refs. Resolve conflicts preserving both intended behaviours. Read the changed code; do not accept one side wholesale.
- Run tools/check-custom.ps1 and the CI matrix before calling an update ready. Never weaken tests to merge. Preserve useful regression tests from all sources.
- Conflicts or failed validation leave custom and the running app at their last working versions. Preserve the candidate branch and report the blocker.
- When a contributor PR is merged upstream, reconcile its final implementation and record the change before retiring its source from fork-sources.json.
- `_local`, .env, databases, provider credentials and logs are local only. Do not commit them.
- Trial services use port 8421 and `_local/data`, running from immutable `_local/releases/<sha>` snapshots. Production is `C:\MyProjects\workflow-pilot\PromptPilot` on 8420 with data in `C:\MyProjects\workflow-pilot\local-data`.
- `tools/start-custom.ps1` does not start a quota-consuming worker by default. `-Worker` plus an explicit Resume enables agent work. Never enable it just to demonstrate the UI.
- Use `tools/promote_custom.py` to gate publication on the exact commit's local receipt and every required CI job. Never advance custom on an unverified candidate.
- Do not copy production tasks into the trial worker or activate email/GitLab connectors without the user's concrete account/routing instructions.
- Future maintenance may commit and push tested updates to the user's fork. It may not post to or change the other two repositories.
