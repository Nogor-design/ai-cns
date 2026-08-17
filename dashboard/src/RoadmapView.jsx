import { useMemo, useState } from 'react'
import {
  AlertTriangle, CalendarClock, Check, CircleAlert, Clock3, Diamond,
  Link2, Save, X,
} from 'lucide-react'

import { buildRoadmap, classifyRoadmapTask, roadmapDateLabel } from './roadmap.js'

const PROJECT_PALETTE = ['#1b7468', '#8057a8', '#b55d42', '#4270a6', '#a67b2f', '#596b7c']

function projectColor(projectId = '') {
  const index = [...projectId].reduce((sum, char) => sum + char.charCodeAt(0), 0)
  return PROJECT_PALETTE[index % PROJECT_PALETTE.length]
}

function pretty(value) {
  return String(value || '').replaceAll('_', ' ')
}

function dateInput(value) {
  return value ? String(value).slice(0, 10) : ''
}

function TaskMeta({ task }) {
  const blocked = task.status === 'blocked' || task.blockingDependencies.length > 0
  return <span className="roadmap-task-meta">
    <em className={`roadmap-kind ${task.classification}`}>{task.classificationLabel}</em>
    <em>{task.layer === 'phases' ? 'delivery phase' : 'task'}</em>
    <em>{task.assignee || 'unassigned'}</em>
    {task.milestone && <em><Diamond size={9} />{task.milestone}</em>}
    {blocked && <em className="blocked"><Link2 size={9} />{task.blockingDependencies.length || 'work'} blocked</em>}
  </span>
}

function RoadmapTaskModal({ task, onClose, onSave }) {
  const isPhase = task.layer === 'phases'
  const [form, setForm] = useState({
    start_at: dateInput(task.start_at),
    target_at: dateInput(task.target_at),
    due_at: dateInput(task.due_at),
    milestone: task.milestone || '',
    progress: Number(task.progress || 0),
  })
  const [saving, setSaving] = useState(false)
  const blockedBy = task.dependencies || []

  async function submit(event) {
    event.preventDefault()
    if (isPhase) return
    setSaving(true)
    const saved = await onSave(task.id, {
      start_at: form.start_at || null,
      target_at: form.target_at || null,
      due_at: form.due_at || null,
      milestone: form.milestone.trim() || null,
      progress: Number(form.progress),
    })
    setSaving(false)
    if (saved) onClose()
  }

  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><form className="roadmap-task-modal" onSubmit={submit} aria-labelledby="roadmap-task-title">
    <div className="modal-head"><div><span className="eyebrow">{isPhase ? 'Delivery phase' : 'Local schedule record'}</span><h2 id="roadmap-task-title">{task.title}</h2><p>{task.project_name} · {task.id}</p></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div>
    <div className="roadmap-modal-summary"><span><small>Status</small><strong>{pretty(task.status)}</strong></span><span><small>Worker</small><strong>{task.assignee || 'Unassigned'}</strong></span><span><small>Roadmap meaning</small><strong>{task.classificationLabel}</strong></span></div>
    {!isPhase ? <div className="roadmap-schedule-form"><label>Start date<input type="date" value={form.start_at} onChange={event => setForm({ ...form, start_at: event.target.value })} /></label><label>Target date<input type="date" value={form.target_at} onChange={event => setForm({ ...form, target_at: event.target.value })} /></label><label>Due date<input type="date" value={form.due_at} onChange={event => setForm({ ...form, due_at: event.target.value })} /></label><label>Milestone<input value={form.milestone} onChange={event => setForm({ ...form, milestone: event.target.value })} placeholder="Named outcome" /></label><label className="roadmap-progress-field">Progress <span>{form.progress}%</span><input type="range" min="0" max="100" step="5" value={form.progress} disabled={task.status === 'done'} onChange={event => setForm({ ...form, progress: event.target.value })} /></label></div> : <div className="roadmap-schedule-form"><p>Delivery phases are read-only on the roadmap view. Use the Project blueprint panel to manage phase quality and progression.</p></div>}
    <div className="roadmap-detail-grid"><section><small>Next action</small><p>{task.next_action || 'No next action recorded.'}</p></section><section><small>Blocked reason</small><p>{task.blocked_reason || 'No blocker recorded.'}</p></section><section className="roadmap-dependency-detail"><small>Finish-to-start dependencies</small>{blockedBy.length ? <ul>{blockedBy.map(dependency => <li key={dependency.depends_on_task_id}><span>{dependency.depends_on_title || dependency.depends_on_task_id}</span><em>{pretty(dependency.depends_on_status || 'not visible')}{dependency.satisfied ? ' · satisfied' : ' · blocking'}</em></li>)}</ul> : <p>No upstream dependencies.</p>}</section></div>
    <div className="roadmap-edit-note"><AlertTriangle size={14} /><span>{isPhase ? 'Phase rows are read-only schedule evidence and cannot be updated from this modal.' : 'These edits update local Cortex schedule evidence only. They do not start AI work, change GitHub, or claim a critical path.'}</span></div>
    <div className="modal-actions"><button type="button" onClick={onClose}>Close</button>{!isPhase && <button type="submit" className="primary" disabled={saving}><Save size={14} />{saving ? 'Saving…' : 'Save local schedule'}</button>}</div>
  </form></div>
}

function RoadmapMobileCard({ task, onOpen }) {
  return <button type="button" className="roadmap-mobile-card" onClick={() => onOpen(task)} style={{ '--project-color': projectColor(task.project_id) }}>
    <span><i />{task.project_name}<em>{pretty(task.status)}</em></span>
    <strong>{task.title}</strong>
    <TaskMeta task={task} />
    <small><Clock3 size={12} />{roadmapDateLabel(task)}</small>
  </button>
}

