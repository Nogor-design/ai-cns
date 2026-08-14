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
- [ ] Implement guarded Task Codex next bridge (code; running; owner: codex; progress: 0%; next: Publish the accepted PM control-plane baseline, then implement copy-prompt fallback and bridge capability detection)

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-14T13:49 — codex — Publish gate caught and resolved the transient worker-probe contract; focused test passed 10 consecutive runs, full Python suite passed, and dashboard tests/build passed. (publish.validation_completed)
- 2026-08-14T13:49 — codex — Changed Resolve transient worker availability state test regression from open to done (task.status_changed)
- 2026-08-14T13:48 — codex — Changed Resolve transient worker availability state test regression from done to open (task.status_changed)
- 2026-08-14T13:47 — codex — Changed Implement guarded Task Codex next bridge from open to running (task.status_changed)
- 2026-08-14T13:47 — codex — PM session started: Implement guarded Task Codex next bridge (pm.session_started)
- 2026-08-12T20:06 — codex — Changed Verify Codex App Server start-resume integration from running to done (task.status_changed)
- 2026-08-12T20:06 — codex — Accepted: live local probe proved exact-CWD discovery, persisted-thread resume, safe ephemeral start, and lifecycle notifications. Codex Desktop visibility was cross-checked by task ID; App Server has no desktop-navigation method, so standalone UI must retain copy-prompt fallback. (pm.session_closed)
- 2026-08-12T20:06 — codex — Created work item: Implement guarded Task Codex next bridge (task.created)
- 2026-08-12T20:04 — codex — Live Codex App Server probe verified exact-CWD thread listing, persisted-thread resume, safe ephemeral thread start, and lifecycle notification capture; Codex desktop project identity was matched by path. (integration.probed)
- 2026-08-12T20:02 — codex — PM session started: Verify Codex App Server start-resume integration (pm.session_started)

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
