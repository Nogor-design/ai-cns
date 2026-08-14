# Cortex Portfolio Control Plane — State
_Last updated: 2026-08-14 by Cortex_

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
- [ ] Spike GitHub Projects engineering mirror (research; open; owner: codex; progress: 0%)
- [ ] Add owner-gated Codex execution bridge (code; blocked; owner: owner; progress: 0%; next: Owner decides whether Cortex may start or resume paid Codex turns from the dashboard.; blocked: Explicit owner authorization is required before Cortex may create or resume a Codex task or start a paid model turn.)

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-14T14:06 — codex — Changed Implement guarded Task Codex next bridge from running to done (task.status_changed)
- 2026-08-14T14:06 — codex — Accepted and pushed the safe copy-only Task Codex next phase; it cannot create/resume threads, start turns, spend tokens, or navigate Desktop. (pm.session_closed)
- 2026-08-14T14:06 — codex — Codex verified the project drill-down, continuation preview, bounded prompt, copy confirmation, mobile layout, and clean browser console. (browser.verified)
- 2026-08-14T14:06 — codex — Codex implemented and pushed the guarded copy-only bridge with exact-repository App Server capability inspection and local action guards. (implementation.pushed)
- 2026-08-14T14:05 — codex — Add owner-gated Codex execution bridge is blocked by Implement guarded Task Codex next bridge (task.dependency_added)
- 2026-08-14T14:05 — codex — Changed Add owner-gated Codex execution bridge from open to blocked (task.status_changed)
- 2026-08-14T14:05 — codex — Created work item: Add owner-gated Codex execution bridge (task.created)
- 2026-08-14T14:05 — Harvey — Harvey reviewed the bridge boundary and required an on-click, exact-repository, copy-only first release with no thread or turn mutations. (architecture.reviewed)
- 2026-08-14T14:05 — codex — Updated work item: Implement guarded Task Codex next bridge (task.updated)
- 2026-08-14T14:01 — owner — Previewed Codex handoff for Implement guarded Task Codex next bridge (codex.launch_previewed)

## Known risks / assumptions
- The current GitHub CLI authorization lacks the `read:project` and `project` scopes needed for Project inspection and synchronization.
- Codex App Server can list, start, and resume tasks, but its protocol cannot navigate the standalone Codex Desktop UI; the browser dashboard must keep a copy-prompt fallback.
- Existing active repositories are dirty; write dispatch must remain blocked until each is checkpointed.
- Perplexity has no local CLI configured and remains a manual/API research route.
- Local Ollama reviews are inexpensive but require bounded source evidence and premium acceptance for consequential decisions.
- Headless provider output formats may change; adapters must remain covered by smoke tests.
- The new GitHub Actions workflow cannot be proven remotely until the branch is pushed.
- Routing evidence is still sparse: most recorded run outcomes need a human judgment.

## Definition of done (for this project)
- tests pass
- app boots
- no secrets in diff
- client-safe data only
