import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, Bot, Check, CheckCircle2, ChevronRight, CircleAlert,
  CirclePlay, Clock3, Cpu, FolderGit2, GitBranch, LayoutDashboard,
  LoaderCircle, MoreHorizontal, PauseCircle, Plus, RefreshCw, RotateCcw,
  Search, Sparkles, Telescope, UserRound, WandSparkles, X,
} from 'lucide-react'

const workers = {
  codex: { label: 'Codex', tone: 'emerald' }, claude: { label: 'Claude', tone: 'orange' },
  gemini: { label: 'Gemini', tone: 'blue' }, grok: { label: 'Grok', tone: 'ink' },
  ollama: { label: 'Ollama', tone: 'violet' }, perplexity: { label: 'Perplexity', tone: 'cyan' },
  owner: { label: 'Owner', tone: 'slate' }, deterministic: { label: 'No-token plan', tone: 'slate' },
}

const navItems = [
  ['overview', 'Today', LayoutDashboard], ['strategy-analysis', 'Strategy Analysis', Telescope],
  ['paused', 'Paused / external', PauseCircle],
]
const statuses = ['open', 'assigned', 'in_progress', 'running', 'review', 'blocked', 'done']

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`)
  return payload
}

function pretty(value) { return String(value || '').replaceAll('_', ' ') }
function projectColor(project) {
  const palette = ['#1b7468', '#8057a8', '#b55d42', '#4270a6', '#a67b2f', '#596b7c']
  return palette[[...project.project_id].reduce((sum, char) => sum + char.charCodeAt(0), 0) % palette.length]
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
  const worker = workers[String(name || '').toLowerCase()] || { label: name || 'Unassigned', tone: 'slate' }
  return <span className={`worker-badge ${worker.tone}`}><Bot size={13} />{worker.label}{recommended && <em>recommended</em>}</span>
}

function StatusPill({ value }) { return <span className={`status-pill status-${value}`}>{pretty(value)}</span> }
function Priority({ value }) { return <span className={`priority p${value}`}>P{value}</span> }

function Sidebar({ data, active, onChange, selectedId, onSelect }) {
  const activeProjects = data.projects.filter(project => project.status === 'active').sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name))
  return (
    <aside className="sidebar">
      <div className="wordmark"><span className="brand-mark"><i /><i /><i /><i /></span><div><strong>Cortex</strong><small>AI project manager</small></div></div>
      <nav aria-label="Portfolio views">
        {navItems.map(([id, label, Icon]) => <button key={id} className={active === id ? 'active' : ''} onClick={() => onChange(id)}><Icon size={17} /><span>{label}</span>{id === 'paused' && <em>{data.summary.paused_projects}</em>}</button>)}
      </nav>
      <div className="project-rail-title"><span>Active projects</span><em>{activeProjects.length}</em></div>
      <div className="project-rail">
        {activeProjects.map(project => (
          <button key={project.project_id} className={selectedId === project.project_id ? 'selected' : ''} onClick={() => onSelect(project.project_id)}>
            <span className="project-monogram" style={{ '--project-color': projectColor(project) }}>{project.name.slice(0, 2).toUpperCase()}</span>
            <span><strong>{project.name}</strong><small>{project.current_goal || 'Goal not set'}</small></span>
            {(project.review_tasks || project.blocked_tasks) ? <em className="attention-count">{project.review_tasks + project.blocked_tasks}</em> : <ChevronRight size={14} />}
          </button>
        ))}
      </div>
      <div className="sidebar-foot"><span className="local-dot" /><span>Local-only control plane</span></div>
    </aside>
  )
}

function Hero({ planner, setPlanner, onContinue, busy, onManual }) {
  return (
    <section className="hero">
      <div><span className="eyebrow">Your daily starting point</span><h1>Move the right project forward.</h1><p>Cortex finds the next decision or bounded task. You approve; the agents do the work.</p></div>
      <div className="hero-actions">
        <button className="continue-button" onClick={onContinue} disabled={busy}>{busy ? <LoaderCircle className="spin" size={20} /> : <Sparkles size={20} />}Continue with AI</button>
        <div className="plan-mode"><span>Planner</span><button className={planner === 'codex' ? 'active' : ''} onClick={() => setPlanner('codex')}>Smart · Codex</button><button className={planner === 'ollama' ? 'active' : ''} onClick={() => setPlanner('ollama')}>Local · Ollama</button></div>
        <details className="more-menu"><summary><MoreHorizontal size={16} />More</summary><button onClick={onManual}><Plus size={14} />Manual task</button></details>
      </div>
    </section>
  )
}

function DecisionLane({ tasks, projects, onSelect, onUpdate }) {
  return (
    <section className="workflow-band decision-band" id="needs-decision">
      <div className="band-heading"><span className="band-icon amber"><UserRound size={18} /></span><div><h2>Needs your decision <em>{tasks.length}</em></h2><p>Only the work that cannot safely continue without you.</p></div></div>
      <div className="decision-list">
        {tasks.slice(0, 4).map(task => <article key={task.id} data-task={task.id}>
          <div><span className="project-kicker">{projects[task.project_id]?.name}</span><h3>{task.title}</h3><p>{String(task.assignee || '').toLowerCase() === 'owner' ? 'This task is assigned to you.' : task.status === 'blocked' ? 'This agent needs a decision or missing input.' : task.status === 'review' ? 'A completed result is ready for your review.' : 'This manual step needs your attention.'}</p></div>
          <StatusPill value={task.status} />
          <button onClick={() => onSelect(task.project_id)}>{task.status === 'review' ? 'Review' : 'Open'} <ChevronRight size={14} /></button>
        </article>)}
        {!tasks.length && <div className="lane-empty"><CheckCircle2 size={17} />Nothing is waiting on you.</div>}
      </div>
    </section>
  )
}

function WorkingLane({ tasks, projects, onStart, onSelect }) {
  return (
    <section className="workflow-band working-band" id="ai-working">
      <div className="band-heading"><span className="band-icon teal"><Cpu size={18} /></span><div><h2>AI working <em>{tasks.length}</em></h2><p>Assigned work, in-flight runs, and the next safe starts.</p></div></div>
      <div className="working-grid">
        {tasks.slice(0, 5).map(task => <article key={task.id} data-task={task.id}>
          <div className="working-top"><span className="project-monogram" style={{ '--project-color': projectColor(projects[task.project_id]) }}>{projects[task.project_id]?.name.slice(0, 2).toUpperCase()}</span><StatusPill value={task.status} /></div>
          <span className="project-kicker">{projects[task.project_id]?.name}</span><h3>{task.title}</h3>
          <div className="working-meta"><WorkerBadge name={task.assignee || task.recommended_worker} /><span>{task.route.budget} effort</span></div>
          {task.status === 'assigned' && !['owner', 'perplexity'].includes(String(task.assignee || '').toLowerCase()) ? <button className="soft-action" onClick={() => onStart(task)}>Start safely <CirclePlay size={15} /></button> : <button className="text-action" onClick={() => onSelect(task.project_id)}>Open project <ChevronRight size={14} /></button>}
        </article>)}
        {!tasks.length && <div className="lane-empty"><Clock3 size={17} />No agents are assigned yet.</div>}
      </div>
    </section>
  )
}

function SuggestionCard({ suggestion, project, onApprove, onDismiss }) {
  return (
    <article className="suggestion-card" data-suggestion={suggestion.id}>
      <div className="suggestion-top"><span className="project-kicker">{project?.name}</span><Priority value={suggestion.priority} /></div>
      <h3>{suggestion.title}</h3><p className="why">{suggestion.why}</p>
      <div className="acceptance"><Check size={14} /><span><strong>Done when</strong>{suggestion.acceptance}</span></div>
      <div className="route-strip"><WorkerBadge name={suggestion.recommended_worker} recommended /><span>{pretty(suggestion.action)}</span><span>{suggestion.budget} effort</span></div>
      <div className="card-actions"><button className="approve" onClick={() => onApprove(suggestion, true)}>Approve & start</button><button onClick={() => onApprove(suggestion, false)}>Queue</button><button className="icon-button" title="Dismiss suggestion" aria-label={`Dismiss ${suggestion.title}`} onClick={() => onDismiss(suggestion.id)}><X size={15} /></button></div>
      <small className="source">Planned by {workers[suggestion.source_worker]?.label || suggestion.source_worker}</small>
    </article>
  )
}

function RecommendationLane({ suggestions, projects, onApprove, onDismiss, onPlan, planning }) {
  return (
    <section className="workflow-band recommendation-band" id="recommendations">
      <div className="band-heading"><span className="band-icon violet"><WandSparkles size={18} /></span><div><h2>Recommended next moves <em>{suggestions.length}</em></h2><p>AI-proposed work is inert until you approve it.</p></div><button className="band-action" onClick={onPlan} disabled={planning}>{planning ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}Plan selected project</button></div>
      <div className="recommendation-grid">
        {suggestions.slice(0, 6).map(suggestion => <SuggestionCard key={suggestion.id} suggestion={suggestion} project={projects[suggestion.project_id]} onApprove={onApprove} onDismiss={onDismiss} />)}
        {!suggestions.length && <div className="plan-empty"><WandSparkles size={22} /><div><strong>No plan is waiting.</strong><span>Pick a project, then let Codex or Ollama propose three bounded moves.</span></div><button onClick={onPlan} disabled={planning}>{planning ? 'Planning…' : 'Plan next moves'}</button></div>}
      </div>
    </section>
  )
}

function ProjectsTable({ projects, selectedId, onSelect, onPlan }) {
  return <section className="projects-section"><div className="section-title"><div><span className="eyebrow">Portfolio evidence</span><h2>Project workstreams</h2></div><span>{projects.length} visible</span></div><div className="project-table-wrap"><table><thead><tr><th>Project</th><th>Priority</th><th>Goal</th><th>Queue</th><th>Repository</th><th>Activity</th><th /></tr></thead><tbody>
    {projects.map(project => <tr key={project.project_id} className={selectedId === project.project_id ? 'selected' : ''} onClick={() => onSelect(project.project_id)}><td><span className="project-cell"><span className="health-dot" data-health={project.health} />{project.name}</span><small>{project.program}</small></td><td><Priority value={project.priority} /></td><td>{project.current_goal || 'Goal not set'}</td><td><strong>{project.task_count}</strong><small>{project.suggestion_count} suggestions</small></td><td><span className="repo-state"><GitBranch size={13} />{project.branch || 'No Git'}</span><small>{project.git_status_available ? `${project.modified + project.untracked} changed` : 'status deferred'}</small></td><td>{relativeDate(project.updated_at)}</td><td><button className="table-plan" onClick={event => { event.stopPropagation(); onPlan(project.project_id) }}>Plan <Sparkles size={13} /></button></td></tr>)}
  </tbody></table></div></section>
}

function ProjectDrawer({ project, onClose, onPlan, onTaskUpdate, onStart, onManual }) {
  if (!project) return null
  return <aside className="drawer"><div className="drawer-head"><div><span className="project-monogram large" style={{ '--project-color': projectColor(project) }}>{project.name.slice(0, 2).toUpperCase()}</span><span><small>{project.program}</small><h2>{project.name}</h2></span></div><button className="icon-button" onClick={onClose}><X size={18} /></button></div>
    <div className="drawer-primary"><button onClick={() => onPlan(project.project_id, 'codex')}><Sparkles size={16} />Ask Codex to plan next</button><button onClick={() => onPlan(project.project_id, 'ollama')}><Cpu size={16} />Use local planner</button></div>
    <section><label>Current goal</label><p>{project.current_goal || 'No goal has been set.'}</p></section>
    <section className="drawer-evidence"><label>Repository evidence</label><div><GitBranch size={14} />{project.branch || 'Not under Git'}<span>{project.git_status_available ? `${project.modified + project.untracked} changed` : 'live status deferred'}</span></div>{project.warnings?.[0] && <small><AlertTriangle size={13} />{project.warnings[0]}</small>}</section>
    <section><div className="drawer-section-head"><label>Active work ({project.tasks.length})</label><button onClick={() => onManual(project.project_id)}><Plus size={13} />Manual</button></div><div className="drawer-task-list">{project.tasks.map(task => <div className="drawer-task" key={task.id}><div><strong>{task.title}</strong><span><StatusPill value={task.status} /><WorkerBadge name={task.assignee || task.recommended_worker} /></span></div><div>{task.status === 'assigned' && !['owner', 'perplexity'].includes(String(task.assignee || '').toLowerCase()) && <button onClick={() => onStart(task)}><CirclePlay size={14} />Start</button>}<select value={task.status} onChange={event => onTaskUpdate(task.id, { status: event.target.value })}>{statuses.map(status => <option key={status} value={status}>{pretty(status)}</option>)}</select></div></div>)}{!project.tasks.length && <div className="lane-empty">No active tasks.</div>}</div></section>
    <div className="drawer-path"><FolderGit2 size={14} /><span title={project.repo_path}>{project.repo_path}</span></div>
  </aside>
}

function ManualTaskModal({ projects, initialProject, onClose, onCreated }) {
  const choices = projects.filter(project => project.status === 'active')
  const [form, setForm] = useState({ project_id: initialProject || choices[0]?.project_id || '', title: '', type: 'review', acceptance: '' })
  const [saving, setSaving] = useState(false)
  async function submit(event) { event.preventDefault(); setSaving(true); try { await request('/api/tasks', { method: 'POST', body: JSON.stringify(form) }); onCreated() } finally { setSaving(false) } }
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><form className="manual-modal" onSubmit={submit}><div className="modal-head"><div><span className="eyebrow">Advanced fallback</span><h2>Add a task yourself</h2><p>Usually, use “Plan next moves” and approve a suggestion.</p></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div><label>Project<select value={form.project_id} onChange={event => setForm({ ...form, project_id: event.target.value })}>{choices.map(project => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}</select></label><label>Outcome<input autoFocus required value={form.title} onChange={event => setForm({ ...form, title: event.target.value })} placeholder="One concrete outcome" /></label><label>Type<select value={form.type} onChange={event => setForm({ ...form, type: event.target.value })}>{['review', 'code', 'research', 'planning', 'docs', 'data', 'other'].map(type => <option key={type}>{type}</option>)}</select></label><label>Done when<textarea rows="3" value={form.acceptance} onChange={event => setForm({ ...form, acceptance: event.target.value })} placeholder="Observable acceptance check" /></label><div className="modal-actions"><button type="button" onClick={onClose}>Cancel</button><button className="primary" disabled={saving}>{saving ? <LoaderCircle className="spin" size={15} /> : <Plus size={15} />}Add to queue</button></div></form></div>
}

export default function App() {
  const [data, setData] = useState(null), [activeNav, setActiveNav] = useState('overview')
  const [selectedId, setSelectedId] = useState(null), [search, setSearch] = useState('')
  const [planner, setPlanner] = useState('codex'), [job, setJob] = useState(null)
  const [error, setError] = useState(''), [toast, setToast] = useState('')
  const [manualProject, setManualProject] = useState(undefined)
  const pollRef = useRef(null)

  async function load(silent = false) { if (!silent) setError(''); try { setData(await request('/api/portfolio')) } catch (err) { setError(err.message) } }
  useEffect(() => { load(); return () => clearInterval(pollRef.current) }, [])
  useEffect(() => { if (!toast) return undefined; const timer = setTimeout(() => setToast(''), 3500); return () => clearTimeout(timer) }, [toast])

  const projectMap = useMemo(() => Object.fromEntries((data?.projects || []).map(project => [project.project_id, project])), [data])
  const filteredProjects = useMemo(() => {
    if (!data) return []
    let rows = data.projects.filter(project => activeNav === 'paused' ? project.status === 'paused' : project.status === 'active')
    if (activeNav === 'strategy-analysis') rows = rows.filter(project => project.program.toLowerCase().includes('strategy'))
    if (search.trim()) { const term = search.toLowerCase(); rows = rows.filter(project => `${project.name} ${project.current_goal || ''}`.toLowerCase().includes(term)) }
    return rows.sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name))
  }, [data, activeNav, search])
  const visibleIds = new Set(filteredProjects.map(project => project.project_id))
  const visibleTasks = (data?.tasks || []).filter(task => visibleIds.has(task.project_id))
  const decisions = visibleTasks.filter(task => ['review', 'blocked'].includes(task.status) || ['owner', 'perplexity'].includes(String(task.assignee || '').toLowerCase()))
  const working = visibleTasks.filter(task => ['assigned', 'in_progress', 'running'].includes(task.status) && !['owner', 'perplexity'].includes(String(task.assignee || '').toLowerCase()))
  const suggestions = (data?.suggestions || []).filter(suggestion => visibleIds.has(suggestion.project_id))
  const selected = projectMap[selectedId] || null

  function watchJob(jobId, label) {
    clearInterval(pollRef.current); setJob({ id: jobId, label, status: 'running' })
    pollRef.current = setInterval(async () => {
      try {
        const payload = await request(`/api/jobs/${jobId}`)
        if (payload.job.status === 'running') return
        clearInterval(pollRef.current); setJob(null)
        if (payload.job.status === 'failed') throw new Error(payload.job.error)
        setToast(label === 'Planning' ? 'AI plan is ready for approval' : 'Agent run finished; review the result')
        await load(true)
        setTimeout(() => document.getElementById(label === 'Planning' ? 'recommendations' : 'needs-decision')?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
      } catch (err) { clearInterval(pollRef.current); setJob(null); setError(err.message) }
    }, 1500)
  }

  async function planProject(projectId = selectedId, workerOverride = planner, force = false) {
    const target = projectId || filteredProjects[0]?.project_id
    if (!target) { setError('Select an active project first.'); return }
    let allowCloud = false
    if (workerOverride === 'codex' && projectMap[target]?.privacy === 'restricted') {
      allowCloud = window.confirm('This project is marked restricted. Allow the Codex CLI to inspect its repository in read-only mode for this plan?')
      if (!allowCloud) return
    }
    try { const payload = await request(`/api/projects/${target}/plan`, { method: 'POST', body: JSON.stringify({ worker: workerOverride, force, allow_cloud: allowCloud }) }); watchJob(payload.job_id, 'Planning') } catch (err) { setError(err.message) }
  }

  async function continueWork() {
    try {
      const payload = await request('/api/continue', { method: 'POST', body: JSON.stringify({ worker: planner }) })
      if (payload.job_id) { watchJob(payload.job_id, 'Planning'); return }
      const focus = payload.focus
      if (focus.project_id) setSelectedId(focus.project_id)
      const target = focus.kind === 'suggestion' ? `[data-suggestion="${focus.id}"]` : `[data-task="${focus.id}"]`
      setTimeout(() => document.querySelector(target)?.scrollIntoView({ behavior: 'smooth', block: 'center' }), 80)
      setToast(focus.kind === 'decision' ? 'This item needs your decision' : focus.kind === 'suggestion' ? 'This is the next recommended move' : 'This is the next work item')
    } catch (err) { setError(err.message) }
  }

  async function approveSuggestion(suggestion, start) {
    let allowWrite = false
    if (start && suggestion.action === 'implement') {
      allowWrite = window.confirm('This will let the assigned agent edit an isolated Git worktree. Cortex will not merge the result. Continue?')
      if (!allowWrite) return
    }
    try {
      const payload = await request(`/api/suggestions/${suggestion.id}/approve`, { method: 'POST', body: JSON.stringify({ start, allow_write: allowWrite, approve_high_risk: false }) })
      if (payload.job_id) watchJob(payload.job_id, 'Working')
      else { setToast('Approved and assigned; ready to start'); await load(true) }
    } catch (err) { setError(err.message) }
  }

  async function startTask(task) {
    let allowWrite = false
    if (task.route.action === 'implement') {
      allowWrite = window.confirm('Start this implementation in an isolated Git worktree? Cortex will not merge it.')
      if (!allowWrite) return
    }
    try { const payload = await request(`/api/tasks/${task.id}/start`, { method: 'POST', body: JSON.stringify({ allow_write: allowWrite, approve_high_risk: false }) }); watchJob(payload.job_id, 'Working') } catch (err) { setError(err.message) }
  }

  async function updateTask(id, fields) { try { await request(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(fields) }); setToast('Task updated'); await load(true) } catch (err) { setError(err.message) } }
  async function dismissSuggestion(id) { try { await request(`/api/suggestions/${id}`, { method: 'PATCH', body: JSON.stringify({ status: 'dismissed' }) }); setToast('Suggestion dismissed'); await load(true) } catch (err) { setError(err.message) } }

  if (!data && !error) return <div className="app-loading"><LoaderCircle className="spin" size={22} />Loading your portfolio…</div>
  if (!data) return <div className="app-loading error"><CircleAlert size={24} /><strong>Dashboard unavailable</strong><span>{error}</span><button onClick={() => load()}><RotateCcw size={15} />Retry</button></div>

  return <div className="app-shell">
    <Sidebar data={data} active={activeNav} onChange={setActiveNav} selectedId={selectedId} onSelect={setSelectedId} />
    <div className={`content-shell ${selected ? 'drawer-open' : ''}`}><main>
      <header className="topbar"><div><span className="eyebrow">Local AI control plane</span><strong>Cortex Portfolio</strong></div><div><label className="search"><Search size={15} /><input aria-label="Search projects" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search projects" /></label><button className="icon-button" onClick={() => load()} aria-label="Refresh"><RefreshCw size={16} /></button></div></header>
      {error && <div className="error-banner"><CircleAlert size={15} /><span>{error}</span><button onClick={() => setError('')}><X size={14} /></button></div>}
      {job && <div className="job-banner"><LoaderCircle className="spin" size={16} /><span><strong>{job.label}</strong> continues in the background. You can keep using the dashboard.</span></div>}
      <div className="main-content">
        <Hero planner={planner} setPlanner={setPlanner} onContinue={continueWork} busy={Boolean(job)} onManual={() => setManualProject(null)} />
        <div className="summary-line"><span><strong>{data.summary.needs_decision}</strong> need you</span><span><strong>{data.summary.working}</strong> assigned / working</span><span><strong>{data.summary.recommendations}</strong> ready to approve</span><span><strong>{data.summary.active_projects}</strong> active projects</span><em>Plans are cached to conserve tokens</em></div>
        <DecisionLane tasks={decisions} projects={projectMap} onSelect={setSelectedId} onUpdate={updateTask} />
        <WorkingLane tasks={working} projects={projectMap} onStart={startTask} onSelect={setSelectedId} />
        <RecommendationLane suggestions={suggestions} projects={projectMap} onApprove={approveSuggestion} onDismiss={dismissSuggestion} onPlan={() => planProject()} planning={job?.label === 'Planning'} />
        <ProjectsTable projects={filteredProjects} selectedId={selectedId} onSelect={setSelectedId} onPlan={id => planProject(id)} />
        <footer><span>SQLite source of truth</span><span>{data.database}</span><span>No automatic merges or external actions</span></footer>
      </div>
    </main><ProjectDrawer project={selected} onClose={() => setSelectedId(null)} onPlan={planProject} onTaskUpdate={updateTask} onStart={startTask} onManual={id => setManualProject(id)} /></div>
    {manualProject !== undefined && <ManualTaskModal projects={data.projects} initialProject={manualProject || ''} onClose={() => setManualProject(undefined)} onCreated={async () => { setManualProject(undefined); setToast('Manual task added'); await load(true) }} />}
    {toast && <div className="toast"><Check size={15} />{toast}</div>}
  </div>
}
