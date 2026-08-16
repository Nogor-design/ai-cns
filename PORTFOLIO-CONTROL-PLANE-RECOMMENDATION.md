# Portfolio Control Plane Recommendation

Date: 2026-08-12
Decision owner: Codex PM, subject to owner approval for external authorization

## Recommendation

Use a **hybrid control plane**:

1. **Cortex is canonical** for the complete portfolio, PM state, AI attribution, decisions, evidence, and next-task control.
2. **GitHub Issues and one user-level GitHub Project are the engineering mirror** for repositories hosted on GitHub.
3. **Codex is the execution surface**, with Cortex storing the Codex project and task/thread identifiers.
4. Add **Planner-style concepts selectively**: milestones, start and target dates, finish-to-start blocking dependencies, progress, and baseline-versus-actual milestone dates. Do not introduce resource leveling, timesheets, or detailed critical-path scheduling until a real project needs them.

No single existing product covers the required combination. GitHub does not cover local/non-code projects or AI run provenance; Codex task history is not a portfolio plan; Microsoft Planner Premium has stronger scheduling but would duplicate engineering state and does not provide native AI evidence tracking.

## Product responsibilities

| Surface | Owns | Does not own |
| --- | --- | --- |
| Cortex | Portfolio, PM work items, assignments, decisions, AI runs, evidence, next action, cross-system IDs | Pull-request discussion and repository-native collaboration |
| GitHub | Issues, pull requests, code review, repository collaboration, engineering roadmap views | Non-Git projects, complete AI activity, private local artifacts |
| Codex | Interactive execution, goals, local project context, task continuation | Durable cross-project prioritization |
| Planner Premium | Optional schedule analysis for a date-critical program | Canonical engineering or AI work records |

## Required views

### 1. Today / Control room

Show owner decisions, blocked work, work currently running, review waiting, and one ready-next item per active program. This is the default view.

### 2. Portfolio roadmap

Show project and milestone lanes on a common time axis with planned start, target, actual completion, and blocking relationships. Provide month and quarter zoom. This is the useful part of a Gantt chart for this portfolio.

Do not display every prompt or investigation as a bar. Execution detail belongs in the project drill-down and activity timeline.

### 3. Work items

Provide table and board modes over the same records. Support parent/subtask hierarchy, milestone, priority, owner or model, status, risk, start, target, progress, blocked-by, next action, and links to Codex and GitHub.

### 4. Activity and evidence

Provide an append-only timeline grouped by day. Each entry states who or which model did what, for which project and work item, when it happened, the outcome, and linked evidence. Include task transitions, delegations, decisions, runs, commits, pull requests, tests, reviews, and acceptance judgments.

The Evidence Timeline and project filter previously prototyped by Gemini should be reconstructed first, then backed by generic activity events rather than only the most recent run rows.

### 5. Project drill-down

Show the outcome and current bounded slice first, followed by milestones, work-item tree, dependencies, people/models, decisions, evidence, and the full activity timeline. The main action is **Task Codex next**. It creates or resumes a linked Codex task using the tracked scope and done-when check.

### 6. AI team and allocation

Show current assignments, queue, review state, accepted outcomes by task type, cycle time, retry rate, and token/cost data when available. Optimize for accepted outcomes and owner attention, not for keeping every model busy.

## Data model additions

### Project

- `remote_url`, `github_owner`, `github_repo`
- `codex_project_id`
- `phase`, `target_start`, `target_end`

### Work item

- `parent_id`, `milestone_id`
- `start_at`, `target_at`, `completed_at`, `progress`
- `blocked_reason`, `next_action`
- `github_issue_id`, `github_issue_number`, `github_issue_url`, `github_project_item_id`
- `codex_thread_id`, `sync_state`

### Dependency

- `task_id`, `depends_on_task_id`, `type`
- Start with one type: `blocks`. Add more dependency types only if scheduling behavior requires them.

### Activity event

- `project_id`, `task_id`
- `actor_type`, `actor_name`, `model`
- `action`, `summary`, `source`, `source_ref`
- `occurred_at`, `evidence_json`

The event log is append-only. Current tables remain the fast current-state projection; events explain how that state was reached.

## GitHub Projects design

Create one private, user-level Project spanning repositories. Use these fields:

- Status: Backlog, Ready, In progress, Review, Blocked, Done
- Program and Cortex project
- Priority and work type
- Owner or agent
- Start and target dates
- Estimate, risk, and milestone
- Cortex task ID and Codex task URL/ID
- Evidence state

Create these views:

- **Needs me:** owner decisions, blocked items, and reviews
- **Execution board:** Ready through Done
- **Roadmap:** milestone and target-date timeline
- **By repository:** grouped table
- **Done recently:** accepted work with evidence

Synchronization must be field-owned to prevent loops:

- GitHub owns issue title/body, assignees, labels, pull-request state, and human discussion.
- Cortex owns AI assignment, run provenance, acceptance evidence, Codex task ID, and cross-project priority.
- Status, milestone, start, and target fields synchronize under explicit mappings with a stored last-sync marker and conflict state.

Current blocker: the installed GitHub CLI token has repository access but lacks the `read:project` scope. Reading Projects requires owner authorization; writing also requires the `project` scope. Do not request those scopes until the integration spike is ready.

## Codex integration

Store `codex_project_id` on the project and `codex_thread_id` on the work item. The first integration spike should verify that Codex App Server can:

1. list tasks filtered by project working directory;
2. start a task with the Cortex brief and measurable done-when check;
3. resume that task later;
4. make the resulting task visible and openable in the desktop application;
5. return status and completion events that Cortex can record.

Until those behaviors are verified, **Task Codex next** should copy or send a bounded launch prompt and retain a manual link rather than pretending the integration is complete.

## Delivery sequence

### Phase 1 — Trustworthy PM record

- Restore the Gemini Evidence Timeline and project filter.
- Add hierarchy, blocking dependencies, generic activity events, cross-system links, and the mandatory PM update transaction.
- Build Today, Activity, and project drill-down around those records.

Acceptance: five PM sessions across three projects can be reconstructed without reading chat history.

### Phase 2 — GitHub engineering mirror

- Authorize `read:project` and `project` scopes.
- Map repositories, import/link issues, create the cross-repository Project, and implement dry-run sync with conflict reporting.
- Add PR, commit, test, and issue evidence to activity.

Acceptance: a GitHub issue change and a Cortex PM update converge without duplicate items or a sync loop.

### Phase 3 — Start and continue work from Cortex

- Complete the Codex App Server compatibility spike.
- Add **Task Codex next**, **Continue task**, and **Open in Codex** actions.
- Automatically persist task/thread identity and lifecycle events.

Acceptance: an owner can open a project, start its ready-next item, later resume it, and see its accepted evidence in Cortex.

### Phase 4 — Roadmap and selective scheduling

- Add milestone timeline, baseline and actual dates, slippage, and blocked-chain highlighting.
- Add critical-path calculation only for programs with reliable dependencies and date commitments.

Acceptance: the roadmap reveals a real delivery risk earlier than the Today view alone.

## Explicit non-goals for the first release

- Replacing GitHub Issues or pull-request review
- Full Microsoft Project compatibility
- Timesheets, utilization targets, or resource leveling
- Automatic merge or automatic acceptance of agent output
- Maintaining the same information manually in Cortex and GitHub

## Decision

Proceed with Cortex as the canonical portfolio control plane and GitHub Projects as a synchronized engineering mirror. Restore trustworthy attribution and evidence before building the Gantt-style roadmap, because a polished schedule built on incomplete work history would be misleading.
