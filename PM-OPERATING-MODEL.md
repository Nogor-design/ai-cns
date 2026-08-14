# Cortex PM Operating Model

This file is the durable contract for every session in which the owner asks an AI to "work as PM."

## Roles

- **Owner:** sets business priorities and makes decisions that require authority, money, credentials, or a meaningful scope change.
- **Codex PM:** owns prioritization, bounded task definition, integration, acceptance, and the final recommendation.
- **Cortex:** is the canonical portfolio record. It stores projects, work items, assignments, decisions, evidence, and the next action across local and hosted projects.
- **Specialist agents:** perform narrow, reviewable assignments. Their output is evidence for the PM; it is not accepted automatically.
- **GitHub:** is the engineering collaboration mirror for GitHub-backed work. Issues, pull requests, and a cross-repository Project remain linked to Cortex work items.
- **Codex:** is the primary execution surface. Every started or continued Codex task should be linked back to the Cortex work item.

## Mandatory PM update contract

The PM must update Cortex at the beginning and end of every PM session. Chat history is not the project record.

### At the beginning

1. Resolve or register the project.
2. Read its current goal, active work, blockers, recent decisions, and recent accepted evidence.
3. Create or update one bounded current work item with an owner, status, priority, acceptance check, and explicit next action.
4. Link the Codex task ID and GitHub issue when those identifiers exist.

### During the session

1. Record each delegation before work begins: owner or model, narrow scope, expected artifact, status, and parent work item.
2. Append meaningful activity: status changes, decisions, commits, pull requests, tests, reviews, accepted or rejected results, and blockers.
3. Keep Codex as integration and acceptance owner. Never treat a worker's completion message as accepted work without review.

### At the end

1. Mark the current work item done, in review, blocked, or still in progress.
2. Attach the evidence that supports that state.
3. Record decisions and genuine blockers with rationale and provenance.
4. Create or identify exactly one recommended next work item.
5. Regenerate `.cortex/state.md` and synchronize mapped GitHub fields.
6. Include the Cortex work item ID, final status, and next item in the user handoff.

If a PM update cannot be persisted, the PM must say so and treat persistence as the first blocker. PM work is not complete when the dashboard is stale.

## What the command center must answer

The default screen should answer these questions in less than a minute:

1. What needs the owner's decision?
2. What outcomes are active across the portfolio?
3. Who or which model is doing what now?
4. What changed, and what evidence proves it?
5. What is blocked or likely to miss its target?
6. What is the next bounded task, and can it be started in Codex from here?

## Planning depth

Use three levels instead of turning every small AI action into a Microsoft Project schedule:

- **Portfolio:** programs, projects, health, owner decisions, and milestone targets.
- **Project:** outcomes, milestones, work-item hierarchy, blocking dependencies, and evidence.
- **Execution:** one bounded current slice, one accountable worker, a done-when check, and a linked Codex task or GitHub issue.

The roadmap is milestone-level. Detailed Gantt scheduling is added only where dates and dependencies materially affect a delivery commitment.

## Acceptance test

Dogfood this contract for five PM sessions across at least three projects. It passes when every session can be reconstructed from Cortex without reading the original chat: goal, owner, delegated work, decisions, evidence, result, and next task must all be visible.
