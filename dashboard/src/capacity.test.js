import test from 'node:test'
import assert from 'node:assert/strict'
import { clampReserve, resetLabel, schedulerState, sortModels, stopSummary, windowLabel, windowTone } from './capacity.js'

test('window tone tracks the background ceiling', () => {
  assert.equal(windowTone(null, 70), 'unknown')
  assert.equal(windowTone(40, 70), 'ok')
  assert.equal(windowTone(62, 70), 'near')
  assert.equal(windowTone(79, 70), 'held')
  assert.equal(windowTone(79, null), 'ok')
})

test('reset labels are relative and tolerate bad input', () => {
  const now = Date.parse('2026-09-17T12:00:00Z')
  assert.equal(resetLabel('2026-09-17T12:30:00Z', now), 'resets in 30m')
  assert.equal(resetLabel('2026-09-17T14:05:00Z', now), 'resets in 2h 5m')
  assert.equal(resetLabel('2026-09-20T12:00:00Z', now), 'resets in 3d')
  assert.equal(resetLabel('2026-09-17T11:00:00Z', now), 'resetting')
  assert.equal(resetLabel('nope', now), '')
  assert.equal(resetLabel(null, now), 'rolling window')
  assert.equal(resetLabel(undefined, now), '')
})

test('reserve is clamped to the server range', () => {
  assert.equal(clampReserve('45'), 45)
  assert.equal(clampReserve(120), 95)
  assert.equal(clampReserve(-3), 0)
  assert.equal(clampReserve('x'), 30)
})

test('models sort usable lanes first and hide namespaced copies', () => {
  const sorted = sortModels([
    { name: 'big', lane: 'avoid', size_gb: 40 },
    { name: 'coder', lane: 'hybrid_moe', size_gb: 17 },
    { name: 'trading-hub/phi4', lane: 'gpu', size_gb: 8 },
    { name: 'phi4', lane: 'gpu', size_gb: 8 },
    { name: 'tiny', lane: 'gpu', size_gb: 1 },
  ])
  assert.deepEqual(sorted.map(model => model.name), ['tiny', 'phi4', 'coder', 'big'])
  assert.equal(windowLabel('seven_day'), 'Weekly')
})

test('scheduler state distinguishes live, paused and stopped leases', () => {
  const now = Date.parse('2026-09-17T12:10:00Z')
  assert.deepEqual(schedulerState(null, now), { label: 'Not started', tone: 'idle' })
  const lease = { heartbeat_at: '2026-09-17T12:00:00Z' }
  assert.equal(schedulerState({ lease, running: true, paused: false }, now).label, 'Running')
  assert.equal(schedulerState({ lease, running: true, paused: true }, now).tone, 'near')
  assert.equal(schedulerState({ lease, running: false }, now).label, 'Stopped 10m ago')
  assert.equal(schedulerState({ lease: { heartbeat_at: 'bad' }, running: false }, now).label, 'Stopped')
})

test('stop summaries pair the reason with the limit that triggered it', () => {
  const limits = { max_tool_calls: 25, stall_seconds: 600, repeat_limit: 5 }
  assert.equal(
    stopSummary({ supervisor: { reason: 'used more than 25 tool calls', tool_calls: 26, limits } }),
    'used more than 25 tool calls · 26 of 25 allowed',
  )
  assert.equal(
    stopSummary({ supervisor: { reason: 'no output for 10 minutes', limits } }),
    'no output for 10 minutes · limit 10 min',
  )
  assert.equal(
    stopSummary({ supervisor: { reason: 'repeated the same action 5 times (pytest)', limits } }),
    'repeated the same action 5 times (pytest) · limit 5 in a row',
  )
  assert.equal(stopSummary({ human_note: 'grok exited with code 1' }), 'grok exited with code 1')
  assert.equal(stopSummary({}), 'Did not finish; see the run log.')
})
