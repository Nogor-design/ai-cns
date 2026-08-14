function validDate(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function localDayKey(date) {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function buildTimelineItems(runs = [], activity = []) {
  const recordedRunIds = new Set(
    activity
      .filter(event => event.source === 'cortex-run' && event.source_ref)
      .map(event => event.source_ref),
  )
  const activityItems = activity.map(event => ({
    ...event,
    kind: 'activity',
    timestamp: event.occurred_at,
    run_id: event.source === 'cortex-run' ? event.source_ref : null,
  }))
  const legacyRuns = runs
    .filter(run => !recordedRunIds.has(run.id))
    .map(run => ({ ...run, kind: 'run', timestamp: run.started_at, run_id: run.id }))
  return [...activityItems, ...legacyRuns]
}

export function filterAndGroupTimeline(runs, activity = [], projectId = null) {
  const groups = new Map()
  const items = buildTimelineItems(runs, activity)
  const filtered = projectId
    ? items.filter(item => item.project_id === projectId)
    : items

  filtered
    .map(item => ({ item, date: validDate(item.timestamp) }))
    .filter(item => item.date)
    .sort((left, right) => right.date.getTime() - left.date.getTime())
    .forEach(({ item, date }) => {
      const key = localDayKey(date)
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          label: date.toLocaleDateString(undefined, {
            weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
          }),
          events: [],
        })
      }
      groups.get(key).events.push(item)
    })

  return [...groups.values()]
}

export function filterAndGroupRuns(runs, projectId = null) {
  return filterAndGroupTimeline(runs, [], projectId).map(group => ({
    ...group,
    runs: group.events,
  }))
}
