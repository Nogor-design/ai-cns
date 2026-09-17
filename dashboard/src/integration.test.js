import test from 'node:test'
import assert from 'node:assert/strict'
import {
  branchLabel, canRevert, checkLabel, gateSummary, passedCount, reviewerLabel,
  statusLabel, statusTone,
} from './integration.js'

const REJECTED = {
  status: 'rejected',
  task_branch: 'cortex/demo/t1',
  checks: [
    { name: 'changes', status: 'pass', detail: '2 files committed' },
    { name: 'path_scope', status: 'fail', detail: '1 file outside the task scope' },
    { name: 'protected_files', status: 'pass', detail: 'none touched' },
  ],
}

test('a check is named by what it asked, not by its code name', () => {
  assert.equal(checkLabel('merged_tests'), 'Tests pass on the merged result')
  assert.equal(checkLabel('unknown_check'), 'unknown_check')
})

test('needs-you is a question, not a refusal', () => {
  assert.equal(statusTone('merged'), 'ok')
  assert.equal(statusTone('verified'), 'ready')
  assert.equal(statusTone('needs_owner'), 'ask')
  assert.equal(statusTone('rejected'), 'refused')
  assert.equal(statusLabel('needs_owner'), 'Needs you')
})

test('the summary of a rejection names the check that stopped it', () => {
  assert.equal(
    gateSummary(REJECTED),
    'Stayed inside the task scope: 1 file outside the task scope',
  )
  assert.equal(passedCount(REJECTED), 2)
})

test('a merge summary carries the commit that can be reverted', () => {
  const merged = {
    status: 'merged', integration_branch: 'cortex/integration',
    merge_commit: 'abcdef1234567890', checks: [],
  }
  assert.equal(gateSummary(merged), 'Merged into cortex/integration (abcdef12)')
  assert.equal(canRevert(merged), true)
  assert.equal(canRevert({ ...merged, reverted_at: '2026-09-17T12:00:00Z' }), false)
  assert.equal(canRevert(REJECTED), false)
})

test('a passing change that may not merge says where it is waiting', () => {
  assert.equal(
    gateSummary({ status: 'verified', task_branch: 'cortex/demo/t1', checks: [] }),
    'Every check passed; waiting on cortex/demo/t1',
  )
})

test('the branch line distinguishes not-allowed from not-yet-created', () => {
  assert.equal(
    branchLabel({ merge_allowed: false }),
    'Merges need you (autonomy is not set to integration)',
  )
  assert.equal(
    branchLabel({ merge_allowed: true, exists: false }),
    'No integration branch yet; the first verified change creates it',
  )
  assert.equal(
    branchLabel({ merge_allowed: true, exists: true, ahead: 0, behind: 0, base: 'master' }),
    'Level with master',
  )
  assert.equal(
    branchLabel({ merge_allowed: true, exists: true, ahead: 3, behind: 2, base: 'main' }),
    '3 commits to review · 2 behind main',
  )
})

test('the reviewer line says when review is off', () => {
  assert.equal(reviewerLabel({ review_required: false }), 'Second-model review is off')
  assert.equal(reviewerLabel({ review_required: true, reviewer: 'codex' }), 'Reviewed by codex')
  assert.equal(
    reviewerLabel({ review_required: true }),
    'Reviewed by the best available model',
  )
})
