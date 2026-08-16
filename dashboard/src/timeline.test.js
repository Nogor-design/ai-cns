import assert from 'node:assert/strict'
import test from 'node:test'

import { buildTimelineItems, filterAndGroupRuns, filterAndGroupTimeline } from './timeline.js'

const runs = [
  { id: 'older', project_id: 'alpha', started_at: '2026-08-10T08:00:00-06:00' },
  { id: 'newer', project_id: 'alpha', started_at: '2026-08-11T09:00:00-06:00' },
  { id: 'other', project_id: 'beta', started_at: '2026-08-11T10:00:00-06:00' },
  { id: 'invalid', project_id: 'alpha', started_at: 'not-a-date' },
]

test('groups valid runs by local day in newest-first order', () => {
  const groups = filterAndGroupRuns(runs)
  assert.equal(groups.length, 2)
  assert.deepEqual(groups[0].runs.map(run => run.id), ['other', 'newer'])
  assert.deepEqual(groups[1].runs.map(run => run.id), ['older'])
})

test('filters the timeline to one project before grouping', () => {
  const groups = filterAndGroupRuns(runs, 'alpha')
  assert.deepEqual(groups.flatMap(group => group.runs.map(run => run.id)), ['newer', 'older'])
})

test('combines attributed activity with legacy runs without duplicating recorded runs', () => {
  const activity = [
    { id: 'event-1', project_id: 'alpha', occurred_at: '2026-08-11T11:00:00-06:00', source: 'cli-pm', action: 'pm.session_started' },
    { id: 'event-2', project_id: 'alpha', occurred_at: '2026-08-11T10:00:00-06:00', source: 'cortex-run', source_ref: 'newer', action: 'run.started' },
  ]
  const items = buildTimelineItems(runs, activity)
  assert.equal(items.filter(item => item.run_id === 'newer').length, 1)
  assert.ok(items.some(item => item.id === 'event-1'))
  assert.ok(items.some(item => item.id === 'older' && item.kind === 'run'))
})

test('filters combined activity and run history by project', () => {
  const activity = [
    { id: 'alpha-event', project_id: 'alpha', occurred_at: '2026-08-12T10:00:00-06:00', source: 'cli-pm' },
    { id: 'beta-event', project_id: 'beta', occurred_at: '2026-08-12T11:00:00-06:00', source: 'cli-pm' },
  ]
  const groups = filterAndGroupTimeline(runs, activity, 'alpha')
  assert.ok(groups.flatMap(group => group.events).every(item => item.project_id === 'alpha'))
})
