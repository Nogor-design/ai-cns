# Cortex-Lite Portfolio Control Plane

Cortex-Lite is a local command center for working across many repositories and
many AI tools without repeatedly rebuilding context. It keeps compact project
state, a task queue, model-run provenance, Git/test evidence, portfolio health,
and explainable task routing in one SQLite database.

See `AI-CNS-V1-MVP-Spec.md` for the original design.

For PM use, start with `PM-OPERATING-MODEL.md` and
`PORTFOLIO-CONTROL-PLANE-RECOMMENDATION.md`.

## What works now

1. **AI project-manager dashboard and expert team** - one Continue action,
   visible working/queued/idle agent states, cached next-move suggestions,
   recoverable paused/external projects, Git evidence, and clear ownership.
2. **Per-project worker allowlist** - each repository declares which CLIs may
   read it. Routing chooses the best worker it is allowed to choose instead of
   recommending one worker and then refusing it.
3. **Live run output** - workers stream as they work, so a long agent run shows
   what it is reading and doing rather than an indeterminate progress bar.
4. **Context compiler** - creates a bounded model-ready brief and blocks known
   secrets or PII before it leaves the machine.
5. **Rule-based routing** - recommends a worker, model tier, budget, action, and
   reviewer from task type, risk, privacy, complexity, and acceptance criteria.
6. **Safe headless dispatch** - dry-run by default. Read-only work can use
   Codex, Claude, Gemini, Grok, or Ollama. Write work requires `--allow-write`
   and runs in a separate Git worktree.
7. **Evidence capture** - records command, output, usage metadata when exposed,
   changed files, path-scope violations, test results, and whether work later
   survived into Git history.

No dispatcher merges work automatically.

## Which worker may see which repository

Privacy is enforced in exactly one place: a per-project allowlist. It answers a
single question - *which workers is this repository willing to expose itself
to?* - and everything else follows from it.

```powershell
.\scripts\cortex-portfolio.ps1 project workers nt-strategy-forge
.\scripts\cortex-portfolio.ps1 project workers nt-strategy-forge claude,ollama
```

The same control is in each project's drawer in the dashboard. Two guarantees
are kept deliberately separate:

- The **allowlist** controls who may read the repository (a privacy question).
- **`--allow-write`** and isolated worktrees control whether files change (a
  blast-radius question).

A project with no explicit allowlist falls back to a default by privacy level:
`restricted` stays local (Ollama only), everything else may use any worker. When
routing wants a worker a project does not permit, Cortex substitutes the best
permitted one and labels the substitution rather than leaving a task that no
button can start.

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
   time, completion state, exit code, and captured response. Open a running item
   to watch its live output as the agent reads files and calls tools; the
   transcript is written to `.cortex/runs/logs/<run-id>.log` and survives a
   dashboard refresh or restart.
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

## Mirror approved work to GitHub Projects

Cortex remains the portfolio source of truth. The GitHub Project is an
engineering view of already-linked issues; Cortex never creates an issue,
matches by title, bulk-syncs, or schedules this adapter.

Configure and verify the target once, then use the read-only commands freely:

```powershell
.\scripts\cortex-portfolio.ps1 github configure cortex-portfolio-control-plane `
  --owner Nogor-design --number 1
.\scripts\cortex-portfolio.ps1 github inventory cortex-portfolio-control-plane
.\scripts\cortex-portfolio.ps1 github link <task-id> `
  https://github.com/<owner>/<repo>/issues/<number>
.\scripts\cortex-portfolio.ps1 github plan <task-id>
```

`github plan` reads every Project, field, and item-field page and prints an
immutable fingerprint plus a deterministic operation ID. It changes neither
portfolio records nor GitHub. When a plan actually has actions, applying it
requires both exact values printed by that fresh plan:

```powershell
.\scripts\cortex-portfolio.ps1 github apply <task-id> `
  --approve <plan-fingerprint> `
  --operation-id <operation-id>
```

The project drawer also has **Check GitHub mirror** for each active task. It is
an explicit, on-demand read-only preview of the same strict plan: target and
stable issue/item identity, actions or conflicts, fingerprint, and any durable
recovery operation are shown together. When a safe CLI action or recovery is
available, the dashboard can copy the exact command, but it has no GitHub apply
endpoint and never runs that command. Portfolio polling does not contact
GitHub; the preview requires the local dashboard Host/Origin guard and action
token before invoking `gh`.

Every field is checked immediately before its mutation, every result is
re-read, and the local Project-item link and sync evidence commit atomically
only after remote verification. Interrupted operations retain their exact
operation ID and completed-action evidence; inspect one with
`github operation <operation-id>` and resume it with the same approved command.

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

The compiled brief is scanned locally for secrets before every execution, local
or not. Dispatch refuses a worker the project's allowlist does not permit; it no
longer refuses read-only work for being high risk, because a review that cannot
run is not safer than one that can.

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

- Keep each project's worker allowlist honest; it is the only thing standing
  between a private repository and a cloud CLI.
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
