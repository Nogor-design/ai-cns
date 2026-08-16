# GitHub Projects engineering mirror

## Decision

Cortex remains the canonical portfolio and PM record. GitHub Projects is the
engineering collaboration mirror for issues already linked to Cortex work
items. It is not a second backlog, and the first live release must be opt-in,
dry-run first, and limited to one linked issue.

GitHub Projects is a good mirror because the same items can be shown as a table,
board, or roadmap, and Project insights can chart the fields on those items.
Cortex still owns cross-repository prioritization, PM sessions, actor evidence,
model decisions, privacy policy, and the single recommended next action.

## Identity and no-duplicate rule

- `tasks.github_issue_id` stores the stable GraphQL node ID of an existing issue.
- `tasks.github_issue_url` and `github_issue_number` are display evidence, not
  synchronization keys.
- `tasks.github_project_item_id` stores the stable Project item node ID after the
  Project has been read.
- Partial unique SQLite indexes prevent more than one Cortex task from claiming
  the same non-null issue node ID or Project item node ID.
- The Project custom field `Cortex ID` stores the Cortex task ID.
- Never match by title and never create an issue automatically.
- Before `addProjectV2ItemById`, read all Project items. Reuse the item whose
  content node ID matches `github_issue_id`. More than one match, a conflicting
  `Cortex ID`, or a stale stored item ID stops the sync for owner review.
- Adding an issue and setting its Project fields are separate phases. Re-read the
  Project after adding the item so subsequent mutations use the returned item ID.

## Field ownership

| Field | Type | Owner | Direction |
| --- | --- | --- | --- |
| Cortex ID | Text | Cortex | Cortex to GitHub |
| Cortex Project | Text | Cortex | Cortex to GitHub |
| Status | Single select | Cortex | Cortex to GitHub |
| Priority | Single select | Cortex | Cortex to GitHub |
| Progress | Number | Cortex | Cortex to GitHub |
| Start date / Target date | Date | Cortex | Cortex to GitHub |
| Risk | Single select | Cortex | Cortex to GitHub |
| Worker | Text | Cortex | Cortex to GitHub |
| Cortex Milestone | Text | Cortex | Cortex to GitHub |
| Next action / Blocked reason | Text | Cortex | Cortex to GitHub |
| Cortex Sync | Text fingerprint | Cortex | Cortex to GitHub |
| Issue title, body, state, assignees, labels, native milestone | GitHub issue fields | GitHub | Never overwritten by mirror v1 |
| Comments, discussions, pull requests, views, charts | GitHub | GitHub | Read-only to Cortex v1 |

The Project `Status` options are `Todo`, `In progress`, `Review`, `Blocked`, and
`Done`; priorities are `P1` through `P5`; risks are `Auto`, `Low`, `Medium`, and
`High`. This uses 13 Project fields, below GitHub's 50-field Project limit.

## Operational views and insights

Project `#1` has three saved views. They are deliberately small so the same
item can move from portfolio triage to execution without duplicating work:

| View | Layout | Saved configuration |
| --- | --- | --- |
| Work Queue | Table | Title, Status, Priority, Progress, Worker, Start date, Target date, Risk, Next action, Blocked reason, and Cortex Project |
| Flow | Board | GitHub's Status columns (`Todo`, `In progress`, `Review`, `Blocked`, and `Done`) with Title, Status, Priority, Worker, Risk, Progress, and Next action on each card |
| Timeline | Roadmap | Start date is the start field and Target date is the target field |

The first proof item renders as a one-day roadmap item on 2026-08-15. This is
proof data, not a fabricated future estimate. Future mirrored tasks should use
their real Cortex dates; tasks without dates remain visible in Work Queue and
Flow without implying a schedule.

Insights contains two custom count charts: `Work by status` and
`Work by priority`. GitHub does not offer the custom text `Worker` field as a
chart axis, so worker attribution is intentionally inspected in Work Queue and
on Flow cards. The default Burn up chart remains available. With one approved
proof item these charts are sparse by design and become useful as additional
owner-approved linked issues are mirrored.

The saved view and chart configuration did not create another issue, draft
item, scheduled sync, GitHub Action, or paid model execution.

