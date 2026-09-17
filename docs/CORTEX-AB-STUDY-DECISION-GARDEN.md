# Cortex A/B Study: Decision Garden

Date: 2026-08-19  
Baseline: `D:\Decision-Garden-Baseline`  
Cortex-managed arm: `D:\Decision-Garden-Cortex`

## Executive finding

Cortex did not materially improve the visual polish or core functionality of this small one-session build. Both apps are complete, responsive, local-first decision boards built from the same brief. Cortex's clear advantage was operational: it created durable intent, privacy and worker boundaries, an attributed PM session, a bounded work item, acceptance criteria, a resumable blueprint review, and a copyable handoff prompt that a later session could reconstruct.

For a tiny disposable project, that control-plane overhead is not yet worth the extra interaction cost. For multi-session, multi-agent, high-risk, or owner-gated work, the traceability is valuable enough to justify the setup—provided the onboarding and refresh defects observed here are fixed.

## Use case

Decision Garden is a dependency-free, offline web app for a small team that needs to make pending decisions visible. A user can add a decision with context, owner, due date, and options; filter open, due-soon, and decided items; choose or reopen an option; persist data locally; reset demo data; and export JSON.

This sample was chosen because it is:

- small enough to complete twice in one controlled study;
- rich enough to exercise product intent, non-goals, state, validation, accessibility, testing, responsive layout, and handoff quality;
- free of credentials, deployment differences, package-install variance, or external services.

## Experiment design

The two `PROJECT_BRIEF.md` files are byte-for-byte identical. Both had SHA-256:

`2CD0BA533617EEF82C05851319BFC6B846261428EDA142F1643AEFAA62D2F936`

The baseline arm was built and verified first without registering it in Cortex. The Cortex arm was then registered through the local dashboard with an internal privacy setting and a Codex-only worker allowlist. Its guided product-intent interview was completed through the CLI fallback, its exact blueprint preview was saved in review, a PM work item was started, and the dashboard-generated bounded Codex prompt was copied and used as the implementation handoff.

The blueprint was deliberately not approved. Approval is an owner gate, and the study request did not constitute approval of the exact generated Markdown and fingerprint. No `.cortex/blueprint.md` was written. The saved preview fingerprint is:

`4cbf43663a1bb024bff1629d7b2d299205b25cff284ac90f10c8005abf11106f7`

## Results

| Measure | Baseline | Cortex-managed |
|---|---:|---:|
| Non-Cortex project files | 8 | 8 |
| Counted source/document lines | 523 | 538 |
| Automated tests | 6 passed | 7 passed |
| Initial seeded decisions | 3 | 3 |
| Desktop console warnings/errors | 0 | 0 |
| 390px horizontal overflow | none (`375/375`) | none (`375/375`) |
| Add + reload persistence | passed | passed |
| All/open/due-soon/decided filtering | passed | passed |
| Choose + reopen | passed | passed |
| Reset to three samples | passed | passed |
| JSON export | handler ran; browser download event not surfaced | handler ran, payload unit-tested, and UI announced `Exported 4 decisions.` |
| Durable PM/project record | none | project, draft, fingerprint, work item, PM session, activity, state, prompt |

The implementations stayed similarly sized and delivered comparable UX quality. The Cortex arm's extra automated export-payload test and explicit live-region export confirmation made one acceptance area more observable. That is a useful improvement, but this single sequential run cannot establish that Cortex caused it.

## Experience without Cortex

The baseline path was direct: read the brief, implement, run `node --test`, serve the app, and exercise it in the browser. The main implementation issue was technical rather than managerial: Python's static server served `.mjs` as `text/plain`, so browser modules did not execute. Renaming modules to `.js` and marking the package as ESM fixed it. After that, the app passed the required workflows and responsive check.

The weakness is what remains after the session. The repository explains how to run the app and its source shows what was built, but it does not preserve why the scope was chosen, which worker was permitted, what was explicitly excluded, which acceptance evidence was observed, what remained unproven, or what should happen next. A new PM or agent would need to reconstruct that from files or chat.

## Experience with Cortex

### What worked

- Dashboard registration clearly previewed the local folder before mutation.
- Privacy, allowed workers, goal, stack, test command, priority, state-file creation, and blueprint intent were explicit.
- A Codex-only allowlist was visible in the project drawer.
- The guided interview captured users/problem, desired outcome, non-goals, constraints, the open decision, current phase, outcome, and exit criteria.
- The exact preview was restart-safe and fingerprinted without provider contact or automatic task creation.
- The PM work item persisted a project ID, repository, PM session, outcome, done-when check, allowed paths, risk, next action, and test command.
- The dashboard's Copy Codex prompt was genuinely copy-only and the clipboard matched the visible bounded prompt.
- `.cortex/state.md` made the goal and open work locally visible.

### Friction and defects observed

