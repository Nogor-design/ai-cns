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
| Milestone | Text | Cortex | Cortex to GitHub |
| Next action / Blocked reason | Text | Cortex | Cortex to GitHub |
| Cortex Sync | Text fingerprint | Cortex | Cortex to GitHub |
| Issue title, body, state, assignees, labels, native milestone | GitHub issue fields | GitHub | Never overwritten by mirror v1 |
| Comments, discussions, pull requests, views, charts | GitHub | GitHub | Read-only to Cortex v1 |

The Project `Status` options are `Todo`, `In progress`, `Review`, `Blocked`, and
`Done`; priorities are `P1` through `P5`; risks are `Auto`, `Low`, `Medium`, and
`High`. This uses 13 Project fields, below GitHub's 50-field Project limit.

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
  "items_complete": true,
  "items": [
    {
      "id": "PVTI_item_node_id",
      "content_id": "I_issue_node_id",
      "fields": {"Cortex ID": "task-id", "Status": "Todo"}
    }
  ]
}
```

`items_complete` is mandatory. The live reader must follow every ProjectV2 item
page and set it to true only when the final page reports no next page. An absent,
false, interrupted, or rate-limited read can never propose `add_project_item`.
The normalizer must represent cleared text/date values as `null`, numbers as JSON
numbers, and dates as `YYYY-MM-DD`; the planner also treats empty optional text
and numeric strings defensively to avoid false update loops.

Plans contain only logical actions (`add_project_item`, `set_project_field`,
`clear_project_field`, `update_cortex_link`, or `update_cortex_sync_state`) and
explicit conflicts. A future adapter must resolve field and single-select option
IDs from the current Project before translating actions to GraphQL.
`update_cortex_sync_state` must run last, after a re-read verifies every remote
value and optimistic precondition. `safe_to_apply` is false whenever any conflict
exists.

## Live one-item acceptance gate

The current GitHub CLI credential has `repo` and `workflow` access but not the
required Project authorization. No live read or mutation is allowed until the
owner completes GitHub's interactive authorization:

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
