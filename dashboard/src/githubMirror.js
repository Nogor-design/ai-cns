const statusMeta = {
  unconfigured: { label: 'Project setup required', tone: 'warning' },
  unlinked: { label: 'Issue link required', tone: 'warning' },
  converged: { label: 'Already in sync', tone: 'ready' },
  actions_required: { label: 'CLI approval available', tone: 'attention' },
  conflict: { label: 'Conflict needs review', tone: 'danger' },
  recoverable: { label: 'Interrupted operation can resume', tone: 'attention' },
}

export function mirrorStatusMeta(status) {
  return statusMeta[status] || { label: 'Mirror status unavailable', tone: 'warning' }
}

export function mirrorActionLabel(action) {
  const target = action.field || action.target_id || 'Project item'
  if (action.kind === 'set_project_field') return `Set ${target}`
  if (action.kind === 'clear_project_field') return `Clear ${target}`
  if (action.kind === 'add_project_item') return 'Add linked issue to Project'
  if (action.kind === 'update_cortex_link') return 'Record verified Project item link'
  if (action.kind === 'update_cortex_sync_state') return 'Record verified Cortex sync state'
  return String(action.kind || 'Mirror action').replaceAll('_', ' ')
}
