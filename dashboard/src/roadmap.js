const DAY_MS = 86_400_000
const TERMINAL_STATUSES = new Set(['done', 'abandoned'])

export function parseRoadmapDate(value) {
  if (!value) return null
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/)
  if (!match) return null
  const year = Number(match[1]), month = Number(match[2]), day = Number(match[3])
  const timestamp = Date.UTC(year, month - 1, day)
  const date = new Date(timestamp)
  if (
    date.getUTCFullYear() !== year
    || date.getUTCMonth() !== month - 1
    || date.getUTCDate() !== day
  ) return null
  return timestamp
}

function isoDay(timestamp) {
  return new Date(timestamp).toISOString().slice(0, 10)
}

function dayDiff(start, end) {
  return Math.round((end - start) / DAY_MS)
}

export function classifyRoadmapTask(task) {
  const terminal = TERMINAL_STATUSES.has(task.status)
  const dependencies = task.dependencies || []
  const blockingDependencies = dependencies.filter(dependency => !dependency.satisfied)
  const base = {
    ...task,
    terminal,
    blockingDependencies,
    dependencyCount: dependencies.length,
    warnings: [],
  }

  if (terminal) {
    const created = parseRoadmapDate(task.created_at)
    const completed = parseRoadmapDate(task.completed_at)
    if (created !== null && completed !== null && completed >= created) {
      return {
        ...base,
        classification: 'observed',
        classificationLabel: 'Recorded lifecycle',
        start: isoDay(created),
        end: isoDay(completed),
        startMs: created,
        endMs: completed,
        marker: created === completed,
      }
    }
    return {
      ...base,
      classification: 'history_unscheduled',
      classificationLabel: 'Historical · dates incomplete',
      start: null,
      end: null,
      startMs: null,
      endMs: null,
      marker: false,
    }
  }

  const hasExplicitDate = Boolean(task.start_at || task.target_at || task.due_at)
  const start = parseRoadmapDate(task.start_at)
  const targetSource = task.target_at || task.due_at
  const target = parseRoadmapDate(targetSource)
  if (task.start_at && start === null) base.warnings.push('Start date is invalid')
  if (targetSource && target === null) base.warnings.push('Target date is invalid')

  if (start !== null || target !== null) {
    const first = start ?? target
    const last = target ?? start
    if (last < first) {
      return {
        ...base,
        classification: 'invalid',
        classificationLabel: 'Schedule needs correction',
        start: null,
        end: null,
        startMs: null,
        endMs: null,
        marker: false,
        warnings: [...base.warnings, 'Target date is before start date'],
      }
    }
    return {
      ...base,
      classification: 'planned',
      classificationLabel: start !== null && target !== null ? 'Planned schedule' : 'Planned date',
      start: isoDay(first),
      end: isoDay(last),
      startMs: first,
      endMs: last,
      marker: first === last,
    }
  }

  return {
    ...base,
    classification: hasExplicitDate ? 'invalid' : 'unscheduled',
    classificationLabel: hasExplicitDate ? 'Schedule needs correction' : 'Unscheduled',
    start: null,
    end: null,
    startMs: null,
    endMs: null,
    marker: false,
  }
}

export function filterRoadmapTasks(tasks, {
  scope = 'recent',
  layer = 'tasks',
  projectId = '',
  today = new Date(),
} = {}) {
  const todayMs = parseRoadmapDate(today instanceof Date ? today.toISOString() : today)
  const recentFloor = todayMs - (30 * DAY_MS)
  return tasks.filter(task => {
    if (layer !== 'all' && (task.layer || 'tasks') !== layer) return false
    if (projectId && task.project_id !== projectId) return false
    if (scope === 'all') return true
    if (!TERMINAL_STATUSES.has(task.status)) return true
    if (scope === 'active') return false
    const completed = parseRoadmapDate(task.completed_at)
    return completed !== null && completed >= recentFloor && completed <= todayMs + DAY_MS
  })
}

function rowSort(left, right) {
  return String(left.project_name || '').localeCompare(String(right.project_name || ''))
    || (left.startMs ?? Number.MAX_SAFE_INTEGER) - (right.startMs ?? Number.MAX_SAFE_INTEGER)
    || Number(left.priority || 3) - Number(right.priority || 3)
    || String(left.title || '').localeCompare(String(right.title || ''))
}

function tickInterval(spanDays) {
  if (spanDays <= 35) return 7
  if (spanDays <= 120) return 14
  return 30
}

export function buildRoadmap(tasks, options = {}) {
  const todayInput = options.today || new Date()
  const todayMs = parseRoadmapDate(todayInput instanceof Date ? todayInput.toISOString() : todayInput)
  const filtered = filterRoadmapTasks(tasks, { ...options, today: todayInput })
  const rows = filtered.map(classifyRoadmapTask)
  const scheduled = rows.filter(row => row.startMs !== null && row.endMs !== null).sort(rowSort)
  const unscheduled = rows.filter(row => row.startMs === null || row.endMs === null).sort(rowSort)
  const dated = scheduled.flatMap(row => [row.startMs, row.endMs])
  let domainStart = Math.min(todayMs, ...(dated.length ? dated : [todayMs])) - (2 * DAY_MS)
  let domainEnd = Math.max(todayMs, ...(dated.length ? dated : [todayMs])) + (2 * DAY_MS)
  if (dayDiff(domainStart, domainEnd) < 13) domainEnd = domainStart + (13 * DAY_MS)
  const spanDays = dayDiff(domainStart, domainEnd) + 1

  const positioned = scheduled.map(row => {
    const left = (dayDiff(domainStart, row.startMs) / spanDays) * 100
    const width = ((dayDiff(row.startMs, row.endMs) + 1) / spanDays) * 100
    return { ...row, left, width }
  })
  const interval = tickInterval(spanDays)
  const ticks = []
  for (let offset = 0; offset < spanDays; offset += interval) {
    const timestamp = domainStart + (offset * DAY_MS)
    ticks.push({ date: isoDay(timestamp), left: (offset / spanDays) * 100 })
  }
  const todayLeft = (dayDiff(domainStart, todayMs) / spanDays) * 100

  return {
    scheduled: positioned,
    unscheduled,
    ticks,
    today: isoDay(todayMs),
    todayLeft,
    domainStart: isoDay(domainStart),
    domainEnd: isoDay(domainEnd),
    spanDays,
    metrics: {
      visible: rows.length,
      planned: rows.filter(row => row.classification === 'planned').length,
      observed: rows.filter(row => row.classification === 'observed').length,
      unscheduled: unscheduled.length,
      blocked: rows.filter(row => row.status === 'blocked' || row.blockingDependencies.length).length,
      milestones: new Set(rows.map(row => row.milestone).filter(Boolean)).size,
    },
  }
}

export function roadmapDateLabel(row) {
  if (!row.start || !row.end) return row.classificationLabel
  if (row.start === row.end) return row.start
  return `${row.start} → ${row.end}`
}
