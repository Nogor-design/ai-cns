import test from 'node:test'
import assert from 'node:assert/strict'
import { checkedLabel, kindLabel, planSourceLabel, readingList, refreshOutcome, surveyTone } from './survey.js'

const NOW = Date.parse('2026-09-17T12:00:00Z')

test('a reading is described by how long ago it was taken', () => {
  assert.equal(checkedLabel('2026-09-17T11:59:40Z', NOW), 'read just now')
  assert.equal(checkedLabel('2026-09-17T11:20:00Z', NOW), 'read 40m ago')
  assert.equal(checkedLabel('2026-09-17T06:00:00Z', NOW), 'read 6h ago')
  assert.equal(checkedLabel('2026-09-10T12:00:00Z', NOW), 'read 7d ago')
  assert.equal(checkedLabel(null, NOW), 'never read')
  assert.equal(checkedLabel('not a date', NOW), 'never read')
})

test('tone separates never-read, stale, drifted and clean', () => {
  assert.equal(surveyTone(null, NOW), 'unread')
  assert.equal(surveyTone({ checked_at: '2026-09-15T12:00:00Z', drift: [] }, NOW), 'stale')
  assert.equal(surveyTone({ checked_at: '2026-09-17T11:00:00Z', drift: ['x'] }, NOW), 'drifted')
  assert.equal(surveyTone({ checked_at: '2026-09-17T11:00:00Z', drift: [] }, NOW), 'ok')
})

test('the plan source names the file, or says none was found', () => {
  assert.equal(planSourceLabel({ plan_source: 'docs/ROADMAP.md' }), 'docs/ROADMAP.md')
  assert.equal(planSourceLabel({}), 'no plan document found')
  assert.equal(planSourceLabel(null), 'no plan document found')
})

test('the reading list drops documents that yielded nothing', () => {
  const survey = {
    documents: [
      { path: 'README.md', kind: 'readme', summary: 'A thing.' },
      { path: 'EMPTY.md', kind: 'notes', summary: '', open_items: [] },
      { path: 'TODO.md', kind: 'todo', summary: '', open_items: ['ship it'] },
      { path: 'PLAN.md', kind: 'plan', summary: '', status: 'drafting' },
    ],
  }
  assert.deepEqual(readingList(survey).map(doc => doc.path), ['README.md', 'TODO.md', 'PLAN.md'])
  assert.deepEqual(readingList(null), [])
  assert.equal(readingList(survey, 1).length, 1)
})

test('document kinds read as words', () => {
  assert.equal(kindLabel('readme'), 'README')
  assert.equal(kindLabel('state'), 'Cortex state')
  assert.equal(kindLabel('mystery'), 'mystery')
})

test('re-evaluating says plainly whether anything moved', () => {
  assert.equal(refreshOutcome({ changed: false }), 'Re-read the repository: nothing has changed.')
  assert.equal(
    refreshOutcome({ changed: true, survey: { digest: { next_steps: ['a', 'b'] } } }),
    'Re-read the repository: 2 open items in the plan.',
  )
  assert.equal(
    refreshOutcome({ changed: true, survey: { digest: { next_steps: ['a'] } } }),
    'Re-read the repository: 1 open item in the plan.',
  )
  assert.equal(
    refreshOutcome({ changed: true, survey: { digest: {} } }),
    'Re-read the repository: the reading changed.',
  )
  assert.equal(refreshOutcome(null), '')
})