`Cortex Sync` is a versioned fingerprint of all Cortex-owned values. The same
fingerprint and the exact last-written logical values are kept in
`tasks.sync_state` only after a remote re-read verifies the write. On the next
plan, a changed marker or any difference from those last-written values is
reported as a conflict, even if Cortex has also changed since that sync. Applying
a clean plan and planning again must produce zero actions, which prevents update
loops.

## Dry-run contract

`cortex.github_projects.build_mirror_plan(task, snapshot)` is pure and performs
no network, subprocess, database, or GitHub mutation. The normalized snapshot is:

```json
{
  "project_id": "PVT_project_node_id",
  "field_schema_complete": true,
  "field_schema": {
    "Status": {
      "id": "PVTSSF_status_field",
      "kind": "single_select",
      "options": {"Todo": "option_todo", "Done": "option_done"}
    }
  },
  "items_complete": true,
  "items": [
    {
      "id": "PVTI_item_node_id",
      "content_id": "I_issue_node_id",
      "fields_complete": true,
      "fields": {"Cortex ID": "task-id", "Status": "Todo"}
    }
  ]
}
```

`items_complete` is mandatory. The live reader must follow every ProjectV2 item
page and set it to true only when the final page reports no next page. An absent,
false, interrupted, or rate-limited read can never propose `add_project_item`.
The guarded adapter also requires `field_schema_complete` and
`fields_complete` for the linked item. It follows Project field pages and each
item's field-value pages, binds exact field and select-option node IDs into the
approved operation, and refuses missing, renamed, or type-changed fields before
the first write.
The normalizer must represent cleared text/date values as `null`, numbers as JSON
numbers, and dates as `YYYY-MM-DD`; the planner also treats empty optional text
and numeric strings defensively to avoid false update loops.

Plans contain only logical actions (`add_project_item`, `set_project_field`,
`clear_project_field`, `update_cortex_link`, or `update_cortex_sync_state`) and
explicit conflicts. The guarded adapter resolves field and single-select option
IDs from the current Project before translating actions to GraphQL.
`update_cortex_sync_state` must run last, after a re-read verifies every remote
value and optimistic precondition. `safe_to_apply` is false whenever any conflict
exists.

## Guarded operator adapter

The live adapter is opt-in and one-task-only:

```powershell
.\scripts\cortex-portfolio.ps1 github configure cortex-portfolio-control-plane `
  --owner Nogor-design --number 1
.\scripts\cortex-portfolio.ps1 github inventory cortex-portfolio-control-plane
.\scripts\cortex-portfolio.ps1 github link <task-id> `
  https://github.com/<owner>/<repo>/issues/<number>
.\scripts\cortex-portfolio.ps1 github plan <task-id>
```

Configuration verifies and stores the Project owner, number, and stable node
ID. `github link` resolves one explicit existing issue URL, verifies its
repository against the Cortex project, and stores its stable node ID; it never
creates or edits the issue. Inventory and plan are read-only and create no
operation or task write. A
strict plan fingerprint covers the target Project, issue, item, complete
snapshot digest, ordered actions, conflicts, and mirror version; it is separate
from `Cortex Sync`, which fingerprints only desired field values.

Apply requires the exact fingerprint and deterministic operation ID printed by
the fresh plan:

```powershell
.\scripts\cortex-portfolio.ps1 github apply <task-id> `
  --approve <plan-fingerprint> `
  --operation-id <operation-id>
```

Before the first remote mutation, Cortex durably reserves the operation and
refuses concurrent or replayed plans for that task. Each mutation re-reads the
complete Project, verifies its bound field/option IDs and expected value, writes
one field, and re-reads the result. `Cortex Sync` is written remotely last. Only
after all desired fields verify does one SQLite transaction persist the stable
Project-item link, local sync state, completed operation, and attributed
activity.

If a request is interrupted after GitHub accepted it, Cortex leaves the local
sync evidence untouched and records the operation as `interrupted` with its
completed-action evidence. Re-running the same approved command re-reads each
action: an already-desired value is verified without a duplicate mutation, an
expected old value is safely continued, and any third value is refused. Use
`github operation <operation-id>` to inspect recovery evidence. A verified
replay performs zero writes.

