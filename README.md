# Cortex-Lite Portfolio Control Plane

Cortex-Lite is a local command center for working across many repositories and
many AI tools without repeatedly rebuilding context. It keeps compact project
state, a task queue, model-run provenance, Git/test evidence, portfolio health,
and explainable task routing in one SQLite database.

See `AI-CNS-V1-MVP-Spec.md` for the original design.

## What works now

1. **AI project-manager dashboard and expert team** - one Continue action,
   visible working/queued/idle agent states, cached next-move suggestions,
   recoverable paused/external projects, Git evidence, and clear ownership.
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

The intended daily workflow is deliberately short:

1. Start in **Your expert team**. Each CLI has a specialist role, current or
   next assignment, model/effort, availability, run success, and captured token
   usage. Click **Keep team moving** to start up to three already-approved,
   read-only, low-risk assignments across otherwise idle experts.
2. Click **Continue with AI**. Cortex first points to a review, blocked item,
   assigned run, or existing recommendation without spending an AI token.
3. If a project has no actionable work, select it and click its explicitly
   named **Plan PROJECT** button.
   Smart mode asks the Codex CLI for three bounded proposals; Local mode asks
   Ollama. Plans are cached, and a deterministic no-token plan is used if the
   selected CLI is unavailable.
4. Click **Approve & start** on one proposal. Cortex converts only that proposal
   into a task, assigns the routed worker, and starts read-only work. Code edits
   require one explicit confirmation and run in an isolated Git worktree.
5. Follow **Execution activity** to see the actual worker, model/effort, start time, elapsed
   time, completion state, exit code, and captured response. This evidence
   survives a dashboard refresh.
6. Use **Check GitHub** to refresh stored working-tree and remote ahead/behind
   evidence. The scorecard learns success by model, effort, and task type.
7. Return when the item appears in **Needs your decision**. Cortex never merges
   work or takes an external action automatically.

The planner and executor are deliberately labeled separately. For example,
Codex may propose a task that Cortex routes to Ollama for inexpensive local
execution; the dashboard shows both roles instead of calling both “Codex.”

Manual task entry remains under **More** for known one-off work; it is not the
primary path.

The CLI mirrors the same loop:

```powershell
cd D:\ai-cns
.\scripts\cortex-portfolio.ps1 focus
.\scripts\cortex-portfolio.ps1 team
.\scripts\cortex-portfolio.ps1 keep-working
.\scripts\cortex-portfolio.ps1 plan nt-strategy-forge --worker codex --allow-cloud
.\scripts\cortex-portfolio.ps1 approve <suggestion-id> --start
.\scripts\cortex-portfolio.ps1 git-check --fetch
```

Use `--worker ollama` for a local plan. Use `--queue` instead of `--start` to
approve and assign without running the worker. Implementation requires the
additional `--allow-write` flag and always uses an isolated worktree.
Projects marked `restricted` use the local/no-token planner unless Codex access
is explicitly approved with `--allow-cloud` (or the matching dashboard prompt).

The web view reads persisted Git checks so it stays fast. **Check GitHub** (or
`git-check --fetch`) refreshes dirty/ahead/behind evidence for active projects.
Token totals include only per-run usage that a CLI reports to Cortex; provider
subscription quotas are not universally exposed and are labeled accordingly.

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
