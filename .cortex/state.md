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
- [ ] Spike GitHub Projects engineering mirror (research; blocked; owner: codex; progress: 90%; next: Owner explicitly approves the displayed Project #1 plus one ai-cns issue mutation set; Codex then executes the bounded proof and verifies zero-action convergence.; blocked: GitHub Project authorization is verified and the live inventory is complete: private Project #1 is empty and ai-cns has no issues. The exact one-Project, one-issue mutation set is documented; external writes remain paused for explicit owner approval.)
- [ ] Add owner-gated Codex execution bridge (code; blocked; owner: owner; progress: 0%; next: Owner decides whether Cortex may start or resume paid Codex turns from the dashboard.; blocked: Explicit owner authorization is required before Cortex may create or resume a Codex task or start a paid model turn.)

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-15T19:17 — codex — Changed Spike GitHub Projects engineering mirror from running to blocked (task.status_changed)
- 2026-08-15T19:17 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-15T19:17 — codex — GitHub Project authorization is verified and the live inventory is complete: private Project #1 is empty and ai-cns has no issues. The exact one-Project, one-issue mutation set is documented; external writes remain paused for explicit owner approval. (pm.session_closed)
- 2026-08-15T19:16 — codex — Prepared the exact external-write preview: rename and document private Project #1, align Status options, add twelve Cortex fields, create one ai-cns proof issue for task 8cac6170cc12, add only that issue, verify stable IDs and fields by re-read, then require zero-action convergence. (github.mirror_preflight_prepared)
- 2026-08-15T19:15 — codex — Codex verified the GitHub CLI project scope, found user Project #1 private and empty, confirmed no ai-cns issues exist, and inspected the 13 default Project fields without mutation. (github.authorization_verified)
- 2026-08-15T19:13 — codex — PM session started: Run live GitHub Projects one-item proof (pm.session_started)
- 2026-08-15T19:13 — codex — Changed Spike GitHub Projects engineering mirror from blocked to running (task.status_changed)
- 2026-08-15T17:06 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-15T17:06 — codex — Accepted the offline mirror foundation: complete-read gate, stable-ID uniqueness, duplicate and stale-link conflicts, durable last-synced values, and zero-action convergence are tested. Live one-item GitHub proof remains blocked only by missing Project authorization. (pm.session_closed)
- 2026-08-15T17:06 — codex — Codex pushed the deterministic GitHub Projects dry-run planner, field ownership contract, schema v6 identity constraints, and 15 mirror tests. (implementation.pushed)

## Known risks / assumptions
- GitHub CLI Project authorization is present; the first Project/issue write remains owner-gated by the exact one-item preflight in `docs/GITHUB-PROJECTS-MIRROR.md`.
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
