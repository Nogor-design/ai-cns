# Cortex Portfolio Control Plane — State
_Last updated: 2026-08-17 by Cortex_

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

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-17T17:46 — codex — Updated work item: Design project blueprint, phases, and collaborative planning workflow (task.updated)
- 2026-08-17T17:46 — codex — Implementation and validation are complete at commit 8c05013; the exact live existing-project preview is preserved without approval, and new-project dogfood awaits an owner-selected real repository. (pm.session_closed)
- 2026-08-17T17:46 — codex — Changed Design project blueprint, phases, and collaborative planning workflow from running to review (task.status_changed)
- 2026-08-17T17:46 — codex — Durable project blueprint planning, phase governance, safe Mermaid rendering, and regression coverage committed on the isolated follow-on branch. (implementation.committed)
- 2026-08-17T17:45 — codex — Passed 231 Python tests, 20 dashboard tests, production build, compileall, git diff check, npm audit with zero vulnerabilities, desktop browser QA, and 390x844 mobile fallback QA. (validation.completed)
- 2026-08-17T17:45 — codex — Gemini reviewer was unavailable because the installed client reported an unsupported or ineligible model; its output was not used as evidence. (delegation.failed)
- 2026-08-17T17:45 — codex — Claude Sonnet code review returned ready-with-fixes; removed the raw HTTP approval fallback and added CLI and crash fail-closed coverage. (delegation.completed)
- 2026-08-17T17:37 — codex — Started an independent read-only code review of restart-safe blueprint approval persistence, migrations, HTTP trust boundaries, Mermaid safety, and regression coverage. (delegation.started)
- 2026-08-17T17:36 — codex — Saved resumable local blueprint discovery (blueprint.draft_saved)
- 2026-08-17T17:33 — codex — Saved resumable local blueprint discovery (blueprint.draft_saved)

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
