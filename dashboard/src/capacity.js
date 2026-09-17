// Pure helpers for the capacity panel, kept separate so they can be unit tested.

export const LANE_LABELS = {
  gpu: 'Fits GPU',
  hybrid_moe: 'GPU + RAM (MoE)',
  hybrid_dense: 'GPU + RAM (slow)',
  cpu_batch: 'Batch only',
  avoid: 'Too large here',
  embedding: 'Embedding',
}

export function windowLabel(name) {
  return { five_hour: '5-hour', seven_day: 'Weekly', thirty_day: 'Monthly', refusal: 'Cooldown' }[name] || String(name || '').replaceAll('_', ' ')
}

// Colour a quota bar by how close it is to the background ceiling.
export function windowTone(usedPercent, ceilingPercent) {
  if (usedPercent == null) return 'unknown'
  if (ceilingPercent == null) return 'ok'
  if (usedPercent >= ceilingPercent) return 'held'
  if (usedPercent >= ceilingPercent - 10) return 'near'
  return 'ok'
}

export function resetLabel(iso, now = Date.now()) {
  if (iso === null) return 'rolling window'
  if (!iso) return ''
  const when = new Date(iso).getTime()
  if (Number.isNaN(when)) return ''
  const minutes = Math.round((when - now) / 60_000)
  if (minutes <= 0) return 'resetting'
  if (minutes < 60) return `resets in ${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `resets in ${hours}h ${minutes % 60}m`
  return `resets in ${Math.round(hours / 24)}d`
}

export function clampReserve(value) {
  const number = Number(value)
  if (!Number.isFinite(number)) return 30
  return Math.max(0, Math.min(95, Math.round(number)))
}

// Unattended-friendly local models first, then by size.
export function sortModels(models) {
  const order = ['gpu', 'hybrid_moe', 'hybrid_dense', 'cpu_batch', 'avoid', 'embedding']
  return [...(models || [])]
    .filter(model => !String(model.name).includes('/'))
    .sort((a, b) => order.indexOf(a.lane) - order.indexOf(b.lane) || a.size_gb - b.size_gb)
}

// A scheduler is only "running" while its lease is live; a lease that stopped
// renewing means the process died or hung, which the owner should see.
export function schedulerState(pilot, now = Date.now()) {
  const lease = pilot?.lease
  if (!lease) return { label: 'Not started', tone: 'idle' }
  if (pilot.running) {
    return pilot.paused
      ? { label: 'Running · paused', tone: 'near' }
      : { label: 'Running', tone: 'ok' }
  }
  const beat = new Date(lease.heartbeat_at).getTime()
  const minutes = Number.isNaN(beat) ? null : Math.round((now - beat) / 60_000)
  return { label: minutes == null ? 'Stopped' : `Stopped ${minutes}m ago`, tone: 'idle' }
}

export const INBOX_KIND_LABELS = {
  run_failed: 'Run failed',
  interrupted: 'Interrupted',
  worker_hold: 'Worker on hold',
  scheduler_error: 'Scheduler error',
}

// One line explaining a stopped run: what the supervisor counted against which
// limit, so the owner can judge whether the limit is set right.
export function stopSummary(stop) {
  const supervisor = stop?.supervisor
  if (!supervisor) return stop?.human_note || 'Did not finish; see the run log.'
  const limits = supervisor.limits || {}
  const parts = [supervisor.reason]
  if (/tool calls/.test(supervisor.reason || '')) parts.push(`${supervisor.tool_calls} of ${limits.max_tool_calls} allowed`)
  else if (/no output/.test(supervisor.reason || '')) parts.push(`limit ${Math.round((limits.stall_seconds || 0) / 60)} min`)
  else if (/repeated/.test(supervisor.reason || '')) parts.push(`limit ${limits.repeat_limit} in a row`)
  return parts.filter(Boolean).join(' · ')
}
