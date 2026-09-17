# Autonomous Operations Plan

Owner decisions recorded 2026-09-17. This plan turns Cortex from a dashboard the
owner drives into a manager that keeps local AI agents working within explicit
quota, compute, and safety limits. It is the controlling document for this work:
any change of scope, order, or guardrail is an owner decision recorded here
before code changes.

Related designs: `docs/LONG-HORIZON-IDEA.txt` (AVO notes),
`D:\autonomous-long-horizon-cns\autonomous-long-horizon-cns-design.md` (durable
executor design, sections 11 and 14), and this repository's README.

## 1. Problem

The owner is the bottleneck. Codex, Claude, Antigravity, Grok, Gemini, opencode,
and Ollama models all run headless, but they wait for the owner to start, check,
and accept each piece of work. Subscription quotas are finite and watched by hand
on provider websites. Local models share one 12 GB GPU.

## 2. Owner decisions (2026-09-17)

| Decision | Resolution |
| --- | --- |
| Unattended starts of paid turns | **Allowed** for Codex and Claude (subscription token limits, no per-call spend). Other subscription CLIs follow the same rule. |
| Quota reserve | Adjustable in the dashboard. **Default 30%** held back for the owner's interactive use. |
| Auto-merge | Allowed into a per-project `cortex/integration` branch only, behind the gates in Phase 3. Never `main`/`master`, never a push. |
| Trading repositories | **Excluded** from all unattended work. |
| Apollo | **Excluded** from all unattended work (production application; owner monitors everything). |

Exclusion means: no unattended starts, no auto-merge, no autonomous task
generation. The owner can still dispatch by hand as today.

## 3. Target architecture

```
Owner ── Decision inbox (batched, daily cap)
   ▲ escalations only
Cortex manager
   Autonomy policy ─ protected projects, per-project autonomy mode
   Capacity ledger ─ provider quota windows, reserve, cooldowns
   Local lanes     ─ one GPU slot, CPU yield to NinjaTrader, benchmarks
   Scheduler       ─ filter eligible workers, rank, start, lease, recover
   Supervisor      ─ stagnation / loop / overrun detection
   Scoreboard      ─ per (worker, task type) acceptance and tokens per accept
        │ bounded task envelope (brief, turn cap, timeout, schema)
Workers: codex exec │ claude -p │ agy -p │ grok │ gemini │ opencode+ollama │ ollama API
        │ read-only in repo, or write in isolated worktree
Verification gate ─ tests, path scope, protected files, second-model review
        │
cortex/integration branch (merge --no-ff, local only)
        │
Owner merges integration → main at phase gates
```

Cortex stays the single manager. The CNS runtime's M2 patterns (leases, fences,
explicit recovery, conservative accounting) are reused as designs, not imported
as a second database.

## 4. Hard guardrails (all phases)

1. Protected projects (trading, Apollo) are identified by built-in rules in code
   plus the Trading Capability Hub registry. The dashboard/API cannot lift them;
   only a reviewed code change can.
2. Unattended work never pushes, never touches `main`/`master`, never deletes
   branches or worktrees it did not create, and never uses permission-bypass flags
   (`--dangerously-skip-permissions`, `--always-approve`, `--yolo`).
3. Quota: an unattended start requires every known provider window to stay below
   `100 - reserve` after the expected cost of one run. Unknown quota allows at
   most one unattended run at a time for that provider until a reading arrives.
   A rate-limit error puts the provider on cooldown until its reset time.
4. Local compute: one local model slot. While NinjaTrader is running, unattended
   local work is limited to models that fit entirely in GPU memory.
5. Every unattended action is attributed (`actor=cortex-scheduler`) in the
   activity log with the quota/lane evidence used to allow it.
6. The owner can pause all autonomy with one control; pausing stops new starts
   immediately and lets in-flight runs finish or time out.

## 5. Phases

Each phase lists scope, explicit non-goals, and exit criteria. A phase is done
only when its exit criteria are verified and recorded in Cortex.

### Phase 1 — Guardrails, capacity visibility, adapters (done)