The dashboard cannot edit `github_issue_id`, `github_issue_number`,
`github_project_item_id`, or `sync_state`; those columns are adapter-owned
verification evidence. No daemon, webhook, scheduled job, bulk mode, issue
creation, title matching, or GitHub-owned issue-field mutation exists.

## Live one-item acceptance gate

The GitHub CLI credential has the required `project` scope. The initial
read-only inventory on 2026-08-15 found one private, open Project (`#1`) with
GitHub's 13 default fields and zero items, while `Nogor-design/ai-cns` had
Issues enabled and zero issues. The approved proof changed that exact empty
baseline as recorded below.

The owner approved this exact bounded preflight, and Codex executed it on
2026-08-15:

1. Rename private user Project `#1` from its generated untitled name to
   `Cortex Engineering Mirror`, add the Cortex-source-of-truth description and
   README, and link `Nogor-design/ai-cns`.
2. Preserve the existing `Todo`, `In Progress`, and `Done` Status option IDs,
   normalize the display name to `In progress`, and add `Review` and `Blocked`.
3. Add the twelve missing fields from the ownership table: seven text fields,
   two single-select fields, one number field, and two date fields. This leaves
   the Project at 25 total fields, below GitHub's 50-field limit.
   The Cortex-owned text field is named `Cortex Milestone` because GitHub
   reserves `Milestone` for the native issue field, which the mirror never
   overwrites.
4. Create exactly one repository issue titled
   `Spike GitHub Projects engineering mirror`, with Cortex task
   `8cac6170cc12`, its acceptance evidence, and the source-of-truth boundary in
   the issue body. Do not create any other issues or draft items.
5. Add only that issue to Project `#1`, re-read the Project, and display the
   stable issue and Project item node IDs before writing item fields.
6. Apply the planner's Cortex-owned values with optimistic expected values,
   verify them by re-read, persist the link and sync fingerprint locally, and
   require the next plan to contain zero actions and zero conflicts.
7. Change and restore one GitHub-owned issue attribute to prove the mirror does
   not overwrite it, then run the duplicate/stale-link refusal probes without
   remote writes.

### Live proof result

- Private Project `#1` is now `Cortex Engineering Mirror`, linked to
  `Nogor-design/ai-cns`, with exactly one item: public issue
  [`#2`](https://github.com/Nogor-design/ai-cns/issues/2).
- Stable identity is recorded as issue node `I_kwDOTC7hi88AAAABM6qslA` and
  Project item node `PVTI_lAHODBDAWM4BNbW0zg2r2N4`; the final verified sync
  marker is `v1:4ca5a8f04b73374b` after the proof item's verified one-day date
  span was added for the Timeline acceptance check.
- GitHub reserves the native field name `Milestone`, so the live proof and
  planner use `Cortex Milestone` for the Cortex-owned text value. GitHub's
  native issue milestone remains GitHub-owned.
- GitHub's default `Auto-close issue`, `Item closed`, `Pull request linked to
  issue`, and `Pull request merged` workflows were disabled because they crossed
  the declared Status/issue-state ownership boundary. `Item added to project`
  and `Auto-add sub-issues to project` remain enabled.
- A reversible issue-title change produced zero mirror actions and was restored;
  the issue remains open while the Project item remains Done.
- Duplicate and stale-link fixtures produced their named conflicts and zero
  writes. The final complete re-read produced zero actions and zero conflicts.

If authorization is ever missing on another machine, restore it interactively:

```powershell
gh auth refresh -s project
```

After authorization, the first live proof remains bounded to one disposable or
explicitly approved linked issue:

1. Read the user-level Project, field configuration, option IDs, and all items.
2. Normalize that read into the snapshot above and print the dry-run plan.
3. Require explicit owner approval of the exact issue, Project, and actions.
4. Apply one phase with optimistic preconditions and a unique operation ID.
5. Re-read the Project and persist stable link IDs plus attributed activity.
6. Run the planner again; acceptance requires zero actions and zero conflicts.
7. Change one GitHub-owned issue field and confirm the mirror does not overwrite
   it. Create a duplicate fixture and confirm the live adapter refuses to write.

Do not enable scheduled sync, automatic issue creation, bidirectional status,
bulk import, or GitHub Actions automation until the one-item proof passes and the
owner accepts the audit evidence.
