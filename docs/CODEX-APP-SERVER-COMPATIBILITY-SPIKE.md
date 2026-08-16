# Codex App Server compatibility spike

_Verified locally on 2026-08-12 with `codex-cli 0.146.0` and Codex Desktop._

## Outcome

Cortex can safely discover Codex tasks by exact repository path, create an
empty thread, resume a persisted thread, and capture lifecycle notifications.
The App Server protocol does **not** expose a command that makes Codex Desktop
navigate to a thread. Starting a model turn would also be a real external work
action, so the spike deliberately did not submit one.

The right first release is therefore a guarded local bridge:

1. **Task Codex next** shows the complete bounded prompt and asks the owner to
   confirm starting work.
2. On confirmation, the bridge calls `thread/start`, persists the returned
   `thread.id`, then calls `turn/start` and records notifications in
   `activity_events`.
3. **Continue task** calls `thread/resume` by the stored ID before starting a
   new turn.
4. **Open in Codex** is enabled only when the dashboard is hosted inside a
   Codex surface that supplies navigation. The standalone browser dashboard
   instead offers **Copy Codex prompt** and displays the saved task ID.

This avoids promising desktop navigation that the local protocol cannot
perform and prevents duplicate threads by making `codex_thread_id` the
idempotency key.

## Reproducible evidence

The safe probe disables plugins, never submits a prompt, and only starts an
ephemeral thread when explicitly requested:

```powershell
python scripts/codex-app-server-spike.py `
  --cwd D:\ai-cns `
  --resume-thread 019fc2b0-2d57-7b32-9401-f965e5a580bb `
  --ephemeral-start
```

Observed results:

| Behavior | Result | Evidence |
|---|---|---|
| List by cwd | Proven | `thread/list` returned two threads and every `cwd` exactly matched `D:\ai-cns`. |
| Start | Proven safely | `thread/start` returned ephemeral thread `019ff792-f515-75b1-bc15-b545a697ed9f`; no model turn was submitted and nothing was persisted. |
| Resume | Proven | `thread/resume` loaded persisted task `019fc2b0-2d57-7b32-9401-f965e5a580bb` with 14 turns. |
| Desktop visibility | Partially proven | Current task `019ff716-a5f5-7bd1-a407-bfa53781f234` appeared in both exact-cwd App Server results and the Codex Desktop task list. Visibility of a newly persisted bridge-created task remains an acceptance check for implementation. |
| Desktop navigation | Unsupported by App Server | Generated protocol schemas contain task lifecycle methods but no desktop navigation method. Codex-host navigation exists separately and cannot be assumed by a standalone web page. |
| Event capture | Proven | The live stream emitted `thread/started`, `thread/status/changed`, `remoteControl/status/changed`, and MCP startup notifications. |

The Codex Desktop project for this repository was matched by exact path and
stored in Cortex as `8689d302-71e4-49a9-a718-eada7054eabc`.

## Implementation guardrails

- Bind the bridge to loopback only and keep it opt-in.
- Do not accept arbitrary cwd values from the browser; resolve them from the
  registered Cortex project.
- Never start a turn without an explicit owner confirmation and a persisted
  acceptance test.
- Reuse `codex_thread_id`; do not create a second task when one is already
  linked.
- Record `thread/started`, turn completion/failure, model, task ID, and evidence
  in the current `pm_session_id`.
- Treat protocol version changes as a compatibility failure and fall back to
  copy-prompt mode.
