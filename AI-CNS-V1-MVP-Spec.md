# AI CNS — Version 1 MVP Build Spec (Revised)

**Working name:** Cortex-Lite
**One-line purpose:** A local memory-and-context layer for working across many projects with many models — that captures what each model did *as a side effect of normal work*, and compiles current project state into a model-ready brief on demand.

This is a deliberate departure from the original "Version 1: Task Router." The router framing puts routing/prompting at the center and memory at the edge. This spec inverts that. The center is **durable project memory + a context compiler**; routing is reduced to lightweight, queryable history that informs *your* decision rather than automating it.

---

## 1. What this MVP is and is not

**It is:**
- A canonical, regenerated state document per project (the source of truth).
- A context compiler that turns that state into a tight brief for any model/session.
- A provenance log of model runs that fills itself from git + tests + API responses.
- A read-only decision-support view of model history per task type.

**It is explicitly NOT (in V1):**
- A learned/automated router. (Sample sizes are too small to be honest; deferred.)
- A multi-agent autonomous executor. (Deferred to V2+.)
- A local fine-tuning pipeline. (Deferred to V4+; likely never for judgment tasks.)
- A 7-page dashboard. (One project view + one global activity log.)
- A manual copy-paste-and-rate treadmill. (Capture is automatic wherever possible.)

**Design rule that governs every decision:** *Capture is a byproduct of work, never a separate task.* If a feature requires the user to remember to log something, it is wrong until proven otherwise.

---

## 2. The three execution modes (the core insight)

The original spec assumed one execution path: generate a prompt, the human pastes it into a model, the human pastes the result back. That path is the *worst* one and should be the fallback, not the default. Work actually happens in three modes, each with a different — and mostly automatic — capture mechanism.

| Mode | Models | How work happens | How outcome is captured |
|---|---|---|---|
| **Agentic CLI** | Codex, Claude Code | Cortex writes a task brief; you run the agent in the repo | **Automatic** — git diff before/after + test results + agent transcript |
| **API chat** | Gemini, Perplexity, Grok, Claude API, Ollama (local) | Cortex calls the API directly with the compiled brief | **Automatic** — response stored with the run |
| **Manual UI** | Any model you only have as a web subscription | Cortex generates a prompt; you paste, you paste back | **Manual** — accept it will be lossy; minimize its use |

The MVP must make Agentic CLI and API modes first-class. Manual is the degraded path for tools without programmatic access.

> Practical note on agentic tools: Codex/Claude Code are not chat endpoints. Cortex's job for them is (a) compile a strong task brief, (b) record the repo's git HEAD, (c) let you run the agent, (d) on return, diff the repo, run the test command, and store the result as a run. The user action is just "run the agent" — everything around it is captured automatically.

---

## 3. The value, ranked (build in this order)

1. **Context compiler** — *highest value, zero ML, build first.*
   Turn canonical project state into a model-ready brief: goal, stack, important files, recent decisions, open tasks, known risks, definition of done, constraints. Kills the #1 daily tax: re-explaining context to every model every session.

2. **Self-filling provenance log** — *the only durable record worth having.*
   Every run (agentic/API/manual) is logged with what changed, whether tests passed, and whether the change survived into a commit. This is the real evaluation signal — implicit, not a 1–5 rating.

3. **Routing memory (decision support, not automation)** — *lightweight, last.*
   A query over the run log: "for this task type in this project, here's what each model has done and whether it worked." You decide. No scoreboard, no learned model.

If you build only #1 and #2, you have ~70% of the value. #3 is a thin read-only view on top.

---

## 4. Data model (SQLite — single file, zero config)

Use SQLite. It is durable, transactional, requires no server, and is perfect for a single user. Store canonical state as a **git-tracked markdown file inside each repo** (`/.cortex/state.md`) so provenance and history come for free from git; the database indexes and links to it rather than owning it.

