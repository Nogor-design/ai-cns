import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity, AlertTriangle, Bot, CalendarRange, Check, CheckCircle2, ChevronRight, CircleAlert,
  CirclePlay, Clock3, Copy, Cpu, FolderGit2, FolderPlus, GitBranch, Gauge, LayoutDashboard,
  Github, LoaderCircle, MoreHorizontal, PauseCircle, Plus, RefreshCw, RotateCcw,
  Search, ShieldCheck, Sparkles, Trash2, UsersRound, UserRound, WandSparkles, X,
} from 'lucide-react'
import { filterAndGroupTimeline } from './timeline.js'
import { mirrorActionLabel, mirrorStatusMeta } from './githubMirror.js'
import RoadmapView from './RoadmapView.jsx'
import AddProjectModal from './AddProjectModal.jsx'
import RemoveProjectModal from './RemoveProjectModal.jsx'

const workers = {
  codex: { label: 'Codex', tone: 'emerald' }, claude: { label: 'Claude', tone: 'orange' },
  gemini: { label: 'Gemini', tone: 'blue' }, grok: { label: 'Grok', tone: 'ink' },
  ollama: { label: 'Ollama', tone: 'violet' }, perplexity: { label: 'Perplexity', tone: 'cyan' },
  owner: { label: 'Owner', tone: 'slate' }, deterministic: { label: 'No-token plan', tone: 'slate' },
}