1. After **Register & build blueprint**, the blueprint modal displayed only the three-step rail. It showed no loading state, questions, error, or recovery action. Closing and reopening produced the same blank shell. The CLI interview was required to proceed.
2. The project drawer initially showed no active work after the PM task was created. An explicit refresh plus closing and reopening the drawer was required before both work items appeared.
3. The drawer continued to label the project **Blueprint needed** even though an exact saved preview existed in review. It did not communicate **Awaiting owner approval** or provide the saved fingerprint/preview from the project summary.
4. The copy-prompt handoff remained available while the blueprint draft was unapproved. The generated prompt did not automatically warn about that unresolved gate; the PM had to add the warning to the task's next action manually.
5. The automatically created PM task began with a generic done-when check and no allowed-path restriction. The PM had to update the acceptance field and allowed paths before copying a sufficiently bounded prompt.
6. PowerShell expanded a wildcard passed to `--allowed-paths`, causing an attempted `task add` command to fail with an unexpected file argument. JSON-form allowed paths worked, but the CLI ergonomics are not Windows-safe enough for the documented shell.
7. Dashboard navigation state was easy to lose: selecting a project from Timeline did not reopen the project drawer as expected, and returning to Today was needed to recover the task controls.

## Cortex advantages

### 1. Reconstructable project intent

The saved interview answers explain the intended user, desired evidence, non-goals, constraints, and open decision. This is far more useful for a later session than a generic README or chat-only prompt.

### 2. Better scope and safety boundaries

The Cortex arm made the local repository, privacy classification, Codex-only allowlist, risk, allowed paths, test command, and prohibition on approval/deployment explicit. The baseline relied on the active agent remembering those boundaries.

### 3. Durable accountability

The Cortex record distinguishes the owner-gated blueprint work from the Codex implementation work, links the PM session and task, and records who did what. That is the strongest benefit for real portfolio work.

### 4. Higher-quality handoff

The copied prompt is immediately usable by another Codex session because it includes stable project and task identities, the current goal, acceptance check, repository, and next action. The baseline has no equivalent continuation object.

### 5. Evidence-oriented acceptance

The Cortex workflow naturally pushed the implementation toward observable proof. In this sample, that showed up in an export-payload unit test and a UI announcement that let browser QA verify the handler ran.

## What should improve, ranked

### P0 — Make guided onboarding reliable and fail visibly

The blank blueprint modal is the largest adoption blocker. The UI should never render an inert rail. It should show the loaded questions, a bounded loading state, or a concrete error with Retry and CLI fallback commands. Add an end-to-end test for register → blueprint questions using a new non-Git local folder.

Done when: a newly registered folder reaches actionable product-intent questions in the dashboard, and simulated API failure shows a recoverable error rather than a blank modal.

### P1 — Surface the real blueprint lifecycle state

Replace **Blueprint needed** with **Drafting**, **Ready for owner review**, **Approved**, or **Drifted** based on the actual draft. Put the exact saved fingerprint and Reopen review action in the project drawer.

Done when: a CLI-created review draft immediately appears as **Ready for owner review** in the dashboard without rediscovery.

### P1 — Enforce or explicitly relax the approval gate

If guided projects must not execute before approval, Copy Codex prompt and dispatch controls should be disabled or should require an explicit owner-approved exception. If manual implementation is intentionally allowed, the prompt must carry the unapproved state and explain what actions remain prohibited.

Done when: tests prove a review-stage blueprint cannot silently produce an execution-ready handoff that omits the gate.

### P1 — Make task creation PM-complete on the first pass

Starting a PM session should support the task brief/type/risk/complexity/allowed paths and full acceptance criteria in one operation, or the dashboard should provide those fields before generating a prompt. A generic auto-created task should not look ready when it is still under-specified.

Done when: the first copied prompt contains the material scope, non-goals, acceptance checks, allowed paths, and gate state without a CLI repair step.

### P2 — Improve refresh and navigation consistency

Mutations should update the selected project drawer immediately. Project selection should behave consistently from Today, Timeline, Roadmap, and All Projects.

Done when: creating or updating a work item updates Active work in place, and selecting the same project from every portfolio view opens the same drawer.

### P2 — Harden PowerShell argument ergonomics

Document and accept Windows-friendly allowed-path syntax that does not rely on callers knowing when PowerShell expands wildcards. Prefer repeatable `--allowed-path` options or a file-based JSON input.

Done when: the documented PowerShell command can add `**` without shell expansion or quoting surprises.

### P2 — Add evidence fields to the state file

The generated state currently preserves the goal and tasks but leaves Important files, risks, and evidence generic. PM close should populate verified commands, browser checks, and key artifact paths automatically.

Done when: a later agent can reconstruct the accepted build and its evidence from `.cortex/state.md` without opening the original chat.

## Recommendation

Use Cortex by default for work expected to span sessions, involve multiple agents, carry an owner approval, or require portfolio visibility. Keep a quick path for one-session prototypes. The next product slice should fix the blank blueprint modal and lifecycle-state visibility together; without that, Cortex's strongest design feature—the approval-stable blueprint—creates more friction than confidence at the exact moment a new user is evaluating it.

## Study limitations

- One model built both arms sequentially, so the Cortex arm benefited from lessons learned during the baseline build.
- This is one small project, not a statistically meaningful benchmark.
- The baseline was intentionally not given an equivalent manual PM artifact set; the study compares the normal direct-build path with the normal Cortex-managed path.
- The in-app browser did not surface blob downloads as download events. The baseline export handler was exercised without console errors; the Cortex export additionally had a unit-tested payload and visible completion announcement.

## Evidence identifiers

- Cortex comparison work item: `2247690a6434`
- Cortex comparison PM session: `1925ae00154f`
- Decision Garden implementation work item: `019d512804e5`
- Decision Garden PM session: `7d6d44d3c50e`
- Saved blueprint fingerprint: `4cbf43663a1bb024bff1629d7b2d299205b25cff284ac90f10c8005abf11106f7`
