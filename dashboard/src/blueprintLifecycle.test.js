import test from 'node:test'
import assert from 'node:assert/strict'
import {
  blueprintCliFallback,
  blueprintLifecycleMeta,
  validateBlueprintDraft,
} from './blueprintLifecycle.js'

test('blueprint lifecycle distinguishes a saved owner review from an unfinished draft', () => {
  const fingerprint = 'a'.repeat(64)
  const review = blueprintLifecycleMeta({
    status: 'review',
    draft: { preview_fingerprint: fingerprint },
  })

  assert.equal(review.label, 'Ready for owner review')
  assert.equal(review.actionLabel, 'Reopen owner review')
  assert.equal(review.previewFingerprint, fingerprint)
  assert.equal(blueprintLifecycleMeta({ status: 'draft' }).label, 'Drafting')
  assert.equal(blueprintLifecycleMeta(null).label, 'Blueprint needed')
})

test('blueprint draft validation fails visibly for empty or incomplete responses', () => {
  assert.throws(() => validateBlueprintDraft(null), /did not return a blueprint draft/)
  assert.throws(
    () => validateBlueprintDraft({ questions: [], phase_questions: [] }),
    /could not confirm bounded repository discovery/,
  )

  const draft = {
    questions: [], phase_questions: [], discovery: { bounded_characters: 0 },
    discovery_hash: 'bounded-evidence-hash',
  }
  assert.equal(validateBlueprintDraft(draft), draft)
})

test('blueprint CLI fallback preserves a quoted Windows path', () => {
  assert.equal(
    blueprintCliFallback('D:\\Sample Project'),
    '.\\scripts\\cortex-portfolio.ps1 project onboard "D:\\Sample Project" --draft-only',
  )
})
