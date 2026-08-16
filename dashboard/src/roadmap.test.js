import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildRoadmap, classifyRoadmapTask, filterRoadmapTasks, parseRoadmapDate,
} from './roadmap.js'

const base = {
  id: 'task', project_id: 'alpha', project_name: 'Alpha', title: 'Task',
  status: 'open', priority: 2, progress: 25, dependencies: [],
}

test('classifies explicit ranges and single dates without inventing duration', () => {
  const range = classifyRoadmapTask({
    ...base, start_at: '2026-08-10', target_at: '2026-08-14',
  })
  const point = classifyRoadmapTask({ ...base, due_at: '2026-08-20' })
  assert.equal(range.classification, 'planned')
  assert.equal(range.marker, false)
  assert.equal(range.start, '2026-08-10')
  assert.equal(range.end, '2026-08-14')
  assert.equal(point.classification, 'planned')
  assert.equal(point.marker, true)
  assert.equal(point.start, '2026-08-20')
  assert.equal(point.end, '2026-08-20')
})

test('terminal work uses recorded lifecycle dates instead of planned dates', () => {
  const row = classifyRoadmapTask({
    ...base,
    status: 'done',
    start_at: '2026-09-01',
    target_at: '2026-09-10',
    created_at: '2026-08-01T15:00:00Z',
    completed_at: '2026-08-03T20:00:00Z',
  })
  assert.equal(row.classification, 'observed')
  assert.equal(row.start, '2026-08-01')
  assert.equal(row.end, '2026-08-03')
})

test('keeps unscheduled and invalid active work off the time axis', () => {
  assert.equal(classifyRoadmapTask(base).classification, 'unscheduled')
  const reversed = classifyRoadmapTask({
    ...base, start_at: '2026-08-20', target_at: '2026-08-10',
  })
  assert.equal(reversed.classification, 'invalid')
  assert.ok(reversed.warnings.includes('Target date is before start date'))
  assert.equal(parseRoadmapDate('2026-02-30'), null)
})

test('active recent and all scopes have deterministic terminal boundaries', () => {
  const tasks = [
    base,
    { ...base, id: 'recent', status: 'done', completed_at: '2026-08-10' },
    { ...base, id: 'old', status: 'done', completed_at: '2026-06-01' },
  ]
  assert.deepEqual(
    filterRoadmapTasks(tasks, { scope: 'active', today: '2026-08-15' }).map(row => row.id),
    ['task'],
  )
  assert.deepEqual(
    filterRoadmapTasks(tasks, { scope: 'recent', today: '2026-08-15' }).map(row => row.id),
    ['task', 'recent'],
  )
  assert.equal(filterRoadmapTasks(tasks, { scope: 'all', today: '2026-08-15' }).length, 3)
})

test('builds a shared axis and preserves missing dependency evidence', () => {
  const roadmap = buildRoadmap([
    {
      ...base,
      id: 'scheduled',
      start_at: '2026-08-10',
      target_at: '2026-08-14',
      dependencies: [{ depends_on_task_id: 'missing', satisfied: false }],
    },
    { ...base, id: 'unscheduled', project_id: 'beta', project_name: 'Beta' },
  ], { scope: 'all', today: '2026-08-15' })
  assert.equal(roadmap.scheduled.length, 1)
  assert.equal(roadmap.unscheduled.length, 1)
  assert.equal(roadmap.scheduled[0].blockingDependencies.length, 1)
  assert.ok(roadmap.scheduled[0].left >= 0)
  assert.ok(roadmap.scheduled[0].width > 0)
  assert.ok(roadmap.ticks.length >= 2)
  assert.equal(roadmap.metrics.blocked, 1)
})
