import test from 'node:test'
import assert from 'node:assert/strict'

import {
  dependencyCandidates,
  editableDependencyPhases,
  selectedDependencyOrdinals,
  toggleDependencyOrdinal,
} from './phaseDependencies.js'

const phases = [
  { id: 'p1', ordinal: 1, name: 'Foundation', depends_on: [] },
  { id: 'p2', ordinal: 2, name: 'Delivery', depends_on: [{ depends_on_ordinal: 1 }] },
  { id: 'p3', ordinal: 3, name: 'Scale', depends_on: [{ depends_on_ordinal: 2 }, { depends_on_ordinal: 1 }] },
]

test('only phases after the first can author dependency edges', () => {
  assert.deepEqual(editableDependencyPhases(phases).map(phase => phase.id), ['p2', 'p3'])
})

test('dependency candidates are restricted to earlier phase ordinals', () => {
  assert.deepEqual(dependencyCandidates(phases, phases[2]).map(phase => phase.ordinal), [1, 2])
})

test('existing dependency ordinals are normalized and toggled deterministically', () => {
  assert.deepEqual(selectedDependencyOrdinals(phases[2]), [1, 2])
  assert.deepEqual(toggleDependencyOrdinal([1, 2], 1), [2])
  assert.deepEqual(toggleDependencyOrdinal([2], 1), [1, 2])
})
