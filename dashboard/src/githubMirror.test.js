import assert from 'node:assert/strict'
import test from 'node:test'

import { mirrorActionLabel, mirrorStatusMeta } from './githubMirror.js'

test('maps every API mirror state to an explicit operator-facing status', () => {
  assert.deepEqual(mirrorStatusMeta('converged'), {
    label: 'Already in sync', tone: 'ready',
  })
  assert.equal(mirrorStatusMeta('recoverable').tone, 'attention')
  assert.equal(mirrorStatusMeta('conflict').tone, 'danger')
  assert.equal(mirrorStatusMeta('unknown').label, 'Mirror status unavailable')
})

test('turns bounded mirror operations into concise review labels', () => {
  assert.equal(
    mirrorActionLabel({ kind: 'set_project_field', field: 'Status' }),
    'Set Status',
  )
  assert.equal(
    mirrorActionLabel({ kind: 'add_project_item', target_id: 'PVT_target' }),
    'Add linked issue to Project',
  )
  assert.equal(
    mirrorActionLabel({ kind: 'update_cortex_sync_state' }),
    'Record verified Cortex sync state',
  )
})
