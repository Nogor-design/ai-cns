# Cortex Portfolio Control Plane — State
_Last updated: 2026-08-31 by Cortex_

## Goal (now)
Make every PM session visible, attributable, drillable, and actionable across all projects

## Stack
Python, React, SQLite, GitHub

## Important files
- `cortex/cli.py` - portfolio, task, routing, dispatch, result, and evidence commands
- `cortex/dispatcher.py` - dry-run-first execution and review gates
- `cortex/health.py` - Git/state/task health and daily digest rendering
- `cortex/routing.py` - explainable risk and complexity routing
- `cortex/workers.py` - Codex, Claude, Gemini, Grok, and Ollama adapters
- `cortex/jobs.py` - durable dashboard background jobs and restart recovery
- `cortex/policy.py` - per-project worker privacy allowlists
- `cortex/runlog.py` - bounded live worker transcripts
- `cortex/worktrees.py` - isolated write-agent worktrees on the configured SSD
- `.github/workflows/ci.yml` - cross-platform Python tests and dashboard build gate
- `scripts/cortex-portfolio.ps1` - wrapper for the live portfolio database
- `README.md` - operator runbook

## Open tasks
- [ ] Add owner-gated Codex execution bridge (code; blocked; owner: owner; progress: 0%; next: Owner decides whether Cortex may start or resume paid Codex turns from the dashboard.; blocked: Explicit owner authorization is required before Cortex may create or resume a Codex task or start a paid model turn.)
- [ ] Review and merge Cortex control-plane draft PR #1 (review; review; owner: owner; progress: 100%; next: Review guarded Remove from Cortex workflow at feature commit 2398fb1 in draft PR #1)
- [ ] Design project blueprint, phases, and collaborative planning workflow (planning; review; owner: codex; progress: 97%; next: Review fingerprint 8a9fa3ac07a52f5178d1998ab5b2ed02ff33126123e206b15139b6329624d94d and approve or reject it.)
- [ ] Build project blueprint (planning; in_progress; owner: owner; progress: 90%; next: Owner reviews and approves or rejects the exact saved blueprint preview.)
- [ ] Surface the current PM slice and next action in project drill-down (code; review; owner: ollama; progress: 0%)
- [ ] Design end-to-end Project Studio lifecycle and dashboard (planning; review; owner: codex; progress: 0%; next: Owner decides whether to adopt Cortex Studio as an extension of Cortex; if approved, implement the read-only Change Package and Artifact Registry vertical slice before any execution or release mutations.)

## Recent decisions (last ~10, newest first)
- 2026-08-19 — Use Cortex by default for multi-session, multi-agent, high-risk, or owner-gated work; retain a quick path for one-session prototypes. — This study found comparable application quality but materially better traceability and handoff, with significant setup friction for a small build. — source: Decision Garden A/B study 2026-08-19
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-31T16:37 — codex — Assessment complete: Cortex is technically healthy and locally usable, but its useful product state is trapped on an unmerged branch and its owner decision queue is inflated by stale and unverified review items. The recommended order is release consolidation, decision-inbox hygiene, then three-project blueprint/phase dogfood before new Studio scope. (pm.session_closed)
- 2026-08-31T16:37 — codex — Changed Assess current usefulness and define the next product slice from running to done (task.status_changed)
- 2026-08-31T16:37 — codex — Verified the release gap, passing automated and browser checks, 18-item owner decision queue, stale review evidence, sparse routing outcomes, and zero active blueprint phases; recommended release consolidation followed by decision-inbox and dogfood slices. (assessment.findings_verified)
- 2026-08-31T16:29 — codex — PM session started: Assess current usefulness and define the next product slice (pm.session_started)
- 2026-08-31T16:29 — codex — Changed Assess current usefulness and define the next product slice from open to running (task.status_changed)
- 2026-08-31T16:29 — codex — Created work item: Assess current usefulness and define the next product slice (task.created)
- 2026-08-31T00:58 — codex — Changed Fix blank blueprint onboarding and lifecycle visibility from running to done (task.status_changed)
- 2026-08-31T00:58 — codex — Blueprint onboarding and lifecycle visibility are complete: recoverable startup, immediate refresh, exact review fingerprint, legacy-state reconciliation, and reopen flow are verified. (pm.session_closed)
- 2026-08-31T00:58 — codex — Reconciled legacy saved-review lifecycle without mutating on read; invalid fingerprints fail closed. Verified 232 Python tests, 23 dashboard tests, production build, diff checks, browser drawer state, exact fingerprint reopen, console health, and persistent review repair. (verification.completed)
- 2026-08-31T00:57 — codex — Saved resumable local blueprint discovery (blueprint.draft_saved)

## Known risks / assumptions
- The one-task GitHub adapter is implemented and every live apply remains exact-fingerprint gated; bulk mirroring, automatic issue creation, background polling, and scheduled sync do not exist.
- Codex App Server can list, start, and resume tasks, but its protocol cannot navigate the standalone Codex Desktop UI; the browser dashboard must keep a copy-prompt fallback.
- Perplexity has no local CLI configured and remains a manual/API research route.
- Local Ollama reviews are inexpensive but require bounded source evidence and premium acceptance for consequential decisions.
- Headless provider output formats may change; adapters must remain covered by smoke tests.
- Routing evidence is still sparse: most recorded run outcomes need a human judgment.

## Definition of done (for this project)
- tests pass
- app boots
- no secrets in diff
- client-safe data only
