# Cortex-Lite Portfolio Control Plane

Cortex-Lite is a local command center for working across many repositories and
many AI tools without repeatedly rebuilding context. It keeps compact project
state, a task queue, model-run provenance, Git/test evidence, portfolio health,
and explainable task routing in one SQLite database.

See `AI-CNS-V1-MVP-Spec.md` for the original design.

## What works now

1. **Local visual portfolio dashboard and daily digest** - project lanes,
   recoverable paused/external projects, task assignments, automatic route
   recommendations, worker availability, Git evidence, program, priority, and
   privacy.
2. **Context compiler** - creates a bounded model-ready brief and blocks known
   secrets or PII before it leaves the machine.
3. **Rule-based routing** - recommends a worker, model tier, budget, action, and
   reviewer from task type, risk, privacy, complexity, and acceptance criteria.
4. **Safe headless dispatch** - dry-run by default. Read-only work can use
   Codex, Claude, Gemini, Grok, or Ollama. Write work requires `--allow-write`
   and runs in a separate Git worktree.
5. **Evidence capture** - records command, output, usage metadata when exposed,
   changed files, path-scope violations, test results, and whether work later
   survived into Git history.

No dispatcher merges work automatically.

## Install

```powershell
cd D:\ai-cns
pip install -e .
```

Python 3.11+ is required. Git is required for provenance and worktrees. Ollama
is optional; the configured local default is `phi4:14b`.

## Use the portfolio database

The wrapper keeps operational data in `D:\ai-cns\.cortex\portfolio.db` and
write-agent worktrees on the `E:` SSD.

```powershell
cd D:\ai-cns
.\scripts\cortex-portfolio.ps1 doctor
.\scripts\cortex-portfolio.ps1 dashboard
.\scripts\cortex-portfolio.ps1 digest
.\scripts\write-daily-digest.ps1
```

## Open the visual dashboard

The dashboard reads and writes the same local SQLite database as the CLI. It
binds to `127.0.0.1`, never dispatches or merges work automatically, and keeps
an explicit assignment separate from Cortex's recommendation.

Double-click `scripts\start-cortex-dashboard.cmd`, or run:

```powershell
cd D:\ai-cns
.\scripts\start-cortex-dashboard.ps1
```

The first launch installs the dashboard packages and creates a production
build; later launches go straight to `http://127.0.0.1:8765`. Use `-Rebuild`
after changing frontend source, `-NoOpen` to suppress browser launch, or a
different local port with `-Port 8877`.

Inside the dashboard you can:

- switch between Overview, Strategy Analysis, monetization, infrastructure,
  and Paused / External lanes;
- open a project drawer for its goal, branch, tasks, routing rationale, and
  local path;
- add a bounded task with priority, risk, budget, and acceptance criteria;
- accept the recommended worker or assign Codex, Claude, Gemini, Grok, Ollama,
  Perplexity, or yourself;
- move tasks through assigned, running, review, blocked, and done;
- pause or reactivate a project without deleting its history.

The fast web view reads branch and activity metadata without running a fleet of
Git processes across large or shared repositories. Use the CLI `dashboard`
command when you want a live dirty/ahead/behind probe; the web view labels that
field as deferred instead of presenting stale data as clean.

The `.cmd` wrapper is equivalent:

```powershell
.\scripts\cortex-portfolio.cmd dashboard
```

To preview an optional daily scheduled task without installing anything:

```powershell
.\scripts\install-daily-digest-task.ps1 -At 07:00
```

Add `-Install` only after the printed executable, arguments, and schedule look
correct. The scheduled action writes a local Markdown digest and does not start
an AI worker.

## Register a project

```powershell
.\scripts\cortex-portfolio.ps1 init D:\my-project `
  --name "My Project" `
  --program monetization `
  --priority 1 `
  --privacy internal `
  --stack "Next.js, Postgres" `
  --goal "Reach a paid pilot" `
  --test "npm test"
```

Use `--no-state` for paused/reference projects when you do not want Cortex to
create `.cortex/state.md` yet.

## Add, route, and preview a task

```powershell
.\scripts\cortex-portfolio.ps1 task add my-project "Review launch blockers" `
  --type review `
  --risk low `
  --acceptance "Return the five highest-impact blockers with file evidence" `
  --budget local

.\scripts\cortex-portfolio.ps1 route <task-id>
.\scripts\cortex-portfolio.ps1 dispatch <task-id>
```

`dispatch` is only a preview unless `--execute` is supplied.

## Execute a read-only worker

```powershell
.\scripts\cortex-portfolio.ps1 dispatch <task-id> --execute
```

The compiled brief is scanned locally before execution. Restricted/high-risk
work requires explicit `--approve-high-risk` unless it stays with Ollama.

Review and score the captured result:

```powershell
.\scripts\cortex-portfolio.ps1 result <run-id>
.\scripts\cortex-portfolio.ps1 judge <run-id> --outcome accepted --note "Meets acceptance"
.\scripts\cortex-portfolio.ps1 history my-project --type review
```

Rejected results reopen their task. Accepted results become routing evidence so
future model selection can favor the least expensive worker that succeeds.

## Execute a write task

```powershell
.\scripts\cortex-portfolio.ps1 dispatch <task-id> `
  --execute `
  --allow-write
```

Write execution is refused when the canonical repository is dirty. Cortex then
creates `E:\AI-Worktrees\<project>\<task>` and leaves successful work in
`review`; it does not merge the branch. Use `--allowed-paths` on the task to
enforce path-level scope after the run.

## Existing manual run loop

```powershell
cortex brief my-project --task <task-id> --model codex --mode agentic_cli
cortex run <task-id> --mode agentic_cli --model codex
# run the agent in the repository
cortex done <run-id>
cortex resolve my-project
cortex history my-project --type code
cortex state my-project --regen
```

## Configuration

- `CORTEX_DB` - SQLite database path.
- `CORTEX_WORK_ROOT` - isolated Git worktree root.
- `OLLAMA_HOST` - default `http://localhost:11434`.
- `CORTEX_OLLAMA_MODEL` - default `phi4:14b`.
- API mode reads `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.

## Safety rules

- Do not use bypass-permission or YOLO modes on canonical repositories.
- Do not run write agents until the canonical worktree is checkpointed.
- Keep customer data and proprietary trading material local unless the compiled
  brief has been explicitly reviewed and sanitized.
- Treat `review` as a real gate: inspect the diff and test evidence before merge.

## Tests

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m pytest -q -p no:cacheprovider
```
