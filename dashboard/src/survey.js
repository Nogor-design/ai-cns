// Pure helpers for the project survey panel, kept separate so they can be unit tested.

// How long a reading stays believable before it should be taken again. A repo
// can change under Cortex at any moment, so an old reading is a claim about the
// past, not the present, and the panel says so.
export const STALE_AFTER_HOURS = 24

export function checkedLabel(iso, now = Date.now()) {
  if (!iso) return 'never read'
  const when = new Date(iso).getTime()
  if (Number.isNaN(when)) return 'never read'
  const minutes = Math.round((now - when) / 60_000)
  if (minutes < 1) return 'read just now'
  if (minutes < 60) return `read ${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `read ${hours}h ago`
  return `read ${Math.round(hours / 24)}d ago`
}

export function surveyTone(survey, now = Date.now()) {
  if (!survey) return 'unread'
  const when = new Date(survey.checked_at || survey.created_at).getTime()
  if (Number.isNaN(when)) return 'unread'
  if (now - when > STALE_AFTER_HOURS * 3_600_000) return 'stale'
  return (survey.drift || []).length ? 'drifted' : 'ok'
}

// One line naming the document the plan came from, without the directory noise.
export function planSourceLabel(survey) {
  const source = survey?.plan_source
  if (!source) return 'no plan document found'
  return source
}

const KIND_LABELS = {
  readme: 'README',
  plan: 'Plan',
  roadmap: 'Roadmap',
  blueprint: 'Blueprint',
  state: 'Cortex state',
  todo: 'To do',
  changelog: 'Changelog',
  notes: 'Notes',
}

export function openCount(doc) {
  return doc?.open_count ?? (doc?.open_items || []).length
}

export function kindLabel(kind) {
  return KIND_LABELS[kind] || String(kind || 'document')
}

// The documents worth listing: the ones that actually carried something.
// The portfolio payload sends `open_count`; the full digest sends `open_items`.
export function readingList(survey, limit = 6) {
  const documents = survey?.documents || []
  return documents
    .filter(doc => doc.summary || (doc.open_items || []).length || doc.open_count || doc.status)
    .slice(0, limit)
}

// What a re-evaluation actually produced, phrased for someone who pressed a
// button and wants to know whether it was worth it.
export function refreshOutcome(result) {
  if (!result) return ''
  if (result.changed === false) return 'Re-read the repository: nothing has changed.'
  const count = (result.survey?.digest?.next_steps || []).length
  if (!count) return 'Re-read the repository: the reading changed.'
  return `Re-read the repository: ${count} open item${count === 1 ? '' : 's'} in the plan.`
}