Scope:
- `cortex/autonomy.py`: protected-project rules (trading via Hub registry +
  built-in roots + `trading-systems` program; Apollo by path/name), per-project
  `autonomy_mode` (`off` | `read_only` | `integration`; default `read_only`,
  protected forced `off`), and a global pause switch.
- `cortex/capacity.py`: owner settings (reserve %, default 30), quota snapshots
  from real provider signals, cooldowns, and an `admit(provider)` decision.
  - Codex: `rate_limits` in `~/.codex/sessions/**/rollout-*.jsonl` (account-wide
    `used_percent`, `window_minutes`, `resets_at`) and in dispatched run output.
  - Claude: `rate_limit_event` in `claude -p --output-format stream-json`
    output (`five_hour` / `seven_day` utilization and reset times).
  - Grok, Gemini, Antigravity: no quota signal yet → call-count cap per rolling
    5 hours (default 10 unattended runs) and cooldown on rate-limit errors.
- `cortex/lanes.py`: GPU/RAM inventory, Ollama model classification (fits GPU,
  hybrid MoE, CPU-only, avoid), single local slot, NinjaTrader yield, and a local
  benchmark that records tokens/second.
- Worker adapters: `agy` (Antigravity, `--print=<pointer to brief file>
  --output-format stream-json --sandbox`; `--mode accept-edits` only for write
  routes, since headless mode auto-denies permission prompts) and `opencode` (local-only: models
  must be `ollama/<name>`, Cortex-owned config via `OPENCODE_CONFIG`).
- Existing unattended entry point (`keep-working`) honors autonomy mode, pause,
  quota admission, and the local slot.
- Dispatch records quota signals from every run.
- API: `GET /api/capacity`, `POST /api/capacity/settings`,
  `POST /api/capacity/refresh`. Dashboard capacity panel with reserve slider,
  per-provider window bars, local lane status, autonomy pause, and the list of
  excluded projects.
- CLI: `cortex capacity show|refresh|reserve|pause|resume`,
  `cortex capacity bench <model>`.

Non-goals: no scheduler daemon, no write autonomy, no auto-merge, no ranking
changes, no new task generation.

Exit criteria:
- Trading Capability Hub, Apollo, and trading-named projects report `off`
  and cannot be changed through the API.
- Live dashboard shows real Codex and Claude window usage with reset times and
  the 30% reserve line; changing the slider persists.
- `keep-working` refuses providers above the reserve and explains why.
- `agy` and `opencode` adapters pass unit tests and one real smoke run each.
- Full Python suite and dashboard build/tests pass.

Measured while planning (2026-09-17): Codex weekly window 78% used (so Codex is
above the default 70% background ceiling until reset); Claude five-hour 29%,
seven-day 14%. opencode's system prompt is ~17.7k tokens, and a trivial turn on
`qwen3-coder:30b` took ~56 s with the model split 58% CPU / 42% GPU.

### Phase 2 — Unattended scheduler and supervisor (implemented; soak pending)

Scope: a single scheduler loop (`cortex autopilot`) with a database lease so only
one runs; per-run heartbeat and timeout; restart recovery that marks orphaned
runs as `unknown` (no blind replay); worker selection = filter (policy, autonomy,
quota, lane, availability) then rank (Phase 4 plugs in a better ranker);
supervisor that stops runs on repeated identical failures or no progress;
owner decision inbox with a daily cap; Windows scheduled-task installer that the
owner runs explicitly.

Non-goals: write autonomy, auto-merge.

Exit: 8-hour read-only soak across at least three providers with no quota
breach of the reserve, no duplicate runs after a forced restart, and every start
explained in the activity log.

Added during implementation (owner request to fold in the Trading Capability
Hub): a task-content trading guard. Project rules alone miss a trading
procedure filed under an ordinary project, so unattended admission also asks
the Hub's own playbook router (loaded read-only by file path) and a built-in
pattern list. A broken or missing Hub falls back to the patterns, never to
"allowed".

### Phase 3 — Verification gates and integration auto-merge

