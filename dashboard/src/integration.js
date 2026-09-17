// Pure helpers for the verification gate panel, kept testable and separate.

// What each gate check actually asked, in the owner's words rather than the
// code's. The panel shows these instead of the raw check names so a rejection
// explains itself without anyone reading cortex/verification.py.
export const CHECK_LABELS = {
  changes: 'Produced a change',
  path_scope: 'Stayed inside the task scope',
  protected_files: 'Left protected files alone',
  secrets: 'No secret in the diff',
  task_tests: 'Tests pass on its own branch',
  merge: 'Merges into the integration branch',
  merged_tests: 'Tests pass on the merged result',
  review: 'A second model approved it',
  rollback: 'Integration branch rewound',
}

export const STATUS_LABELS = {
  merged: 'Merged',
  verified: 'Passed, not merged',
  rejected: 'Rejected',
  needs_owner: 'Needs you',
}

export function checkLabel(name) {
  return CHECK_LABELS[name] || String(name || 'check')
}

export function statusLabel(status) {
  return STATUS_LABELS[status] || String(status || 'unknown')
}

// Tone drives colour. "Needs you" is deliberately not an error: the gate did
// its job and is asking a question, which is different from refusing.
export function statusTone(status) {
  if (status === 'merged') return 'ok'
  if (status === 'verified') return 'ready'
  if (status === 'needs_owner') return 'ask'
  return 'refused'
}

// The checks worth putting in front of someone: the ones that stopped the merge.
export function blockingChecks(verification) {
  return (verification?.checks || []).filter(
    check => check.status === 'fail' || check.status === 'owner',
  )
}

export function passedCount(verification) {
  return (verification?.checks || []).filter(check => check.status === 'pass').length
}

// One line for a verification row. Says what happened and why, never just a
// status word.
export function gateSummary(verification) {
  if (!verification) return ''
  const blockers = blockingChecks(verification)
  if (blockers.length) {
    return blockers.map(check => `${checkLabel(check.name)}: ${check.detail}`).join(' · ')
  }
  if (verification.status === 'merged') {
    return `Merged into ${verification.integration_branch || 'the integration branch'}`
      + (verification.merge_commit ? ` (${verification.merge_commit.slice(0, 8)})` : '')
  }
  if (verification.status === 'verified') {
    return `Every check passed; waiting on ${verification.task_branch || 'its own branch'}`
  }
  return verification.summary || ''
}

// Where a project's integration branch stands relative to the branch it was
// cut from. A project that cannot auto-merge is said so plainly rather than
// shown an empty row.
export function branchLabel(project) {
  if (!project) return ''
  if (!project.merge_allowed) return 'Merges need you (autonomy is not set to integration)'
  if (!project.exists) return 'No integration branch yet; the first verified change creates it'
  const ahead = project.ahead ?? 0
  const behind = project.behind ?? 0
  const base = project.base || 'its base branch'
  if (!ahead && !behind) return `Level with ${base}`
  const parts = []
  if (ahead) parts.push(`${ahead} commit${ahead === 1 ? '' : 's'} to review`)
  if (behind) parts.push(`${behind} behind ${base}`)
  return parts.join(' · ')
}

// Only merges can be undone, and only once.
export function canRevert(verification) {
  return Boolean(verification?.merge_commit) && !verification?.reverted_at
}

export function reviewerLabel(payload) {
  if (!payload?.review_required) return 'Second-model review is off'
  return payload.reviewer ? `Reviewed by ${payload.reviewer}` : 'Reviewed by the best available model'
}