export default function RoadmapView({ tasks, projects, projectId = '', onProjectChange, onSaveTask }) {
  const [scope, setScope] = useState('recent')
  const [layer, setLayer] = useState('tasks')
  const [selectedTask, setSelectedTask] = useState(null)
  const roadmap = useMemo(
    () => buildRoadmap(tasks, { scope, projectId, layer }),
    [tasks, scope, layer, projectId],
  )
  const mobileRows = [...roadmap.scheduled, ...roadmap.unscheduled]

  return <section className="roadmap-view">
    <div className="roadmap-heading"><div><span className="eyebrow">Portfolio schedule</span><h1>Roadmap</h1><p>Planned dates, recorded delivery history, and unscheduled work stay visibly distinct.</p></div><div className="roadmap-filters"><label>Project<select value={projectId} onChange={event => onProjectChange(event.target.value)}><option value="">All non-archived projects</option>{projects.filter(project => project.status !== 'archived').map(project => <option key={project.project_id} value={project.project_id}>{project.name}{project.status === 'paused' ? ' · paused' : ''}</option>)}</select></label><label>Scope<select value={scope} onChange={event => setScope(event.target.value)}><option value="recent">Active + last 30 days</option><option value="active">Active work only</option><option value="all">All recorded work</option></select></label><label>Layer<select value={layer} onChange={event => setLayer(event.target.value)}><option value="tasks">Tasks</option><option value="phases">Delivery phases</option><option value="all">All</option></select></label></div></div>
    <div className="roadmap-metrics"><span><strong>{roadmap.metrics.visible}</strong><small>visible tasks</small></span><span><strong>{roadmap.metrics.planned}</strong><small>planned</small></span><span><strong>{roadmap.metrics.observed}</strong><small>recorded history</small></span><span><strong>{roadmap.metrics.unscheduled}</strong><small>off-axis</small></span><span><strong>{roadmap.metrics.blocked}</strong><small>blocked</small></span><span><strong>{roadmap.metrics.milestones}</strong><small>milestones</small></span></div>
    <div className="roadmap-legend"><span><i className="planned" />Planned schedule</span><span><i className="observed" />Recorded lifecycle</span><span><i className="today" />Today</span><em>Only tasks with recorded dates appear on the time axis. Click any task for exact evidence and local schedule editing.</em></div>

    <div className="roadmap-board">
      <div className="roadmap-axis-label"><strong>Work item</strong><small>{roadmap.domainStart} → {roadmap.domainEnd}</small></div>
      <div className="roadmap-axis"><span className="roadmap-today-line" style={{ '--left': `${roadmap.todayLeft}%` }}><em>Today</em></span>{roadmap.ticks.map(tick => <span className="roadmap-tick" key={tick.date} style={{ '--left': `${tick.left}%` }}><em>{tick.date.slice(5)}</em></span>)}</div>
      {roadmap.scheduled.length ? roadmap.scheduled.map(task => <div className="roadmap-row" key={task.id}>
        <button type="button" className="roadmap-row-label" onClick={() => setSelectedTask(task)}><span><i style={{ background: projectColor(task.project_id) }} />{task.project_name}</span><strong>{task.title}</strong><TaskMeta task={task} /></button>
        <div className="roadmap-row-track">{roadmap.ticks.map(tick => <i className="roadmap-grid-line" key={tick.date} style={{ '--left': `${tick.left}%` }} />)}<i className="roadmap-today-line" style={{ '--left': `${roadmap.todayLeft}%` }} /><button type="button" className={`roadmap-bar ${task.classification} ${task.marker ? 'marker' : ''}`} style={{ '--left': `${task.left}%`, '--width': `${task.width}%`, '--project-color': projectColor(task.project_id) }} onClick={() => setSelectedTask(task)} title={`${task.title} · ${roadmapDateLabel(task)}`}>{task.classification === 'planned' && !task.marker && <i style={{ width: `${Math.max(0, Math.min(100, Number(task.progress || 0)))}%` }} />}{task.milestone && <Diamond className="roadmap-milestone" size={12} />}</button></div>
      </div>) : <div className="roadmap-axis-empty"><CalendarClock size={20} /><span><strong>No work has usable dates in this scope.</strong><small>Open an off-axis task below to record a start, target, or due date.</small></span></div>}
    </div>

    <div className="roadmap-mobile-list">{mobileRows.length ? mobileRows.map(task => <RoadmapMobileCard key={task.id} task={task} onOpen={setSelectedTask} />) : <div className="lane-empty">No work matches this roadmap scope.</div>}</div>

    <section className="roadmap-unscheduled"><div><span className="eyebrow">Off-axis queue</span><h2>Unscheduled or incomplete dates <em>{roadmap.unscheduled.length}</em></h2><p>These items remain visible without inventing a bar.</p></div>{roadmap.unscheduled.length ? <div className="roadmap-unscheduled-grid">{roadmap.unscheduled.map(task => <button type="button" key={task.id} onClick={() => setSelectedTask(task)} style={{ '--project-color': projectColor(task.project_id) }}><span><i />{task.project_name}<em>{pretty(task.status)}</em></span><strong>{task.title}</strong><TaskMeta task={task} />{task.warnings.length > 0 && <small className="roadmap-warning"><CircleAlert size={11} />{task.warnings.join(' · ')}</small>}</button>)}</div> : <div className="roadmap-all-scheduled"><Check size={17} />Every visible task has usable dates.</div>}</section>

    {selectedTask && <RoadmapTaskModal key={selectedTask.id} task={classifyRoadmapTask(selectedTask)} onClose={() => setSelectedTask(null)} onSave={onSaveTask} />}
  </section>
}
