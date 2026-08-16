# Cortex Portfolio Control Plane — State
_Last updated: 2026-08-16 by Cortex_

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
- [ ] Review and merge Cortex control-plane draft PR #1 (review; review; owner: owner; progress: 100%; next: Open the local dashboard and draft PR #1, review commit 9ced348, then choose approve-for-merge or request changes.)

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-16T01:40 — codex — Changed Expose GitHub mirror readiness and dry-run in dashboard from running to done (task.status_changed)
- 2026-08-16T01:40 — codex — Delivered and published the on-demand read-only GitHub mirror dashboard preview at commit 9ced348. It shows setup, target, stable identities, fresh plans, conflicts, durable recovery, and exact copy-only commands; 174 Python tests, 6 frontend tests, production build, desktop/mobile browser verification, and all six GitHub CI jobs pass. (pm.session_closed)
- 2026-08-16T01:40 — codex — Changed Review and merge Cortex control-plane draft PR #1 from open to review (task.status_changed)
- 2026-08-16T01:39 — codex — Created work item: Review and merge Cortex control-plane draft PR #1 (task.created)
- 2026-08-16T01:39 — codex — Published commit 9ced348 to draft PR #1; all push and pull_request CI jobs pass on Ubuntu, Windows, and the dashboard production build. (delivery.published)
- 2026-08-16T01:36 — codex — Verified the explicit-click GitHub mirror preview: guarded read-only API, setup/action/conflict/recovery states, exact copy-only CLI command, desktop and 390px browser flows, no apply control, and clean browser logs. (dashboard.github_preview_verified)
- 2026-08-16T01:21 — codex — Changed Expose GitHub mirror readiness and dry-run in dashboard from open to running (task.status_changed)
- 2026-08-16T01:21 — codex — PM session started: Expose GitHub mirror readiness and dry-run in dashboard (pm.session_started)
- 2026-08-16T01:18 — codex — Updated work item: Implement opt-in GitHub Project mirror adapter (task.updated)
- 2026-08-16T01:18 — codex — Accepted the opt-in GitHub Project adapter: complete read-only inventory, strict dry-run, exact issue linking, approval fingerprints, durable idempotent/resumable one-task apply, atomic verified local evidence, protected adapter-owned fields, live zero-write convergence, and full automated verification. (pm.session_closed)

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
