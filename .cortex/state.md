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
- [ ] Review and merge Cortex control-plane draft PR #1 (review; review; owner: owner; progress: 100%; next: Review draft PR #1 at feature commit c08cf62 plus the following Cortex state closeout commit; approve the exact branch for merge or record requested changes.)

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-16T03:22 — codex — Updated work item: Review and merge Cortex control-plane draft PR #1 (task.updated)
- 2026-08-16T03:22 — codex — Changed Add local portfolio roadmap and Gantt view from running to done (task.status_changed)
- 2026-08-16T03:22 — codex — Accepted local Roadmap/Gantt at c08cf62: planned, observed, and unscheduled evidence are distinct; task drill-down and local schedule editing are available; API/build/test/browser acceptance passed. (pm.session_closed)
- 2026-08-16T03:22 — codex — Codex added and verified the local portfolio Roadmap with deterministic schedule semantics, dependency evidence, schedule editing, and responsive desktop/mobile layouts; 176 Python tests, 11 dashboard tests, build, and zero browser console warnings passed. (dashboard.roadmap_verified)
- 2026-08-16T03:08 — grok — Grok's UTF-8 product review conditionally approved the roadmap and correctly identified three implementation gates: lock truthful classification/bar geometry, attribute dashboard schedule edits to the human owner, and keep a slim historical roadmap payload with deterministic active/recent/all scopes. Codex verified those findings against the repository and adopted them. (review.accepted)
- 2026-08-16T03:08 — codex — The first automatic product-review attempt failed in the Windows cp1252 reader before producing a usable artifact; Codex accepted no findings from that attempt and reran the same bounded review in UTF-8 mode. (review.rejected)
- 2026-08-16T03:03 — codex — Changed Add local portfolio roadmap and Gantt view from open to running (task.status_changed)
- 2026-08-16T03:03 — codex — PM session started: Add local portfolio roadmap and Gantt view (pm.session_started)
- 2026-08-16T03:03 — codex — Created work item: Add local portfolio roadmap and Gantt view (task.created)
- 2026-08-16T01:40 — codex — Changed Expose GitHub mirror readiness and dry-run in dashboard from running to done (task.status_changed)

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
