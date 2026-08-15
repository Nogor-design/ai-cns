# Cortex Portfolio Control Plane — State
_Last updated: 2026-08-15 by Cortex_

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
- [ ] Configure GitHub Project views and charts (planning; open; owner: codex; progress: 0%; next: Inspect the Project view and insights UI, then configure the smallest useful Table, Board, Roadmap, and chart set without adding another mirrored issue.)

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-15T23:33 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-15T23:33 — codex — Accepted the live GitHub Projects mirror proof. Project #1 and issue #2 converge by stable IDs with verified Cortex-owned fields; the issue-owned title test, workflow boundary correction, duplicate refusal, stale-link refusal, and final zero-action plan all passed. (pm.session_closed)
- 2026-08-15T23:33 — codex — Created work item: Configure GitHub Project views and charts (task.created)
- 2026-08-15T23:33 — codex — Codex executed the owner-approved one-item proof: configured private Project #1, created ai-cns issue #2, stored stable issue and item node IDs, verified fields after unique operation batches f39605c5db944beca68c63f82ce4167f and 859e7bfeb86d4476b3a2af0010a479ec, disabled four ownership-conflicting workflows, restored the issue open, and proved zero-action convergence plus duplicate/stale refusal. (github.mirror_live_proof_verified)
- 2026-08-15T23:33 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-15T23:29 — codex — Changed Spike GitHub Projects engineering mirror from running to done (task.status_changed)
- 2026-08-15T23:28 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-15T23:27 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-15T23:27 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-15T23:24 — codex — Changed Spike GitHub Projects engineering mirror from blocked to running (task.status_changed)

## Known risks / assumptions
- GitHub CLI Project authorization is present and the first one-item proof passed; any broader or scheduled mirror remains owner-gated until the live adapter and Project views are accepted.
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