Scope: write tasks run in isolated worktrees branched from `cortex/integration`;
gate = task tests pass, project test command passes on the merged result, path
scope respected, no protected files changed (CI, lockfiles, secrets, agent
configs, `.cortex/blueprint.md`) without an inbox decision, second-model review
passes against the acceptance criteria; merge with `--no-ff` for easy revert;
conflicts go to the inbox, never auto-resolved.

Exit: 20 synthetic and 5 real low-risk tasks merged or rejected correctly;
reverting any merge restores a passing integration branch.

### Phase 4 — Skill scoreboard and token efficiency

Scope: per (worker, model, task type) acceptance rate, tokens per accepted task,
review pass rate, median time; ranking function (owner-authored weights);
cascades (local brief preparation, local first pass, escalate on failed gate);
shared skill cards rendered into each tool's format.

Exit: A/B over a fixed task set shows lower tokens per accepted task with no
drop in acceptance.

### Phase 5 — Continuous projects

Scope: approved blueprint phases decompose into tasks automatically for
`integration`-mode projects; phase review packets for the owner; next phase
waits for owner approval.

Exit: one real project completes a phase end to end with owner involvement only
at the phase gate.

### Phase 6 — Hardening

Scope: 24-hour soak, provider outage injection, backup/restore of the portfolio
database, dashboard alert thresholds.

## 6. Drift controls

- Work happens on `claude/autonomous-operations` (worktree
  `E:\AI-Worktrees\ai-cns-autonomous-ops`); nothing merges to `master` without the
  owner.
- Each phase starts by re-reading this document and ends by recording results
  in Cortex and in the phase log below.
- Anything discovered that is outside the current phase is recorded as a
  follow-up, not implemented.

## 7. Phase log

- 2026-09-17: Plan written; Phase 1 started.
- 2026-09-17: Phase 1 implemented on `claude/autonomous-operations`. Verified:
  251 Python tests and 24 dashboard tests pass; dashboard builds; the capacity
  panel was exercised in a browser against a copy of the live portfolio
  database (reserve slider persisted, Codex flipped from held to allowed at a
  15% reserve); `cortex capacity show` on that copy listed Trading Capability
  Hub, apollo-ats and trader-dan-landing as protected `off`. Real smoke runs:
  `agy` review 16 s / ~124k input tokens (mostly cached context); `opencode` +
  `qwen3-coder:30b` review 58 s / ~18k local tokens; neither changed files.
  Remaining Phase 1 exit item: owner review, then merge so the live dashboard
  uses schema v14.

- 2026-09-17: Owner authorised merging all pending Cortex work. The abandoned
  blueprint-review fix was committed (9402c5b), Phase 1 merged (3fe47be), and
  `master` fast-forwarded; 252 Python and 27 dashboard tests passed.
- 2026-09-17: OpenCode Go added (owner direction). `opencode` now means the
  owner's OpenCode Go subscription (default `opencode-go/deepseek-v4.1-flash`);
  the local Ollama variant is `opencode-local`. Medium code implementation routes
  to OpenCode Go instead of Gemini. Go quota is dollar-based: spend from
  opencode's local database over rolling 5-hour/7-day/30-day windows against an
  owner-set monthly limit (default $60, the DeepSeek V4.1 Flash cap; 5 hours =
  20%, week = 50%). An unattended end-to-end review cost $0.0033 (19.5k input,
  38.5k cached, 293 output tokens) and changed no files.

- 2026-09-17: Phase 2 implemented on `claude/autopilot`
  (`cortex/autopilot.py`, `leases.py`, `supervisor.py`, `inbox.py`,
  `trading_guard.py`; schema v15 adds `leases`, `inbox`, `jobs.heartbeat_at`,
  `jobs.started_by`). Verified on a copy of the live database with a scratch
  repository: one tick started OpenCode Go, Antigravity and Grok together; all
  three finished read-only reviews in 9-18 s and found the planted bug (Go cost
  $0.0032). Killing the scheduler mid-run and restarting it marked the run
  `unknown`, blocked the task, filed an inbox item and did not replay it; the
  new holder took the lease at a higher fence. A second scheduler exited with
  code 3. 274 Python and 28 dashboard tests pass; the dashboard builds.
  Not yet done: the 8-hour soak. The live portfolio has no `assigned` tasks
  (15 wait in `review`, 2 in progress), so an installed scheduler would idle
  until tasks are approved -- which is Phase 5's problem to solve, not a reason
  to invent work.

