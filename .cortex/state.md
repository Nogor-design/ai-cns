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
- [ ] Implement opt-in GitHub Project mirror adapter (code; open; owner: codex; progress: 0%; next: Implement the read-only paginated GitHub inventory and explicit dry-run command before adding any live apply path.)

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-16T00:47 — codex — Accepted the GitHub Project operational layer: saved Table, Status Board, date-mapped Roadmap, and two useful Insights charts, all verified after reload with one approved proof item and no additional issue or automation. (pm.session_closed)
- 2026-08-16T00:47 — codex — Changed Configure GitHub Project views and charts from running to done (task.status_changed)
- 2026-08-16T00:47 — codex — Updated work item: Configure GitHub Project views and charts (task.updated)
- 2026-08-16T00:47 — codex — Configured and independently reloaded Work Queue, Flow, and Timeline in private Project #1. Timeline maps Start date to Start date and Target date to Target date and renders the approved proof item on 2026-08-15. Added and reloaded Work by status and Work by priority charts; GitHub does not expose the custom text Worker field as a chart axis, so worker attribution remains visible in Work Queue and Flow. GraphQL confirms exactly three views and one Project item; the repository still has exactly issue #2. Pytest passed 153 tests. (github.project_views_configured)
- 2026-08-16T00:47 — codex — Created work item: Implement opt-in GitHub Project mirror adapter (task.created)
- 2026-08-16T00:37 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-16T00:36 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)
- 2026-08-16T00:29 — codex — Changed Configure GitHub Project views and charts from open to running (task.status_changed)
- 2026-08-16T00:29 — codex — PM session started: Configure GitHub Project operational views (pm.session_started)
- 2026-08-15T23:33 — codex — Updated work item: Spike GitHub Projects engineering mirror (task.updated)

## Known risks / assumptions
- GitHub CLI Project authorization is present, the one-item proof passed, and the Project views are accepted; any live adapter apply, broader mirror, or scheduled sync remains owner-gated.
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
