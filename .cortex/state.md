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
- [ ] Expose GitHub mirror readiness and dry-run in dashboard (code; open; owner: codex; progress: 0%; next: Implement the protected on-demand preview endpoint, then add the task-drawer mirror status and copy-command UI without any web apply capability.)

## Recent decisions (last ~10, newest first)
- 2026-08-12 — Require Cortex updates at the start and close of every PM session — The owner needs durable attribution, drill-down evidence, and one visible next task without reconstructing chat history. — source: owner requirement 2026-08-12
- 2026-08-12 — Use Cortex as the canonical portfolio control plane with GitHub Projects as the engineering mirror and Codex as the execution surface — This covers local and hosted projects, preserves AI attribution and evidence, and avoids duplicating GitHub-native collaboration. — source: Codex PM research 2026-08-12

## Recent activity (last 10, newest first)
- 2026-08-16T01:18 — codex — Updated work item: Implement opt-in GitHub Project mirror adapter (task.updated)
- 2026-08-16T01:18 — codex — Accepted the opt-in GitHub Project adapter: complete read-only inventory, strict dry-run, exact issue linking, approval fingerprints, durable idempotent/resumable one-task apply, atomic verified local evidence, protected adapter-owned fields, live zero-write convergence, and full automated verification. (pm.session_closed)
- 2026-08-16T01:18 — codex — Changed Implement opt-in GitHub Project mirror adapter from running to done (task.status_changed)
- 2026-08-16T01:18 — codex — Created work item: Expose GitHub mirror readiness and dry-run in dashboard (task.created)
- 2026-08-16T01:18 — codex — Implemented strict Project and per-item pagination, exact issue linking, field and select-option schema binding, immutable plan fingerprints, deterministic operation IDs, durable replay/recovery records, immediate precondition re-reads, per-write verification, atomic local link/sync commit, and dashboard protection for adapter-owned evidence. Live Project #1 inventory found one item and 25 fields; live task 8cac6170cc12 produced zero actions/conflicts, and the exact apply path returned already_converged with zero writes and no operation record. Python: 167 tests passed; dashboard: 4 tests and production build passed. (github.mirror_adapter_verified)
- 2026-08-16T01:14 — codex — Grok returned progress narration without a verdict or evidence, so Codex rejected it as review evidence despite exit code 0. Gemini fallback then failed authentication because its installed free-tier client is no longer supported. (review.rejected)
- 2026-08-16T01:14 — ollama — Local phi4 adversarial review independently emphasized replay IDs, network/database failure handling, concurrency, authentication boundaries, and recovery tests. Codex accepted those risk categories but rejected its unsupported claims that the pre-adapter code already implemented them. (review.accepted)
- 2026-08-16T01:14 — claude — Claude Opus architecture review identified the apply-critical gaps: dashboard-forgeable sync evidence, missing strict field/option schema, no immutable plan fingerprint, no durable replay/recovery record, and incomplete per-item pagination. Codex verified and integrated those findings. (review.accepted)
- 2026-08-16T01:11 — codex — Configured GitHub Project Nogor-design #1 (Cortex Engineering Mirror) (github.mirror_configured)
- 2026-08-16T00:52 — codex — Changed Implement opt-in GitHub Project mirror adapter from open to running (task.status_changed)

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