```
projects
  id              TEXT PRIMARY KEY
  name            TEXT
  repo_path       TEXT            -- absolute path; state.md lives at repo_path/.cortex/state.md
  stack           TEXT
  status          TEXT            -- active | paused | archived
  current_goal    TEXT
  test_command    TEXT            -- e.g. "pytest -q"  (nullable)
  updated_at      TEXT

tasks
  id              TEXT PRIMARY KEY
  project_id      TEXT REFERENCES projects(id)
  title           TEXT
  type            TEXT            -- code | review | research | docs | data | planning | other
  status          TEXT            -- open | in_progress | done | abandoned
  brief           TEXT            -- the compiled brief / prompt sent to the model
  execution_mode  TEXT            -- agentic_cli | api | manual
  model           TEXT
  created_at      TEXT
  updated_at      TEXT

runs                              -- THE provenance + outcome log; one row per model attempt
  id              TEXT PRIMARY KEY
  task_id         TEXT REFERENCES tasks(id)
  project_id      TEXT REFERENCES projects(id)
  model           TEXT
  execution_mode  TEXT
  started_at      TEXT
  ended_at        TEXT
  git_before      TEXT            -- HEAD sha before (agentic mode)
  git_after       TEXT            -- HEAD sha after
  files_changed   TEXT            -- JSON array
  diff_size       INTEGER         -- lines changed; a cheap risk signal
  tests_passed    INTEGER         -- 1 | 0 | NULL (not run)
  outcome         TEXT            -- survived | reverted | unknown
                                  --   survived = change still present after N days / next commit
                                  --   reverted = git revert / discarded
  response        TEXT            -- API/manual response text (nullable for agentic)
  captured_via    TEXT            -- git | api | manual
  human_note      TEXT            -- optional, never required

decisions
  id              TEXT PRIMARY KEY
  project_id      TEXT REFERENCES projects(id)
  ts              TEXT
  decision        TEXT
  rationale       TEXT
  source          TEXT            -- e.g. "run:<id>", "client meeting 2026-06-10", "manual"
```

**Deliberately omitted:** a `model_scores` table. Model performance is a **VIEW over `runs`**, computed when asked, so it can never go stale and never needs an update step.

```sql
-- model history for decision support (computed, never stored)
CREATE VIEW model_task_history AS
SELECT project_id, type AS task_type, model,
       COUNT(*)                                   AS attempts,
       SUM(tests_passed = 1)                      AS tests_passed,
       SUM(outcome = 'survived')                  AS survived,
       AVG(diff_size)                             AS avg_diff_size,
       MAX(started_at)                            AS last_used
FROM runs JOIN tasks ON runs.task_id = tasks.id
GROUP BY project_id, type, model;
```

---

## 5. The canonical state document (`/.cortex/state.md`)

This is the heart of the system. It is **regenerated, not appended** — kept compact and always-current, so the context compiler always has a clean source and conflicts are resolved rather than accumulated. Living in the repo means git is its version history.

```markdown
# <Project Name> — State
_Last updated: 2026-06-23 by Cortex_

## Goal (now)
<one or two sentences — the current objective>

## Stack
<languages, frameworks, key services>

## Important files
- path — what it does

## Open tasks
- [ ] <task> (type, priority)

## Recent decisions (last ~10, newest first)
- 2026-06-20 — <decision> — <why> — source: <provenance>

## Known risks / assumptions
- <risk or assumption>

## Definition of done (for this project)
- <e.g. tests pass, app boots, no secrets in diff, client-safe data only>
```

A local Ollama model regenerates this on request by reading the previous `state.md`, recent runs, and recent decisions, then producing a fresh compact version. Old versions are recoverable via git. **Never let it grow unbounded** — that defeats its purpose as a context source.

---

## 6. The context compiler (feature #1)

**Input:** project + (optional) the specific task and target model.
**Output:** a tight brief ready to paste or inject.

```
cortex brief <project> [--task <id>] [--model <name>]
```

Composition:
1. Pull `state.md`.
2. If a task is given, prepend the task spec and its type.
3. Tailor format to the target model/mode (terser for API, repo-relative for agentic CLI, full self-contained block for manual paste).
4. Run a **local secret/privacy scan** over anything about to be emitted (regex + Ollama classifier) — block on detected API keys, credentials, client PII. This is cheap, local, and high-value given trading/recruiting data.
5. Emit. For agentic mode, also write it to `/.cortex/last_brief.md` so the CLI agent can be pointed at it.

This is usable on day one with no run history at all.

---

## 7. The run loop (feature #2)

One loop. Not six.

```
compile brief  →  execute (agentic | api | manual)  →  capture outcome  →  update state.md
```

Per mode:

**Agentic CLI**
```
cortex run <task> --mode agentic --model codex
  → record git HEAD (git_before)
  → print/save the brief; you run the agent in the repo
  → on `cortex done <run>`:
      git diff git_before..HEAD  → files_changed, diff_size
      run project.test_command   → tests_passed
      store run; mark outcome=unknown (resolved later)
```