## 8. Phase 1 findings and follow-ups

- OpenCode Go spend is read only from this machine's opencode database, and its
  windows are rolling approximations of Go's own. The OpenCode console
  (opencode.ai/auth) is authoritative; if other machines use the same key,
  lower the monthly limit in the dashboard to compensate.
- Cortex only accepts `opencode-go/*` models for the `opencode` worker; other
  opencode providers such as Zen bill per use and are refused.


- Codex's weekly window was 79% used, above the default 70% background ceiling,
  so Codex is held from unattended work until it resets (2026-09-19 16:05 UTC).
- A settings-free Haiku probe (~6.5k tokens) is the only way to read Claude's
  windows between runs; it runs at most every 15 minutes and only when an
  unattended start needs it or the owner presses Refresh quota.
- `agy` headless mode auto-denies commands and edits without accept-edits, but
  its file tools are not confined to the workspace or `--add-dir`. Treat its
  read scope like any other cloud CLI. A vague brief made it wander (26 steps,
  134k tokens) during testing; Phase 2's supervisor needs turn/token caps for it.
- opencode's system prompt is ~18k tokens, so every local agent turn pays that
  prefill. Prefer direct Ollama calls with compact briefs for triage (Phase 4).
- `trader-dan-landing` is excluded by the trading name rule. If it should be
  eligible, that is an explicit owner decision and a code change.
- Unattended same-provider starts from two concurrent requests can both pass
  the unknown-quota check; Phase 2's scheduler lease removes this race.
- Live NinjaTrader was running during testing, which correctly held hybrid
  models (including opencode's default `qwen3-coder:30b`) from unattended work.

## 9. Phase 2 findings and follow-ups

- The Trading Capability Hub router accepts a half-phrase match (score 0.5), so
  one shared word matches a two-word phrase: "Summarize open PRs" routes to
  `nt-ensure-ready`. Cortex requires a majority (> 0.5). The Hub was not
  edited because another agent had uncommitted work there; its routing tests
  should add an unrelated two-word near-miss.
- The guard still refuses some non-trading phrasings ("optimize these parameters
  for the build cache"). That is the safe direction: the task is held with a
  reason, not run.
- `D:\trading-knowledge` is not registered in Cortex. Its name makes it
  protected if it is. Its governed authoring loop (local Ollama drafts ->
  schema and reference gates -> apply as `draft` only) is the pattern Phase 3's
  verification gate should copy, and its indicator curation is the kind of
  local-lane work that could run unattended if the owner exempts it. That
  exemption is an owner decision and a code change.
- The Hub's generated-context drift check (registry -> rendered bundle ->
  installed file, failing hard on stale installs) is the model for Phase 4's
  skill cards, rendered once per tool from one source.
- Both repositories expose read-only MCP servers (`trading-hub`, `tk`). Cortex
  does not hand them to unattended agents; trading work is out of scope.
- Open-source components from the CNS design (NVIDIA NOOA, NeMo Switchyard,
  OpenShell, Temporal/LangGraph) were not adopted. Phase 2 needed a lease and a
  watchdog, which SQLite and the existing job table provide. Revisit
  **OpenShell** in Phase 3 as the write-isolation backend (WSL2 support is
  experimental; adopt only after its enforcement tests pass here) and
  **Switchyard** in Phase 4 as an A/B against the rule router. NOOA adds typed
  agent objects that Cortex's CLI-boundary design does not need.
- The CNS runtime's M2 work is now committed with evidence
  (`docs/m2-evidence.md`); its fenced recovery is what `leases.py` borrows.
- Supervisor limits are first guesses (`cortex/supervisor.py:WORKER_LIMITS`).
  Tune them from the soak's stopped-run reasons before Phase 3.

