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