**API**
```
cortex run <task> --mode api --model gemini
  → call the API with the compiled brief
  → store response; if the task type is verifiable, optionally apply a validator
```

**Manual**
```
cortex run <task> --mode manual --model <web-only-model>
  → emit brief to clipboard
  → `cortex capture <run>` opens an editor; you paste the result back (lossy, minimize)
```

**Outcome resolution (automatic, runs on a schedule or on next session):**
A change is `survived` if its lines are still present after the next commit / N days; `reverted` if discarded. This is the implicit evaluation signal — no rating required.

---

## 8. Routing memory (feature #3 — thin, read-only)

```
cortex history <project> --type code
```
Returns the `model_task_history` view as a small table. That is the entire feature. It informs your choice; it does not make it. No learned weights, no auto-assignment. (When/if a single (project × type) cell exceeds ~20 verifiable runs, revisit whether automation is warranted — most cells never will, and that's fine.)

---

## 9. Interfaces

V1 ships as a **CLI** (fastest to build, scriptable, lives where the work is). A minimal read-only web view (single project + global activity log) is optional and can come after the CLI proves useful. Do **not** build seven pages.

Core commands:
```
cortex init <repo_path>            register a project, scaffold .cortex/state.md
cortex brief <project>             compile a model-ready brief
cortex run <task> --mode --model   start a run
cortex done <run> | capture <run>  close a run, capture outcome
cortex history <project>           model decision-support view
cortex state <project> --regen     regenerate state.md from runs + decisions
cortex decide <project> "..."      log a decision with rationale
```

---

## 10. Stack

- **Python 3.11+**, **SQLite** (stdlib), **Click/Typer** for the CLI.
- **git** via subprocess for provenance and diffing.
- **Ollama** (local) for: state regeneration, secret/privacy scan, task-type classification, brief tailoring. Keep local models to narrow, verifiable, privacy-sensitive transforms only.
- **API clients** for Gemini / Perplexity / Grok / Claude as available.
- Optional later: **LiteLLM**-style gateway to normalize the API clients behind one contract; **Langfuse/MLflow** if/when you want run tracing beyond SQLite.

Keep it boring. The plumbing should be unremarkable so the memory layer can be the interesting part.

---

## 11. Build plan (timeboxed)

| Step | Deliverable | Est. |
|---|---|---|
| 0 | `cortex init`, SQLite schema, `.cortex/state.md` scaffold | 0.5 day |
| 1 | **Context compiler** (`cortex brief`) + local secret scan | 1 day |
| 2 | Run loop — **agentic CLI capture** via git + tests | 1 day |
| 3 | Run loop — **API capture** for 1–2 models | 0.5 day |
| 4 | Outcome resolution (survived/reverted) + `state --regen` | 1 day |
| 5 | `cortex history` view; manual mode as fallback | 0.5 day |

Target: roughly two focused weekends. If you can't ship steps 0–2 in the first weekend, the scope is still too big — cut the API mode and ship the context compiler + agentic capture alone.

---

## 12. Kill criteria (read this part)

Build this **only if** all three hold:
1. You work across enough projects that re-establishing context is a real recurring tax.
2. You do enough verifiable (code) work that auto-capture yields a real signal.
3. You will actually run `cortex brief` daily.

After two weeks of daily use, answer honestly: *is this saving me time, or am I maintaining a tool instead of doing the work?* If it isn't earning its keep, archive it and keep a hand-written `state.md` per repo — which, notably, is most of the value anyway. A tool like this is a seductive way to avoid the actual work; the timebox and this kill switch are the guardrails against that.

---

## 13. What changed from the original Version 1, and why

| Original Version 1 | This revision | Reason |
|---|---|---|
| Centerpiece = task router | Centerpiece = memory + context compiler | Routing is the least valuable, least learnable part |
| Default path = manual copy-paste | Default = automatic capture via git/API; manual is fallback | Manual logging decays; auto-capture survives |
| Human 1–5 ratings as signal | Implicit signal: tests pass + change survives | Ratings don't get entered; commits do |
| Stored `model_scores` table | `model_task_history` VIEW over runs | Stored scores rot and need an update step |
| 7 UI pages | One CLI + optional single read-only view | Pages are effort that doesn't create value |
| Routing auto-recommends a model | History informs *your* decision | n is too small to automate honestly |
| Learning/training implied early | Deferred until data proves a pattern exists | State before agents, recursively |
