# Cortex Portfolio Control Plane — State
_Last updated: 2026-08-02 by Cortex_

## Goal (now)
Stabilize and dogfood the 1.3.0 portfolio control plane with safe routing and daily evidence

## Stack
Python, Typer, SQLite, Git, Ollama

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
- [ ] Review and commit the 1.3.0 reliability, privacy-policy, and live-output changes
- [ ] Push the local release history after choosing the desired commit boundary
- [ ] Dogfood daily for two weeks and resolve unknown run outcomes before expanding scope

## Recent decisions (last ~10, newest first)
- 2026-08-02 — Treat 1.3.0 as a stabilization checkpoint — Restart recovery, HTTP integration coverage, CI, and responsive QA are release gates — source: release-hardening review
- 2026-08-01 — Never auto-merge agent output — All write work must pass diff, scope, and test review — source: portfolio control-plane implementation
- 2026-08-01 — Use E:\AI-Worktrees for write agents — The E SSD avoids D HDD build contention and isolates writers — source: local hardware inspection
- 2026-08-01 — Limit active work to three program lanes — Protects attention, premium tokens, and repository safety — source: portfolio review 2026-08-01

## Known risks / assumptions
- Existing active repositories are dirty; write dispatch must remain blocked until each is checkpointed.
- Perplexity has no local CLI configured and remains a manual/API research route.
- Local Ollama reviews are inexpensive but require bounded source evidence and premium acceptance for consequential decisions.
- Headless provider output formats may change; adapters must remain covered by smoke tests.
- The new GitHub Actions workflow cannot be proven remotely until the branch is pushed.
- Routing evidence is still sparse: most recorded run outcomes need a human judgment.

## Definition of done (for this project)
- the full test suite passes without writing test artifacts into the repository
- `doctor`, `dashboard`, `digest`, `route`, `dispatch`, `result`, and `judge` work against the live portfolio database
- write routes require an isolated worktree, explicit permission, and a clean canonical repository
- no secrets or client PII leave the machine through a compiled brief
- no agent result is merged automatically
- Python tests, the dashboard production build, and rendered desktop/mobile smoke checks pass
