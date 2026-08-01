import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  BadgeDollarSign,
  Bot,
  CandlestickChart,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  CirclePlay,
  Clock3,
  Cloud,
  Cpu,
  FolderGit2,
  GitBranch,
  LayoutDashboard,
  ListChecks,
  LoaderCircle,
  PauseCircle,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  ServerCog,
  Telescope,
  UserRound,
  WandSparkles,
  X,
} from 'lucide-react'

const navItems = [
  ['overview', 'Overview', LayoutDashboard],
  ['strategy-analysis', 'Strategy Analysis', Telescope],
  ['trading-rd', 'Trading R&D', CandlestickChart],
  ['monetization', 'Monetization', BadgeDollarSign],
  ['infrastructure', 'Infrastructure', ServerCog],
  ['paused', 'Paused / External', PauseCircle],
]

const workers = {
  codex: { label: 'Codex', tone: 'emerald' },
  claude: { label: 'Claude', tone: 'orange' },
  gemini: { label: 'Gemini', tone: 'blue' },
  grok: { label: 'Grok', tone: 'ink' },
  ollama: { label: 'Ollama', tone: 'violet' },
  perplexity: { label: 'Perplexity', tone: 'cyan' },
  owner: { label: 'Owner', tone: 'slate' },
}

const statusOptions = ['open', 'assigned', 'in_progress', 'running', 'review', 'blocked', 'done']

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`)
  return payload
}

function prettyStatus(value) {
  return String(value || 'open').replaceAll('_', ' ')
}

function relativeDate(value) {
  if (!value) return 'No activity'
  const date = new Date(value.length === 10 ? `${value}T00:00:00` : value)
  if (Number.isNaN(date.getTime())) return value
  const days = Math.floor((Date.now() - date.getTime()) / 86_400_000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 14) return `${days}d ago`
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function WorkerBadge({ name, recommended = false }) {
  const key = String(name || '').toLowerCase()
  const worker = workers[key] || { label: name || 'Unassigned', tone: 'slate' }
  return (
    <span className={`worker-badge ${worker.tone}`}>
      <Bot size={14} aria-hidden="true" />
      <span>{worker.label}</span>
      {recommended && <span className="recommended-mark">recommended</span>}
    </span>
  )
}

function StatusPill({ value }) {
  return <span className={`status-pill status-${value}`}>{prettyStatus(value)}</span>
}

function Priority({ value }) {
  const labels = { 1: 'Critical', 2: 'High', 3: 'Medium', 4: 'Low', 5: 'Backlog' }
  return <span className={`priority priority-${value}`}>{labels[value] || 'Medium'}</span>
}

function Sidebar({ active, onChange, pausedCount }) {
  return (
    <aside className="sidebar">
      <div className="brand-mark" aria-label="Cortex Portfolio"><span /><span /><span /><span /></div>
      <nav aria-label="Portfolio views">
        {navItems.map(([id, label, Icon]) => (
          <button key={id} className={active === id ? 'active' : ''} onClick={() => onChange(id)}>
            <Icon size={18} aria-hidden="true" />
            <span>{label}</span>
            {id === 'paused' && pausedCount > 0 && <em>{pausedCount}</em>}
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <span className="local-dot" />
        <span>Local control plane</span>
      </div>
    </aside>
  )
}

function SummaryStrip({ summary }) {
  const cells = [
    ['Active projects', summary.active_projects, FolderGit2, 'teal'],
    ['Needs review', summary.needs_review, ListChecks, summary.needs_review ? 'amber' : 'slate'],
    ['Blocked', summary.blocked, CircleAlert, summary.blocked ? 'coral' : 'slate'],
    ['Unassigned', summary.unassigned, UserRound, summary.unassigned ? 'amber' : 'slate'],
    ['Local queue', summary.local_queue, Cpu, 'teal'],
    ['Cloud queue', summary.cloud_queue, Cloud, 'blue'],
  ]
  return (
    <section className="summary-strip" aria-label="Portfolio summary">
      {cells.map(([label, value, Icon, tone]) => (
        <div className="summary-cell" key={label}>
          <span className={`summary-icon ${tone}`}><Icon size={17} /></span>
          <span><small>{label}</small><strong>{value}</strong></span>
        </div>
      ))}
    </section>
  )
}

function ProjectTable({ projects, selectedId, onSelect }) {
  if (!projects.length) {
    return <div className="empty-state"><FolderGit2 size={24} /><strong>No projects in this view</strong><span>Choose another portfolio lane.</span></div>
  }
  return (
    <div className="project-table-wrap">
      <table className="project-table">
        <thead><tr>
          <th>Project</th><th>Status</th><th>Priority</th><th>Current objective</th>
          <th>Git</th><th>Tasks</th><th>Assigned / recommended</th><th>Activity</th><th />
        </tr></thead>
        <tbody>
          {projects.map(project => {
            const assigned = project.assignees?.[0]
            const recommended = project.recommendations?.[0]
            return (
              <tr key={project.project_id} className={selectedId === project.project_id ? 'selected' : ''} onClick={() => onSelect(project.project_id)}>
                <td><span className="project-name"><span className={`health-dot ${project.health}`} />{project.name}</span><small>{project.program}</small></td>
                <td><StatusPill value={project.status === 'active' ? (project.running_tasks ? 'running' : project.health) : project.status} /></td>
                <td><Priority value={project.priority} /></td>
                <td className="objective">{project.current_goal || 'Goal not set'}</td>
                <td><span className="git-cell"><GitBranch size={13} />{project.branch || 'no git'}</span><small>{!project.git_status_available ? 'status deferred' : project.modified + project.untracked ? `${project.modified + project.untracked} changed` : 'clean'}</small></td>
                <td><strong>{project.task_count}</strong><small>{project.review_tasks ? `${project.review_tasks} review` : 'active queue'}</small></td>
                <td>{assigned ? <WorkerBadge name={assigned} /> : recommended ? <WorkerBadge name={recommended} recommended /> : <span className="muted">No queue</span>}</td>
                <td>{relativeDate(project.last_commit_date || project.updated_at)}</td>
                <td><ChevronRight size={16} /></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function TaskQueue({ tasks, onUpdate, focusRef }) {
  const [mode, setMode] = useState('actionable')
  const visible = tasks.filter(task => mode === 'assigned' ? task.assignee : !task.assignee)
  return (
    <section className="panel task-panel" ref={focusRef}>
      <div className="panel-heading">
        <div><h2>Task assignments</h2><p>Recommended routing stays separate from an explicit assignment.</p></div>
        <div className="segmented">
          <button className={mode === 'actionable' ? 'active' : ''} onClick={() => setMode('actionable')}>Recommended</button>
          <button className={mode === 'assigned' ? 'active' : ''} onClick={() => setMode('assigned')}>Assigned</button>
        </div>
      </div>
      <div className="task-list" role="list">
        {visible.slice(0, 8).map(task => (
          <div className="task-row" role="listitem" key={task.id}>
            <span className={`task-state-dot state-${task.status}`} />
            <div className="task-copy"><strong>{task.title}</strong><span>{task.project_name}</span></div>
            <Priority value={task.priority} />
            <div className="task-route">
              <WorkerBadge name={task.assignee || task.recommended_worker} recommended={!task.assignee} />
              <small>{task.route.reason || task.route.reasons?.at(-1)}</small>
            </div>
            <span className={`budget budget-${task.route.budget}`}>{task.route.budget}</span>
            {!task.assignee ? (
              <button className="inline-action" onClick={() => onUpdate(task.id, { assignee: task.recommended_worker, status: 'assigned' })}>Assign</button>
            ) : (
              <select aria-label={`Status for ${task.title}`} value={task.status} onChange={event => onUpdate(task.id, { status: event.target.value })}>
                {statusOptions.map(option => <option key={option} value={option}>{prettyStatus(option)}</option>)}
              </select>
            )}
          </div>
        ))}
        {!visible.length && <div className="compact-empty"><CheckCircle2 size={18} />Nothing waiting in this queue.</div>}
      </div>
    </section>
  )
}

function WorkerCapacity({ workerRows }) {
  return (
    <section className="panel worker-panel">
      <div className="panel-heading"><div><h2>Worker capacity</h2><p>CLI availability and live assignment load.</p></div></div>
      <div className="worker-list">
        {workerRows.map(worker => (
          <div className="worker-row" key={worker.name}>
            <WorkerBadge name={worker.name} />
            <span className={`availability ${worker.availability}`}>{worker.availability}</span>
            <strong>{worker.assigned}</strong><small>assigned</small>
            <span className="rec-count">+{worker.recommended} recommended</span>
          </div>
        ))}
      </div>
    </section>
  )
}

function DetailDrawer({ project, onClose, onTaskUpdate, onProjectUpdate, onAddTask }) {
  if (!project) return null
  const dirty = project.modified + project.untracked
  const nextTask = project.tasks.find(task => !task.assignee && task.status === 'open')
  return (
    <aside className="detail-drawer" aria-label={`${project.name} details`}>
      <div className="drawer-title">
        <div><span className={`health-dot ${project.health}`} /><h2>{project.name}</h2></div>
        <button className="icon-button" onClick={onClose} aria-label="Close details"><X size={18} /></button>
      </div>
      <div className="drawer-status">
        <StatusPill value={project.status} />
        <Priority value={project.priority} />
        <select value={project.status} onChange={event => onProjectUpdate(project.project_id, { status: event.target.value })}>
          <option value="active">Active in Cortex</option><option value="paused">Paused / external</option><option value="archived">Archived</option>
        </select>
      </div>
      <section><label>Current goal</label><p>{project.current_goal || 'No goal has been set.'}</p></section>
      <section className="git-evidence">
        <div><label>Git evidence</label><span><GitBranch size={14} />{project.branch || 'Not under Git'}</span></div>
        <strong>{!project.git_status_available ? 'Live status deferred' : `${project.ahead != null ? `${project.ahead} ahead · ${project.behind} behind` : 'No upstream'}${dirty ? ` · ${dirty} changed` : ' · clean'}`}</strong>
        {(project.warnings?.length > 0 || project.git_status_note) && <small><AlertTriangle size={13} />{project.warnings?.[0] || project.git_status_note}</small>}
      </section>
      <section className="drawer-tasks">
        <div className="section-title"><label>Current tasks ({project.tasks.length})</label><button onClick={() => onAddTask(project.project_id)}><Plus size={14} />Add</button></div>
        {project.tasks.slice(0, 8).map(task => (
          <div className="drawer-task" key={task.id}>
            <div><strong>{task.title}</strong><span><StatusPill value={task.status} /><Priority value={task.priority} /></span></div>
            <div className="drawer-task-controls">
              <select value={task.assignee || ''} onChange={event => onTaskUpdate(task.id, { assignee: event.target.value || null, status: event.target.value && task.status === 'open' ? 'assigned' : task.status })}>
                <option value="">Recommended: {workers[task.recommended_worker]?.label || task.recommended_worker}</option>
                {Object.entries(workers).map(([id, worker]) => <option key={id} value={id}>{worker.label}</option>)}
              </select>
              <select value={task.status} onChange={event => onTaskUpdate(task.id, { status: event.target.value })}>
                {statusOptions.map(option => <option key={option} value={option}>{prettyStatus(option)}</option>)}
              </select>
            </div>
          </div>
        ))}
        {!project.tasks.length && <div className="compact-empty">No active tasks.</div>}
      </section>
      {nextTask && (
        <section className="recommendation-box">
          <label>Next recommended assignment</label>
          <WorkerBadge name={nextTask.recommended_worker} recommended />
          <p>{nextTask.route.reasons?.at(-1)}</p>
          <div><span>{nextTask.route.budget} budget</span><span>complexity {nextTask.route.complexity}/10</span></div>
          <button className="primary full" onClick={() => onTaskUpdate(nextTask.id, { assignee: nextTask.recommended_worker, status: 'assigned' })}><WandSparkles size={15} />Assign recommended</button>
        </section>
      )}
      <div className="drawer-path"><FolderGit2 size={14} /><span title={project.repo_path}>{project.repo_path}</span></div>
    </aside>
  )
}

function NewTaskModal({ projects, initialProject, onClose, onCreated }) {
  const projectChoices = projects.filter(project => project.status === 'active' || project.project_id === initialProject)
  const [form, setForm] = useState({ project_id: initialProject || projectChoices[0]?.project_id || '', title: '', type: 'review', priority: 3, risk: 'auto', budget: '', acceptance: '', assignee: '' })
  const [saving, setSaving] = useState(false)
  async function submit(event) {
    event.preventDefault(); setSaving(true)
    try {
      const body = { ...form, budget: form.budget || null, assignee: form.assignee || null }
      if (body.assignee) body.status = 'assigned'
      await request('/api/tasks', { method: 'POST', body: JSON.stringify(body) })
      onCreated()
    } finally { setSaving(false) }
  }
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={event => event.target === event.currentTarget && onClose()}>
      <form className="task-modal" onSubmit={submit}>
        <div className="modal-title"><div><span className="eyebrow">Portfolio queue</span><h2>Add a bounded task</h2></div><button type="button" className="icon-button" onClick={onClose} aria-label="Close task form"><X size={18} /></button></div>
        <label>Project<select required value={form.project_id} onChange={e => setForm({ ...form, project_id: e.target.value })}>{projectChoices.map(project => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}</select></label>
        <label>Task title<input required autoFocus value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} placeholder="One concrete outcome" /></label>
        <div className="form-grid">
          <label>Type<select value={form.type} onChange={e => setForm({ ...form, type: e.target.value })}>{['review','code','research','planning','docs','data','other'].map(v => <option key={v}>{v}</option>)}</select></label>
          <label>Priority<select value={form.priority} onChange={e => setForm({ ...form, priority: Number(e.target.value) })}>{[1,2,3,4,5].map(v => <option key={v} value={v}>P{v}</option>)}</select></label>
          <label>Risk<select value={form.risk} onChange={e => setForm({ ...form, risk: e.target.value })}>{['auto','low','medium','high'].map(v => <option key={v}>{v}</option>)}</select></label>
          <label>Budget<select value={form.budget} onChange={e => setForm({ ...form, budget: e.target.value })}><option value="">Auto-route</option>{['local','small','medium','large'].map(v => <option key={v}>{v}</option>)}</select></label>
        </div>
        <label>Acceptance check<textarea value={form.acceptance} onChange={e => setForm({ ...form, acceptance: e.target.value })} placeholder="Done when…" rows="3" /></label>
        <label>Assign now<select value={form.assignee} onChange={e => setForm({ ...form, assignee: e.target.value })}><option value="">Leave for automatic routing</option>{Object.entries(workers).map(([id, worker]) => <option key={id} value={id}>{worker.label}</option>)}</select></label>
        <div className="modal-actions"><button type="button" onClick={onClose}>Cancel</button><button className="primary" disabled={saving}>{saving ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />}Add task</button></div>
      </form>
    </div>
  )
}

export default function App() {
  const [data, setData] = useState(null)
  const [activeNav, setActiveNav] = useState('overview')
  const [selectedId, setSelectedId] = useState(null)
  const [search, setSearch] = useState('')
  const [modalProject, setModalProject] = useState(undefined)
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')
  const taskFocus = useRef(null)

  async function load(silent = false) {
    if (!silent) setError('')
    try { setData(await request('/api/portfolio')) }
    catch (err) { setError(err.message) }
  }
  useEffect(() => { load() }, [])
  useEffect(() => {
    if (!toast) return undefined
    const timer = setTimeout(() => setToast(''), 2800)
    return () => clearTimeout(timer)
  }, [toast])

  const filteredProjects = useMemo(() => {
    if (!data) return []
    let rows = data.projects.filter(project => project.status !== 'archived')
    if (activeNav === 'overview') rows = rows.filter(project => project.status === 'active')
    else if (activeNav === 'paused') rows = rows.filter(project => project.status === 'paused')
    else rows = rows.filter(project => project.program.toLowerCase().replaceAll('_', '-') === activeNav || project.program.toLowerCase().includes(activeNav.replace('-rd', '')))
    if (search.trim()) {
      const term = search.toLowerCase()
      rows = rows.filter(project => `${project.name} ${project.current_goal || ''} ${project.program}`.toLowerCase().includes(term))
    }
    return rows.sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name))
  }, [data, activeNav, search])

  const visibleIds = new Set(filteredProjects.map(project => project.project_id))
  const visibleTasks = data?.tasks.filter(task => visibleIds.has(task.project_id)) || []
  const selected = data?.projects.find(project => project.project_id === selectedId) || null

  async function updateTask(id, fields) {
    try {
      await request(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(fields) })
      setToast(fields.assignee ? 'Assignment saved' : 'Task updated')
      await load(true)
    } catch (err) { setError(err.message) }
  }
  async function updateProject(id, fields) {
    try {
      await request(`/api/projects/${id}`, { method: 'PATCH', body: JSON.stringify(fields) })
      setToast('Project status updated')
      await load(true)
    } catch (err) { setError(err.message) }
  }

  if (!data && !error) return <div className="app-loading"><span className="brand-mark"><span /><span /><span /><span /></span><LoaderCircle className="spin" size={20} />Loading local portfolio…</div>
  if (!data) return <div className="app-loading error"><CircleAlert size={24} /><strong>Dashboard unavailable</strong><span>{error}</span><button onClick={() => load()}><RotateCcw size={15} />Retry</button></div>

  return (
    <div className="app-shell">
      <Sidebar active={activeNav} onChange={setActiveNav} pausedCount={data.summary.paused_projects} />
      <div className={`content-shell ${selected ? 'drawer-open' : ''}`}>
        <main>
          <header className="topbar">
            <div><span className="eyebrow">Local AI control plane</span><h1>Cortex Portfolio</h1></div>
            <div className="top-actions">
              <label className="search-box"><Search size={16} /><input aria-label="Search projects" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search projects" /></label>
              <button className="primary" onClick={() => setModalProject(null)}><Plus size={16} />Add task</button>
              <button onClick={() => taskFocus.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}><WandSparkles size={16} />Route work</button>
              <button className="icon-button" onClick={() => load()} aria-label="Refresh"><RefreshCw size={17} /></button>
            </div>
          </header>
          {error && <div className="error-banner"><CircleAlert size={15} />{error}<button onClick={() => setError('')}><X size={14} /></button></div>}
          <div className="main-content">
            <SummaryStrip summary={data.summary} />
            <section className="portfolio-section">
              <div className="section-heading"><div><span className="eyebrow">{navItems.find(item => item[0] === activeNav)?.[1]}</span><h2>Project workstreams</h2></div><span>{filteredProjects.length} visible · {data.summary.attention_projects} active need attention</span></div>
              <ProjectTable projects={filteredProjects} selectedId={selectedId} onSelect={setSelectedId} />
            </section>
            <div className="lower-grid">
              <TaskQueue tasks={visibleTasks} onUpdate={updateTask} focusRef={taskFocus} />
              <WorkerCapacity workerRows={data.workers} />
            </div>
            <footer><span>SQLite source of truth</span><span>{data.database}</span><span>Updated {relativeDate(data.generated_at)}</span></footer>
          </div>
        </main>
        <DetailDrawer project={selected} onClose={() => setSelectedId(null)} onTaskUpdate={updateTask} onProjectUpdate={updateProject} onAddTask={id => setModalProject(id)} />
      </div>
      {modalProject !== undefined && <NewTaskModal projects={data.projects} initialProject={modalProject || ''} onClose={() => setModalProject(undefined)} onCreated={async () => { setModalProject(undefined); setToast('Task added to the queue'); await load(true) }} />}
      {toast && <div className="toast"><Check size={15} />{toast}</div>}
    </div>
  )
}