const navItems = [
  ['overview', 'Today', LayoutDashboard], ['projects', 'All projects', FolderGit2],
  ['roadmap', 'Roadmap', CalendarRange], ['timeline', 'Timeline', Clock3],
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

function durationLabel(run) {
  if (!run?.started_at) return 'Not started'
  const start = new Date(run.started_at).getTime()
  const end = run.ended_at ? new Date(run.ended_at).getTime() : Date.now()
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function executionWorker(run) {
  return String(run?.model || 'unknown').split(':')[0]
}

function numberLabel(value) { return new Intl.NumberFormat().format(value || 0) }

// Tail a run's live output. The server returns everything written after a byte
// offset, so a dropped poll costs one interval rather than the whole transcript.
function useRunOutput(runId, isRunning) {
  const [text, setText] = useState('')
  const [done, setDone] = useState(false)
  useEffect(() => {
    if (!runId) { setText(''); setDone(false); return undefined }
    let cancelled = false, timer = null, offset = 0, failures = 0
    setText(''); setDone(false)
    async function tick() {
      try {
        const payload = await request(`/api/runs/${runId}/output?offset=${offset}`)
        if (cancelled) return
        failures = 0
        offset = payload.offset
        // Cap retained text so a very chatty agent cannot grow the tab forever.
        if (payload.text) setText(prev => (prev + payload.text).slice(-120000))
        if (payload.running) timer = setTimeout(tick, 1000)
        else setDone(true)
      } catch {
        if (cancelled) return
        // A run's log may not exist yet, or may have been cleaned up. Back off
        // a few times, then stop rather than polling a dead endpoint forever.
        if (++failures > 4) { setDone(true); return }
        timer = setTimeout(tick, 2000)
      }
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [runId, isRunning])
  return { text, done }
}

function LiveTail({ runId, lines = 2 }) {
  const { text } = useRunOutput(runId, true)
  if (!text) return <div className="live-tail waiting"><LoaderCircle className="spin" size={12} />Waiting for the worker to produce output…</div>
  const tail = text.split('\n').filter(Boolean).slice(-lines).join('\n')
  return <pre className="live-tail">{tail}</pre>
}

function gitLabel(project) {
  if (!project.git_check) return 'Not checked'
  if (!project.is_git) return 'Not a Git repository'
  if ((project.behind || 0) > 0) return `${project.behind} behind remote`
  if ((project.ahead || 0) > 0) return `${project.ahead} commit${project.ahead === 1 ? '' : 's'} to push`
  if ((project.modified || 0) + (project.untracked || 0) > 0) return `${project.modified + project.untracked} local changes`
  if (project.git_check.fetched && project.ahead == null && project.behind == null) return 'Fetched; upstream not configured'
  return project.git_check.fetched ? 'Up to date with remote' : 'Working tree clean'
}

function WorkerBadge({ name, recommended = false }) {
  const worker = workers[String(name || '').toLowerCase()] || { label: name || 'Unassigned', tone: 'slate' }
  return <span className={`worker-badge ${worker.tone}`}><Bot size={13} />{worker.label}{recommended && <em>recommended</em>}</span>
}

function StatusPill({ value }) { return <span className={`status-pill status-${value}`}>{pretty(value)}</span> }
function Priority({ value }) { return <span className={`priority p${value}`}>P{value}</span> }

function Sidebar({ data, active, onChange, selectedId, onSelect, onAddProject }) {
  const activeProjects = data.projects.filter(project => project.status === 'active').sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name))
  return (
    <aside className="sidebar">
      <div className="wordmark"><span className="brand-mark"><i /><i /><i /><i /></span><div><strong>Cortex</strong><small>AI project manager</small></div></div>
      <nav aria-label="Portfolio views">
        {navItems.map(([id, label, Icon]) => <button key={id} className={active === id ? 'active' : ''} onClick={() => onChange(id)}><Icon size={17} /><span>{label}</span>{id === 'paused' && <em>{data.summary.paused_projects}</em>}</button>)}
      </nav>
      <div className="project-rail-title"><span>Active projects</span><em>{activeProjects.length}</em><button type="button" onClick={onAddProject} aria-label="Add project"><FolderPlus size={13} />Add</button></div>
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

function DecisionLane({ tasks, projects, onSelect, onViewRun }) {
  return (
    <section className="workflow-band decision-band" id="needs-decision">
      <div className="band-heading"><span className="band-icon amber"><UserRound size={18} /></span><div><h2>Needs your decision <em>{tasks.length}</em></h2><p>Only the work that cannot safely continue without you.</p></div></div>
      <div className="decision-list">
        {tasks.slice(0, 4).map(task => <article key={task.id} data-task={task.id}>
          <div><span className="project-kicker">{projects[task.project_id]?.name}</span><h3>{task.title}</h3><p>{String(task.assignee || '').toLowerCase() === 'owner' ? 'This task is assigned to you.' : task.status === 'blocked' ? 'This agent needs a decision or missing input.' : task.status === 'review' ? `Completed by ${workers[executionWorker(task.latest_run)]?.label || executionWorker(task.latest_run)} in ${durationLabel(task.latest_run)}. The captured result is ready.` : 'This manual step needs your attention.'}</p></div>
          <StatusPill value={task.status} />
          <button onClick={() => task.latest_run ? onViewRun(task.latest_run) : onSelect(task.project_id)}>{task.latest_run ? 'View result' : 'Open'} <ChevronRight size={14} /></button>
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
          <div className="working-meta"><WorkerBadge name={task.assignee || task.recommended_worker} /><span>{task.latest_run?.state === 'running' ? `working ${durationLabel(task.latest_run)}` : `${task.route.budget} effort`}</span></div>
          {task.latest_run?.state === 'running' && <><div className="indeterminate-progress"><span /></div><LiveTail runId={task.latest_run.id} /></>}
          {task.policy_note && <small className="policy-note"><CircleAlert size={12} />{task.policy_note}</small>}
          {task.status === 'assigned' && !['owner', 'perplexity'].includes(String(task.assignee || '').toLowerCase()) ? <button className="soft-action" onClick={() => onStart(task)}>Start safely <CirclePlay size={15} /></button> : <button className="text-action" onClick={() => onSelect(task.project_id)}>Open project <ChevronRight size={14} /></button>}
        </article>)}
        {!tasks.length && <div className="lane-empty"><Clock3 size={17} />No agents are assigned yet.</div>}
      </div>
    </section>
  )
}

function TeamPanel({ team, onKeepWorking, busy }) {
  const active = team.filter(member => member.state === 'working').length
  const readyToStart = team.filter(member => member.state === 'queued').length
  return <section className="workflow-band team-band" id="expert-team">
    <div className="band-heading"><span className="band-icon teal"><UsersRound size={18} /></span><div><h2>Your expert team <em>{active} working · {readyToStart} queued</em></h2><p>Give outcomes to the team; Cortex routes the work and remembers who performs well.</p></div><button className="band-action" onClick={onKeepWorking} disabled={busy}>{busy ? <LoaderCircle className="spin" size={15} /> : <CirclePlay size={15} />}Keep team moving</button></div>
    <div className="team-grid">
      {team.map(member => <article className="expert-card" key={member.name} data-state={member.state}>
        <div className="expert-head"><WorkerBadge name={member.name} /><span className={`expert-state ${member.state}`}><i />{pretty(member.state)}</span></div>
        <h3>{member.role}</h3><p>{member.best_for}</p>
        {member.current_task ? <div className="expert-assignment"><small>{member.state === 'working' ? 'Working on' : 'Next assignment'}</small><strong>{member.current_task.title}</strong><span>{member.current_task.execution_model || 'default'} · {member.current_task.execution_effort || 'auto'} effort</span></div> : <div className="expert-assignment idle"><small>Available</small><strong>{member.availability === 'ready' ? 'Ready for a suitable task' : member.probe_note || 'Setup required'}</strong></div>}
        <div className="expert-score"><span><strong>{member.success_rate == null ? '—' : `${member.success_rate}%`}</strong><small>run success</small></span><span><strong>{member.attempts}</strong><small>attempts</small></span><span><strong>{numberLabel(member.tokens)}</strong><small>tokens tracked</small></span></div>
      </article>)}
    </div>
  </section>
}

function HealthPanel({ data, onGitRefresh, busy }) {
  const git = data.summary
  const topPerformance = (data.model_performance || []).slice(0, 5)
  return <section className="workflow-band health-band" id="daily-health">
    <div className="band-heading"><span className="band-icon blue"><Gauge size={18} /></span><div><h2>Daily operational health</h2><p>Repository synchronization, measured usage, and evidence about which models work best.</p></div><button className="band-action" onClick={onGitRefresh} disabled={busy}>{busy ? <LoaderCircle className="spin" size={15} /> : <GitBranch size={15} />}Check GitHub</button></div>
    <div className="health-grid">
      <article><span className="health-label">Git repositories</span><strong>{git.git_dirty} with changes · {git.git_ahead} to push · {git.git_behind} behind</strong><p>{git.git_unchecked ? `${git.git_unchecked} active projects have not been checked yet.` : git.git_no_upstream ? `${git.git_no_upstream} fetched branches have no upstream comparison.` : 'All active projects have a stored remote comparison.'}</p></article>
      <article><span className="health-label">CLI usage captured</span><strong>{numberLabel(data.usage?.total_tokens)} tokens</strong><p>{numberLabel(data.usage?.input_tokens)} input · {numberLabel(data.usage?.output_tokens)} output · {numberLabel(data.usage?.cached_tokens)} cached</p><small>Per-run usage reported by CLIs; subscription quota is not exposed by every provider.</small></article>
      <article className="performance-card"><span className="health-label">Model scorecard</span>{topPerformance.length ? topPerformance.map(row => <div key={`${row.model}-${row.effort}-${row.task_type}`}><span>{row.model || 'unknown'} <small>{row.effort || 'auto'} · {row.task_type}</small></span><strong>{row.success_rate == null ? '—' : `${row.success_rate}%`} <small>{row.attempts} runs</small></strong></div>) : <p>Scorecards appear after Cortex records runs. Outcomes improve future routing.</p>}</article>
    </div>
  </section>
}

function SuggestionCard({ suggestion, project, onApprove, onDismiss }) {
  return (
    <article className="suggestion-card" data-suggestion={suggestion.id}>
      <div className="suggestion-top"><span className="project-kicker">{project?.name}</span><Priority value={suggestion.priority} /></div>
      <h3>{suggestion.title}</h3><p className="why">{suggestion.why}</p>
      <div className="acceptance"><Check size={14} /><span><strong>Done when</strong>{suggestion.acceptance}</span></div>
      <div className="route-strip"><span className="execution-label">Executes with</span><WorkerBadge name={suggestion.recommended_worker} /><span>{pretty(suggestion.action)}</span><span>{suggestion.budget} effort</span></div>
      <div className="card-actions"><button className="approve" onClick={() => onApprove(suggestion, true)}>Approve & start</button><button onClick={() => onApprove(suggestion, false)}>Queue</button><button className="icon-button" title="Dismiss suggestion" aria-label={`Dismiss ${suggestion.title}`} onClick={() => onDismiss(suggestion.id)}><X size={15} /></button></div>
      <small className="source">Planner: {workers[suggestion.source_worker]?.label || suggestion.source_worker} · Worker: {workers[suggestion.recommended_worker]?.label || suggestion.recommended_worker}</small>
    </article>
  )
}

function RecommendationLane({ suggestions, projects, selectedProject, onApprove, onDismiss, onPlan, planning }) {
  const canPlan = Boolean(selectedProject) && !planning
  return (
    <section className="workflow-band recommendation-band" id="recommendations">
      <div className="band-heading"><span className="band-icon violet"><WandSparkles size={18} /></span><div><h2>Recommended next moves <em>{suggestions.length}</em></h2><p>{selectedProject ? <>Planning target: <strong>{selectedProject.name}</strong> · {selectedProject.current_goal || 'goal not set'}</> : 'Select a project in the sidebar or table to ask the PM for a plan.'}</p></div><button className="band-action" onClick={onPlan} disabled={!canPlan}>{planning ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}{selectedProject ? `Plan ${selectedProject.name}` : 'Select a project to plan'}</button></div>
      <div className="recommendation-grid">
        {suggestions.slice(0, 6).map(suggestion => <SuggestionCard key={suggestion.id} suggestion={suggestion} project={projects[suggestion.project_id]} onApprove={onApprove} onDismiss={onDismiss} />)}
        {!suggestions.length && <div className="plan-empty"><WandSparkles size={22} /><div><strong>No plan is waiting.</strong><span>{selectedProject ? `Ask the PM to inspect ${selectedProject.name} and propose bounded outcomes.` : 'Choose a project first; the planning target will be shown here.'}</span></div><button onClick={onPlan} disabled={!canPlan}>{planning ? 'Planning…' : selectedProject ? `Plan ${selectedProject.name}` : 'Select a project'}</button></div>}
      </div>
    </section>
  )
}

function RunActivity({ runs, onView }) {
  return <section className="workflow-band activity-band" id="execution-activity">
    <div className="band-heading"><span className="band-icon blue"><Activity size={18} /></span><div><h2>Execution activity <em>{runs.length}</em></h2><p>Persistent evidence showing what launched, what is still running, and what finished.</p></div></div>
    <div className="run-list">
      {runs.slice(0, 8).map(run => <button key={run.id} className="run-row" onClick={() => onView(run)}>
        <span className={`run-state ${run.state}`} />
        <span className="run-copy"><strong>{run.task_title}</strong><small>{run.project_name}</small></span>
        <span className="run-worker"><WorkerBadge name={executionWorker(run)} /><small>executed by</small></span>
        <span className="run-timing"><strong>{run.state === 'running' ? `Working · ${durationLabel(run)}` : run.exit_code === 0 ? `Completed · ${durationLabel(run)}` : 'Failed'}</strong><small>{run.started_at ? new Date(run.started_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : ''}</small></span>
        <StatusPill value={run.state === 'completed' && run.task_status === 'review' ? 'review' : run.state} />
        <ChevronRight size={15} />
      </button>)}
      {!runs.length && <div className="lane-empty"><Clock3 size={17} />No executions have been recorded yet.</div>}
    </div>
  </section>
}

function timelineStatus(item) {
  if (item.kind === 'activity') {
    if (item.action?.includes('blocked') || item.action === 'run.failed') return 'failed'
    if (item.action?.includes('closed') || item.action?.includes('completed') || item.action?.includes('removed')) return 'completed'
    if (item.action?.includes('started')) return 'running'
    if (item.action?.includes('status_changed')) return 'review'
    return 'queued'
  }
  if (item.state === 'completed' && item.task_status === 'review') return 'review'
  return item.state
}

function timelineOutcome(item) {
  if (item.kind === 'activity') return item.summary
  if (item.state === 'running') return 'Execution is still in progress.'
  if (item.state === 'failed') return `Execution failed${item.exit_code == null ? '' : ` with exit code ${item.exit_code}`}.`
  if (item.outcome && item.outcome !== 'unknown') return `Outcome: ${pretty(item.outcome)}.`
  if (item.tests_passed === 1) return 'Tests passed; evidence was captured.'
  if (item.tests_passed === 0) return 'Tests failed; review the captured evidence.'
  if (item.task_status === 'review') return 'Worker output is waiting for acceptance review.'
  return 'Execution completed and evidence was captured.'
}

function TimelineEvent({ item, linkedRun, onView }) {
  const occurred = new Date(item.timestamp)
  const actor = item.kind === 'activity' ? (item.actor_name || item.actor_type || 'cortex') : executionWorker(item)
  const taskTitle = item.task_title || (item.kind === 'activity' ? 'Portfolio activity' : 'Untitled work item')
  const content = <>
    <span className="timeline-track" aria-hidden="true"><i className={`run-state ${timelineStatus(item)}`} /></span>
    <span className="timeline-time">
      <strong>{occurred.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</strong>
      <small>{item.kind === 'run' && item.ended_at ? `to ${new Date(item.ended_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : pretty(item.action || 'recorded')}</small>
    </span>
    <span className="timeline-card">
      <span className="timeline-card-heading"><span><small>{item.project_name}</small><strong>{taskTitle}</strong></span><StatusPill value={timelineStatus(item)} /></span>
      <span className="timeline-outcome">{timelineOutcome(item)}</span>
      <span className="timeline-meta"><WorkerBadge name={actor} /><span>{item.kind === 'run' ? durationLabel(item) : pretty(item.actor_type || 'system')}</span><code>{item.session_id || item.id}</code>{linkedRun && <em>View evidence <ChevronRight size={13} /></em>}</span>
    </span>
  </>
  if (linkedRun) return <button type="button" className="timeline-event" onClick={() => onView(linkedRun)}>{content}</button>
  return <article className="timeline-event timeline-event-static">{content}</article>
}

function EvidenceTimeline({ runs, activity, project, onView, onClearProject }) {
  const groups = useMemo(
    () => filterAndGroupTimeline(runs, activity, project?.project_id || null),
    [runs, activity, project?.project_id],
  )
  const runMap = useMemo(() => Object.fromEntries(runs.map(run => [run.id, run])), [runs])
  const visibleCount = groups.reduce((count, group) => count + group.events.length, 0)

  return <section className="timeline-section" aria-labelledby="evidence-timeline-title">
    <div className="timeline-heading">
      <div>
        <span className="eyebrow">Who did what</span>
        <h1 id="evidence-timeline-title">{project ? `${project.name} evidence timeline` : 'Portfolio evidence timeline'}</h1>
        <p>{project ? `Attributed work and execution history linked to ${project.name}.` : 'A chronological record of PM sessions, decisions, assignments, agent work, and review evidence across every project.'}</p>
      </div>
      <div className="timeline-heading-actions">
        <span><strong>{visibleCount}</strong> recorded event{visibleCount === 1 ? '' : 's'}</span>
        {project && <button type="button" onClick={onClearProject}>View all projects</button>}
      </div>
    </div>
    <div className="timeline-container">
      {groups.map(group => <section className="timeline-day" key={group.key} aria-labelledby={`timeline-day-${group.key}`}>
        <div className="timeline-day-heading">
          <h2 id={`timeline-day-${group.key}`}>{group.label}</h2>
          <span>{group.events.length} event{group.events.length === 1 ? '' : 's'}</span>
        </div>
        <div className="timeline-events">
          {group.events.map(item => <TimelineEvent key={`${item.kind}-${item.id}`} item={item} linkedRun={runMap[item.run_id]} onView={onView} />)}
        </div>
      </section>)}
      {!groups.length && <div className="timeline-empty"><Clock3 size={20} /><strong>No activity history found.</strong><span>{project ? `Cortex has not recorded work for ${project.name}.` : 'PM sessions, decisions, assignments, and agent runs will appear here automatically.'}</span></div>}
    </div>
  </section>
}

function RunModal({ run, onClose }) {
  const live = useRunOutput(run?.id, run?.state === 'running')
  const bodyRef = useRef(null)
  const [follow, setFollow] = useState(true)
  const liveText = live.text
  useEffect(() => {
    if (follow && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight
  }, [liveText, follow])
  if (!run) return null
  const running = run.state === 'running'
  // Prefer the live transcript whenever there is one: for a finished run it is
  // the full session, while `response` is only the worker's final payload.
  const shown = liveText || run.response || run.human_note || (running
    ? 'The worker has started. Output will appear here as it arrives.'
    : 'No output was captured for this run.')
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><section className="run-modal">
    <div className="modal-head"><div><span className="eyebrow">{running ? 'Live execution' : 'Execution evidence'}</span><h2>{run.task_title}</h2><p>{run.project_name}</p></div><button className="icon-button" onClick={onClose}><X size={18} /></button></div>
    <div className="run-facts"><span><small>Status</small><StatusPill value={run.state === 'completed' && run.task_status === 'review' ? 'review' : run.state} /></span><span><small>Executed by</small><WorkerBadge name={executionWorker(run)} /></span><span><small>Duration</small><strong>{durationLabel(run)}</strong></span><span><small>Exit code</small><strong>{run.exit_code ?? 'running'}</strong></span></div>
    <div className="run-dates"><span>Started {run.started_at ? new Date(run.started_at).toLocaleString() : '—'}</span><span>{run.ended_at ? `Finished ${new Date(run.ended_at).toLocaleString()}` : 'Still running'}</span><code>{run.id}</code></div>
    <div className="run-response">
      <label>{running ? <><span className="live-dot" />Live worker output</> : 'Captured worker output'}
        {running && <button className={`follow-toggle ${follow ? 'on' : ''}`} onClick={() => setFollow(value => !value)}>{follow ? 'Following' : 'Paused'}</button>}
      </label>
      <pre ref={bodyRef} onScroll={event => {
        const el = event.currentTarget
        // Scrolling up pauses auto-follow so reading earlier output is possible.
        setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
      }}>{shown}</pre>
    </div>
  </section></div>
}

function ProjectsTable({ projects, selectedId, onSelect, onPlan }) {
  return <section className="projects-section"><div className="section-title"><div><span className="eyebrow">Portfolio evidence</span><h2>Project workstreams</h2></div><span>{projects.length} visible</span></div><div className="project-table-wrap"><table><thead><tr><th>Project</th><th>Priority</th><th>Goal</th><th>Queue</th><th>Repository</th><th>Activity</th><th /></tr></thead><tbody>
    {projects.map(project => <tr key={project.project_id} className={selectedId === project.project_id ? 'selected' : ''} onClick={() => onSelect(project.project_id)}><td><span className="project-cell"><span className="health-dot" data-health={project.health} />{project.name}</span><small>{project.program}</small></td><td><Priority value={project.priority} /></td><td>{project.current_goal || 'Goal not set'}</td><td><strong>{project.task_count}</strong><small>{project.suggestion_count} suggestions</small></td><td><span className="repo-state"><GitBranch size={13} />{project.branch || 'No Git'}</span><small>{gitLabel(project)}</small></td><td>{relativeDate(project.git_check?.checked_at || project.updated_at)}</td><td><button className="table-plan" onClick={event => { event.stopPropagation(); onPlan(project.project_id) }}>Plan <Sparkles size={13} /></button></td></tr>)}
  </tbody></table></div></section>
}

const ALLOWLIST_CHOICES = ['codex', 'claude', 'gemini', 'grok', 'ollama', 'perplexity']

function WorkerAllowlist({ project, onChange }) {
  const allowed = new Set(project.allowed_workers || [])
  function toggle(name) {
    const next = new Set(allowed)
    if (next.has(name)) next.delete(name); else next.add(name)
    if (!next.size) return   // a project must keep at least one worker
    onChange(project.project_id, [...next])
  }
  return <section className="allowlist">
    <div className="drawer-section-head"><label>Workers allowed to read this repo</label>{!project.allowlist_configured && <small className="default-tag">default</small>}</div>
    <div className="allowlist-grid">
      {ALLOWLIST_CHOICES.map(name => <button key={name} type="button" className={`allow-chip ${allowed.has(name) ? 'on' : ''}`} onClick={() => toggle(name)} aria-pressed={allowed.has(name)}>
        {allowed.has(name) ? <Check size={12} /> : <X size={12} />}{workers[name]?.label || name}{name === 'ollama' && <em>local</em>}
      </button>)}
    </div>
    <small className="allowlist-hint">Only these workers may see <code>{project.repo_path}</code>. Cortex routes around the rest instead of blocking the task.</small>
  </section>
}

function DrawerTask({ task, onTaskUpdate, onStart, onCodex, onGithub }) {
  return <div className="drawer-task"><div><strong>{task.title}</strong><span><StatusPill value={task.status} /><WorkerBadge name={task.execution_worker} /></span><small>{task.execution_model || 'default model'} · {task.execution_effort || 'auto'} effort</small></div>
    <div className="task-safe-actions"><button type="button" className="task-codex-action" onClick={() => onCodex(task)}><Copy size={14} />{task.codex_thread_id ? 'Copy continuation prompt' : 'Copy Codex prompt'}</button><button type="button" className="task-github-action" onClick={() => onGithub(task)}><Github size={14} />Check GitHub mirror</button></div>
    <div>{task.status === 'assigned' && !['owner', 'perplexity'].includes(String(task.assignee || '').toLowerCase()) && <button onClick={() => onStart(task)}><CirclePlay size={14} />Start</button>}<select aria-label="Model" value={task.requested_model || ''} onChange={event => onTaskUpdate(task.id, { requested_model: event.target.value || null })}><option value="">Auto model</option><option value="grok-4.5">grok-4.5</option><option value="sonnet">Claude Sonnet</option><option value="opus">Claude Opus</option></select><select aria-label="Effort" value={task.effort || ''} onChange={event => onTaskUpdate(task.id, { effort: event.target.value || null })}><option value="">Auto effort</option>{['low', 'medium', 'high', 'xhigh'].map(value => <option key={value}>{value}</option>)}</select><select aria-label="Status" value={task.status} onChange={event => onTaskUpdate(task.id, { status: event.target.value })}>{statuses.map(status => <option key={status} value={status}>{pretty(status)}</option>)}</select></div>
  </div>
}

function ProjectDrawer({ project, onClose, onPlan, onTaskUpdate, onStart, onManual, onAllowlist, onTimeline, onCodex, onGithub, onRemove }) {
  if (!project) return null
  return <aside className="drawer"><div className="drawer-head"><div><span className="project-monogram large" style={{ '--project-color': projectColor(project) }}>{project.name.slice(0, 2).toUpperCase()}</span><span><small>{project.program}</small><h2>{project.name}</h2></span></div><button className="icon-button" onClick={onClose}><X size={18} /></button></div>
    <div className="drawer-primary"><button onClick={() => onPlan(project.project_id, 'codex')}><Sparkles size={16} />Ask Codex to plan next</button><button onClick={() => onPlan(project.project_id, 'ollama')}><Cpu size={16} />Use local planner</button></div>
    <section><label>Current goal</label><p>{project.current_goal || 'No goal has been set.'}</p></section>
    <section className="drawer-evidence"><label>Repository evidence</label><div><GitBranch size={14} />{project.branch || 'Not under Git'}<span>{gitLabel(project)}</span></div>{project.warnings?.[0] && <small><AlertTriangle size={13} />{project.warnings[0]}</small>}<button type="button" className="drawer-timeline-action" onClick={() => onTimeline(project.project_id)}><Clock3 size={14} />View project timeline</button></section>
    <WorkerAllowlist project={project} onChange={onAllowlist} />
    <section><div className="drawer-section-head"><label>Active work ({project.tasks.length})</label><button onClick={() => onManual(project.project_id)}><Plus size={13} />Manual</button></div><div className="drawer-task-list">{project.tasks.map(task => <DrawerTask key={task.id} task={task} onTaskUpdate={onTaskUpdate} onStart={onStart} onCodex={onCodex} onGithub={onGithub} />)}{!project.tasks.length && <div className="lane-empty">No active tasks.</div>}</div></section>
    <div className="drawer-path"><FolderGit2 size={14} /><span title={project.repo_path}>{project.repo_path}</span></div>
    <button type="button" className="drawer-remove-project" onClick={() => onRemove(project)}><Trash2 size={13} />Remove from Cortex</button>
  </aside>
}

function CodexLaunchModal({ launch, onClose, onCopied }) {
  const [copied, setCopied] = useState(false)
  if (!launch) return null
  const capability = launch.payload?.capability
  async function copyPrompt() {
    await navigator.clipboard.writeText(launch.payload.prompt)
    setCopied(true)
    onCopied()
  }
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><section className="codex-launch-modal" aria-labelledby="codex-launch-title">
    <div className="modal-head"><div><span className="eyebrow">Safe handoff preview</span><h2 id="codex-launch-title">{launch.task.codex_thread_id ? 'Continue Codex task' : 'Task Codex next'}</h2><p>{launch.task.title}</p></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div>
    {launch.loading ? <div className="codex-launch-loading"><LoaderCircle className="spin" size={18} />Checking the local Codex bridge and building the bounded prompt…</div> : <>
      <div className={`codex-capability ${capability?.available ? 'ready' : 'fallback'}`}><span><Bot size={16} /><strong>{capability?.available ? 'Local Codex capability detected' : 'Copy-prompt fallback active'}</strong></span><small>{capability?.available ? `${capability.threads.length} existing Codex task${capability.threads.length === 1 ? '' : 's'} found for this exact repository path.` : capability?.reason || 'The local App Server could not be inspected.'}</small>{launch.payload.codex_thread_id && <code>{launch.payload.codex_thread_id}{capability?.linked_thread_found ? ' · linked task found' : ' · linked task not currently listed'}</code>}</div>
      <label className="codex-prompt-label">Bounded Codex prompt<textarea aria-label="Codex launch prompt" readOnly rows="16" value={launch.payload.prompt} /></label>
      <div className="codex-launch-note"><AlertTriangle size={15} /><span>This copy-only preview does not create or resume a Codex task, start a model turn, spend tokens, or navigate Codex Desktop.</span></div>
      <div className="modal-actions"><button type="button" onClick={onClose}>Close</button><button type="button" className="primary" onClick={copyPrompt}><Copy size={14} />{copied ? 'Copied' : launch.task.codex_thread_id ? 'Copy continuation prompt' : 'Copy Codex prompt'}</button></div>
    </>}
  </section></div>
}

function GitHubMirrorModal({ launch, onClose, onCopied }) {
  const [copied, setCopied] = useState(false)
  if (!launch) return null
  const payload = launch.payload
  const meta = mirrorStatusMeta(payload?.status)
  const plan = payload?.plan
  const operation = payload?.operation
  async function copyCommand() {
    await navigator.clipboard.writeText(payload.command)
    setCopied(true)
    onCopied(payload.command_kind)
  }
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><section className="github-mirror-modal" aria-labelledby="github-mirror-title">
    <div className="modal-head"><div><span className="eyebrow">On-demand GitHub check</span><h2 id="github-mirror-title">GitHub mirror readiness</h2><p>{launch.task.title}</p></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div>
    {launch.loading ? <div className="github-mirror-loading" role="status"><LoaderCircle className="spin" size={18} />Reading every Project, item, field, and value page…</div> : <>
      <div className={`github-mirror-status ${meta.tone}`}><span>{meta.tone === 'ready' ? <CheckCircle2 size={17} /> : meta.tone === 'danger' ? <CircleAlert size={17} /> : <ShieldCheck size={17} />}<strong>{meta.label}</strong></span><small>{payload.summary}</small></div>
      <div className="github-mirror-facts"><span><small>Target</small><strong>{payload.target ? `${payload.target.title || payload.target.owner} · Project #${payload.target.number}` : 'Not configured'}</strong>{payload.target?.project_id && <code>{payload.target.project_id}</code>}</span><span><small>Linked issue</small><strong>{payload.issue?.number ? `#${payload.issue.number}${payload.issue.title ? ` · ${payload.issue.title}` : ''}` : 'Not linked'}</strong>{payload.issue?.url && <a href={payload.issue.url} target="_blank" rel="noreferrer">Open issue</a>}</span><span><small>Project item</small><strong>{plan?.project_item_id || operation?.github_project_item_id || 'Not recorded'}</strong></span></div>
      {plan && <div className="github-plan-evidence"><div><span><small>Fresh plan fingerprint</small><code>{plan.fingerprint}</code></span><span><small>Snapshot digest</small><code>{plan.snapshot_digest}</code></span></div>{plan.actions.length > 0 && <section><h3>Planned actions ({plan.actions.length})</h3><ul>{plan.actions.map((action, index) => <li key={`${action.kind}-${action.field || index}`}><strong>{mirrorActionLabel(action)}</strong><span>{action.reason}</span></li>)}</ul></section>}{plan.conflicts.length > 0 && <section className="github-conflicts"><h3>Conflicts ({plan.conflicts.length})</h3><ul>{plan.conflicts.map(conflict => <li key={conflict.code}><strong>{pretty(conflict.code)}</strong><span>{conflict.summary}</span></li>)}</ul></section>}</div>}
      {operation && <div className={`github-operation ${['applying', 'interrupted'].includes(operation.status) ? 'recoverable' : ''}`}><span><small>Latest durable operation</small><strong>{operation.operation_id} · {pretty(operation.status)}</strong></span><span><small>Verified steps</small><strong>{operation.completed_actions?.length || 0} / {operation.actions?.length || 0}</strong></span>{operation.error && <p>{operation.error}</p>}{payload.read_error && <p>Fresh read: {payload.read_error}</p>}</div>}
      {payload.command && <label className="github-command-label">Exact {payload.command_kind === 'resume' ? 'recovery' : 'approved apply'} command<textarea aria-label="GitHub mirror CLI command" readOnly rows="3" value={payload.command} /></label>}
      <div className="codex-launch-note"><AlertTriangle size={15} /><span>{payload.note} Review the fingerprint and actions before running any copied command in PowerShell.</span></div>
      <div className="modal-actions"><button type="button" onClick={onClose}>Close</button>{payload.command && <button type="button" className="primary" onClick={copyCommand}><Copy size={14} />{copied ? 'Copied' : payload.command_kind === 'resume' ? 'Copy recovery command' : 'Copy apply command'}</button>}</div>
    </>}
  </section></div>
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
  const [selectedRun, setSelectedRun] = useState(null)
  const [timelineProjectId, setTimelineProjectId] = useState(null)
  const [codexLaunch, setCodexLaunch] = useState(null)
  const [githubLaunch, setGithubLaunch] = useState(null)
  const [showAddProject, setShowAddProject] = useState(false)
  const [removalProject, setRemovalProject] = useState(null)
  const pollRef = useRef(null)

  const previewProjectRemoval = useCallback(async projectId => {
    const payload = await request(`/api/projects/${projectId}/removal-preview`, {
      method: 'POST',
      body: '{}',
      headers: { 'X-Cortex-Action-Token': data?.action_token || '' },
    })
    return payload.preview
  }, [data?.action_token])

  async function load(silent = false) { if (!silent) setError(''); try { const [portfolio, jobPayload] = await Promise.all([request('/api/portfolio'), request('/api/jobs')]); setData({ ...portfolio, jobs: jobPayload.jobs }) } catch (err) { setError(err.message) } }
  useEffect(() => { load(); return () => clearInterval(pollRef.current) }, [])
  useEffect(() => { if (!toast) return undefined; const timer = setTimeout(() => setToast(''), 3500); return () => clearTimeout(timer) }, [toast])

  const projectMap = useMemo(() => Object.fromEntries((data?.projects || []).map(project => [project.project_id, project])), [data])
  const filteredProjects = useMemo(() => {
    if (!data) return []
    let rows = data.projects.filter(project => activeNav === 'paused' ? project.status === 'paused' : project.status === 'active')
    if (search.trim()) { const term = search.toLowerCase(); rows = rows.filter(project => `${project.name} ${project.current_goal || ''}`.toLowerCase().includes(term)) }
    return rows.sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name))
  }, [data, activeNav, search])
  const visibleIds = new Set(filteredProjects.map(project => project.project_id))
  const visibleTasks = (data?.tasks || []).filter(task => visibleIds.has(task.project_id))
  const decisions = visibleTasks.filter(task => ['review', 'blocked'].includes(task.status) || ['owner', 'perplexity'].includes(String(task.assignee || '').toLowerCase()))
  const working = visibleTasks.filter(task => ['assigned', 'in_progress', 'running'].includes(task.status) && !['owner', 'perplexity'].includes(String(task.assignee || '').toLowerCase()))
  const suggestions = (data?.suggestions || []).filter(suggestion => visibleIds.has(suggestion.project_id))
  const visibleRuns = (data?.runs || []).filter(run => visibleIds.has(run.project_id))
  const serverRunningJobs = (data?.jobs || []).filter(item => item.status === 'running')
  const selected = projectMap[selectedId] || null
  const timelineProject = projectMap[timelineProjectId] || null

  useEffect(() => {
    if (!serverRunningJobs.length || job) return undefined
    const timer = setInterval(() => load(true), 2000)
    return () => clearInterval(timer)
  }, [serverRunningJobs.length, Boolean(job)])

  function watchJob(jobId, label) {
    clearInterval(pollRef.current); setJob({ id: jobId, label, status: 'running' })
    pollRef.current = setInterval(async () => {
      try {
        const payload = await request(`/api/jobs/${jobId}`)
        if (payload.job.status === 'running') { await load(true); return }
        clearInterval(pollRef.current); setJob(null)
        if (payload.job.status === 'failed') throw new Error(payload.job.error)
        setToast(label === 'Planning' ? 'AI plan is ready for approval' : label === 'Checking GitHub' ? 'Repository checks are current' : label === 'Starting team' ? 'Safe queued assignments were started' : 'Agent run finished; review the result')
        await load(true)
        const targetId = label === 'Planning' ? 'recommendations' : label === 'Checking GitHub' ? 'daily-health' : label === 'Starting team' ? 'expert-team' : 'needs-decision'
        setTimeout(() => document.getElementById(targetId)?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50)
      } catch (err) { clearInterval(pollRef.current); setJob(null); setError(err.message) }
    }, 1500)
  }

  async function planProject(projectId = selectedId, workerOverride = planner, force = false) {
    const target = projectId
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
  async function saveTaskSchedule(id, fields) {
    try {
      await request(`/api/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(fields) })
      setToast('Local schedule updated')
      await load(true)
      return true
    } catch (err) {
      setError(err.message)
      return false
    }
  }
  async function previewProject(repoPath) {
    const payload = await request('/api/projects/preview', {
      method: 'POST',
      body: JSON.stringify({ repo_path: repoPath }),
      headers: { 'X-Cortex-Action-Token': data.action_token },
    })
    return payload.preview
  }
  async function registerProject(fields) {
    const payload = await request('/api/projects', {
      method: 'POST',
      body: JSON.stringify(fields),
      headers: { 'X-Cortex-Action-Token': data.action_token },
    })
    await load(true)
    setSelectedId(payload.project.id)
    setActiveNav('projects')
    setToast(`${payload.project.name} added to Cortex`)
    return payload
  }
  async function removeProject(projectId, confirmation) {
    const projectName = projectMap[projectId]?.name || 'Project'
    const payload = await request(`/api/projects/${projectId}`, {
      method: 'DELETE',
      body: JSON.stringify(confirmation),
      headers: { 'X-Cortex-Action-Token': data.action_token },
    })
    setSelectedId(null)
    if (timelineProjectId === projectId) setTimelineProjectId(null)
    await load(true)
    setToast(`${projectName} removed from Cortex; repository left untouched`)
    return payload
  }
  async function updateAllowlist(projectId, allowed) { try { await request(`/api/projects/${projectId}`, { method: 'PATCH', body: JSON.stringify({ allowed_workers: allowed }) }); setToast(`Allowlist updated: ${allowed.join(', ')}`); await load(true) } catch (err) { setError(err.message) } }
  async function dismissSuggestion(id) { try { await request(`/api/suggestions/${id}`, { method: 'PATCH', body: JSON.stringify({ status: 'dismissed' }) }); setToast('Suggestion dismissed'); await load(true) } catch (err) { setError(err.message) } }
  async function keepTeamWorking() { try { const payload = await request('/api/team/keep-working', { method: 'POST', body: JSON.stringify({ limit: 3 }) }); if (!payload.job_ids.length) { setToast('No safe approved assignments are waiting; ask the PM to plan a project'); await load(true); return } watchJob(payload.job_ids[0], 'Starting team') } catch (err) { setError(err.message) } }
  async function refreshGit() { try { const payload = await request('/api/git/refresh', { method: 'POST', body: JSON.stringify({ fetch: true }) }); watchJob(payload.job_id, 'Checking GitHub') } catch (err) { setError(err.message) } }
  async function previewCodex(task) {
    setCodexLaunch({ task, loading: true, payload: null })
    try {
      const payload = await request(`/api/tasks/${task.id}/codex/preview`, { method: 'POST', body: '{}', headers: { 'X-Cortex-Action-Token': data.action_token } })
      setCodexLaunch({ task, loading: false, payload })
      await load(true)
    } catch (err) {
      setCodexLaunch(null)
      setError(err.message)
    }
  }

  async function previewGithub(task) {
    setGithubLaunch({ task, loading: true, payload: null })
    try {
      const payload = await request(`/api/tasks/${task.id}/github/preview`, { method: 'POST', body: '{}', headers: { 'X-Cortex-Action-Token': data.action_token } })
      setGithubLaunch({ task, loading: false, payload })
    } catch (err) {
      setGithubLaunch(null)
      setError(err.message)
    }
  }

  function changeNav(next) {
    if (next === 'timeline') setTimelineProjectId(null)
    if (next === 'timeline' || next === 'roadmap') setSelectedId(null)
    setActiveNav(next)
  }

  function openProjectTimeline(projectId) {
    setTimelineProjectId(projectId)
    setSelectedId(null)
    setActiveNav('timeline')
  }

  if (!data && !error) return <div className="app-loading"><LoaderCircle className="spin" size={22} />Loading your portfolio…</div>
  if (!data) return <div className="app-loading error"><CircleAlert size={24} /><strong>Dashboard unavailable</strong><span>{error}</span><button onClick={() => load()}><RotateCcw size={15} />Retry</button></div>

  return <div className="app-shell">
    <Sidebar data={data} active={activeNav} onChange={changeNav} selectedId={selectedId} onSelect={setSelectedId} onAddProject={() => setShowAddProject(true)} />
    <div className={`content-shell ${selected ? 'drawer-open' : ''}`}><main>
      <header className="topbar"><div><span className="eyebrow">Local AI control plane</span><strong>Cortex Portfolio</strong></div><div><label className="search"><Search size={15} /><input aria-label="Search projects" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search projects" /></label><button className="icon-button" onClick={() => load()} aria-label="Refresh"><RefreshCw size={16} /></button></div></header>
      {error && <div className="error-banner"><CircleAlert size={15} /><span>{error}</span><button onClick={() => setError('')}><X size={14} /></button></div>}
      {(job || serverRunningJobs.length > 0) && <div className="job-banner"><LoaderCircle className="spin" size={16} /><span><strong>{job?.label || pretty(serverRunningJobs[0]?.kind || 'Worker')}</strong> is active. Execution activity below shows the worker and elapsed time.</span></div>}
      <div className="main-content">
        {activeNav === 'timeline' ? <EvidenceTimeline runs={data.runs || []} activity={data.activity || []} project={timelineProject} onView={setSelectedRun} onClearProject={() => setTimelineProjectId(null)} /> : activeNav === 'roadmap' ? <RoadmapView tasks={data.roadmap_tasks || []} projects={data.projects || []} onSaveTask={saveTaskSchedule} /> : <>
          <Hero planner={planner} setPlanner={setPlanner} onContinue={continueWork} busy={Boolean(job) || serverRunningJobs.length > 0} onManual={() => setManualProject(null)} />
          <div className="summary-line"><span><strong>{data.summary.needs_decision}</strong> need you</span><span><strong>{data.summary.working}</strong> assigned / working</span><span><strong>{data.summary.recommendations}</strong> ready to approve</span><span><strong>{data.summary.active_projects}</strong> active projects</span><em>Plans are cached to conserve tokens</em></div>
          <TeamPanel team={data.team || []} onKeepWorking={keepTeamWorking} busy={Boolean(job) || serverRunningJobs.length > 0} />
          <DecisionLane tasks={decisions} projects={projectMap} onSelect={setSelectedId} onViewRun={setSelectedRun} />
          <WorkingLane tasks={working} projects={projectMap} onStart={startTask} onSelect={setSelectedId} />
          <RunActivity runs={visibleRuns} onView={setSelectedRun} />
          <RecommendationLane suggestions={suggestions} projects={projectMap} selectedProject={selected} onApprove={approveSuggestion} onDismiss={dismissSuggestion} onPlan={() => planProject(selectedId)} planning={job?.label === 'Planning' || serverRunningJobs.some(item => item.kind === 'plan')} />
          <HealthPanel data={data} onGitRefresh={refreshGit} busy={job?.label === 'Checking GitHub' || serverRunningJobs.some(item => item.kind === 'git')} />
          <ProjectsTable projects={filteredProjects} selectedId={selectedId} onSelect={setSelectedId} onPlan={id => planProject(id)} />
        </>}
        <footer><span>SQLite source of truth</span><span>{data.database}</span><span>No automatic merges or external actions</span></footer>
      </div>
    </main><ProjectDrawer project={selected} onClose={() => setSelectedId(null)} onPlan={planProject} onTaskUpdate={updateTask} onStart={startTask} onManual={id => setManualProject(id)} onAllowlist={updateAllowlist} onTimeline={openProjectTimeline} onCodex={previewCodex} onGithub={previewGithub} onRemove={setRemovalProject} /></div>
    {manualProject !== undefined && <ManualTaskModal projects={data.projects} initialProject={manualProject || ''} onClose={() => setManualProject(undefined)} onCreated={async () => { setManualProject(undefined); setToast('Manual task added'); await load(true) }} />}
    <RunModal run={selectedRun} onClose={() => setSelectedRun(null)} />
    <CodexLaunchModal launch={codexLaunch} onClose={() => setCodexLaunch(null)} onCopied={() => setToast('Bounded Codex prompt copied')} />
    <GitHubMirrorModal launch={githubLaunch} onClose={() => setGithubLaunch(null)} onCopied={kind => setToast(kind === 'resume' ? 'Recovery command copied; review before running' : 'Approved apply command copied; review before running')} />
    {showAddProject && <AddProjectModal onClose={() => setShowAddProject(false)} onPreview={previewProject} onRegister={registerProject} />}
    {removalProject && <RemoveProjectModal project={removalProject} onClose={() => setRemovalProject(null)} onPreview={previewProjectRemoval} onRemove={removeProject} />}
    {toast && <div className="toast"><Check size={15} />{toast}</div>}
  </div>
}
