import { useCallback, useEffect, useState } from 'react'
import { Check, Cpu, Inbox, LoaderCircle, PauseCircle, CirclePlay, RefreshCw, ShieldCheck, X } from 'lucide-react'
import { INBOX_KIND_LABELS, LANE_LABELS, clampReserve, resetLabel, schedulerState, sortModels, windowLabel, windowTone } from './capacity.js'

const PROVIDER_LABELS = { codex: 'Codex', claude: 'Claude', opencode: 'OpenCode Go', grok: 'Grok', gemini: 'Gemini', agy: 'Antigravity' }
const MODE_LABELS = { off: 'Off', read_only: 'Read-only', integration: 'Integration' }

async function call(path, { token, body, method = 'GET' } = {}) {
  const response = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Cortex-Action-Token': token } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`)
  return payload
}

function QuotaRow({ row, reserve }) {
  const ceiling = row.ceiling_pct
  const windows = row.windows.filter(window => window.window !== 'refusal' || window.limited)
  return <div className="quota-row" data-allowed={row.allowed}>
    <div className="quota-head">
      <strong>{PROVIDER_LABELS[row.provider] || row.provider}</strong>
      <span className={`quota-state ${row.allowed ? 'ok' : 'held'}`}>{row.allowed ? 'Can work' : 'Held'}</span>
    </div>
    {row.kind === 'metered' && windows.length > 0 ? windows.map(window => {
      const used = window.used_percent
      const tone = windowTone(used, ceiling)
      return <div className="quota-window" key={window.window}>
        <span>{windowLabel(window.window)}</span>
        <div className="quota-bar" role="img" aria-label={`${windowLabel(window.window)} ${used ?? 'unknown'}% used; reserve starts at ${100 - reserve}%`}>
          <i className={tone} style={{ width: `${Math.min(100, used ?? 0)}%` }} />
          <b style={{ left: `${100 - reserve}%` }} title={`Your ${reserve}% reserve starts here`} />
        </div>
        <em>{used == null ? '?' : `${Math.round(used)}%`}</em>
        <small>{window.estimated ? 'reset since last reading' : [window.detail, resetLabel(window.resets_at)].filter(Boolean).join(' · ')}</small>
      </div>
    }) : <p className="quota-note">{row.kind === 'counted' ? 'No usage signal from this CLI; limited by run count.' : 'No reading yet.'}</p>}
    <p className="quota-reason">{row.reason}</p>
  </div>
}

function AutopilotStrip({ pilot, busy, onSave, onInbox }) {
  const state = schedulerState(pilot)
  const box = pilot.inbox
  const holds = Object.entries(pilot.holds || {})
  return <div className="autopilot-strip">
    <article className="autopilot-state">
      <span className="health-label">Autopilot</span>
      <strong className={`pilot-${state.tone}`}>{state.label}</strong>
      <p>{pilot.running
        ? `Holder ${pilot.lease.holder} · checks every ${pilot.interval_seconds}s`
        : 'Start it with scripts/install-autopilot-task.ps1 -Install, or cortex autopilot run.'}</p>
      <label className="pilot-slots">
        <span>Runs at once</span>
        <select value={pilot.max_concurrent} disabled={Boolean(busy)} onChange={event => onSave({ max_concurrent: Number(event.target.value) }, 'slots')}>
          {[1, 2, 3, 4, 5, 6].map(value => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
      {pilot.in_flight.map(run => <p className="pilot-run" key={run.id}><b>{(run.model || '').split(':')[0] || 'worker'}</b> {run.project_name}: {run.title}</p>)}
      {holds.map(([worker, reason]) => <p className="pilot-hold" key={worker} title={reason}>
        {worker} on hold <button type="button" onClick={() => onSave({ release_hold: worker }, 'hold')} disabled={Boolean(busy)}>Release</button>
      </p>)}
    </article>
    <article className="autopilot-inbox">
      <span className="health-label"><Inbox size={12} /> Needs you · {box.open} open{box.queued ? ` · ${box.queued} queued` : ''}</span>
      <label className="pilot-cap">
        <span>Daily limit</span>
        <input type="number" min="1" max="50" defaultValue={box.daily_cap} key={box.daily_cap}
          onBlur={event => { const value = Number(event.currentTarget.value); if (value && value !== box.daily_cap) onSave({ inbox_daily_cap: value }, 'cap') }}
          aria-label="Inbox items per day" />
        <small>{box.surfaced_today} asked today</small>
      </label>
      {box.items.length === 0 && <p className="quota-note">Nothing waiting on you.</p>}
      <ul className="inbox-list">
        {box.items.map(item => <li key={item.id} data-status={item.status}>
          <div>
            <em>{INBOX_KIND_LABELS[item.kind] || item.kind}{item.status === 'queued' ? ' · queued' : ''}</em>
            <strong>{item.title}</strong>
            {item.detail && <small>{item.detail}</small>}
          </div>
          <span className="inbox-actions">
            <button type="button" title="Done" aria-label="Mark done" onClick={() => onInbox(item.id, 'resolve')} disabled={Boolean(busy)}><Check size={13} /></button>
            <button type="button" title="Dismiss" aria-label="Dismiss" onClick={() => onInbox(item.id, 'dismiss')} disabled={Boolean(busy)}><X size={13} /></button>
          </span>
        </li>)}
      </ul>
    </article>
  </div>
}

export default function CapacityPanel({ token, onError }) {
  const [payload, setPayload] = useState(null)
  const [reserve, setReserve] = useState(30)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      const next = await call('/api/capacity')
      setPayload(next)
      setReserve(next.quota.reserve_pct)
    } catch (err) { onError?.(err.message) }
  }, [onError])

  useEffect(() => {
    load()
    const timer = setInterval(load, 20_000)
    return () => clearInterval(timer)
  }, [load])

  async function save(body, label) {
    setBusy(label)
    try {
      const next = await call('/api/capacity/settings', { method: 'POST', token, body })
      setPayload(next)
      setReserve(next.quota.reserve_pct)
    } catch (err) { onError?.(err.message) } finally { setBusy('') }
  }

  async function closeInboxItem(itemId, action) {
    setBusy(itemId)
    try {
      await call(`/api/inbox/${itemId}/${action}`, { method: 'POST', token, body: {} })
      await load()
    } catch (err) { onError?.(err.message) } finally { setBusy('') }
  }

  async function refresh() {
    setBusy('refresh')
    try {
      const next = await call('/api/capacity/refresh', { method: 'POST', token, body: { probe: true } })
      setPayload(next)
    } catch (err) { onError?.(err.message) } finally { setBusy('') }
  }

  async function setMode(projectId, mode) {
    setBusy(projectId)
    try {
      await call(`/api/projects/${projectId}`, { method: 'PATCH', body: { autonomy_mode: mode } })
      await load()
    } catch (err) { onError?.(err.message) } finally { setBusy('') }
  }

  if (!payload) return <section className="workflow-band capacity-band" id="capacity"><div className="band-heading"><span className="band-icon teal"><Cpu size={18} /></span><div><h2>Capacity &amp; autonomy</h2><p>Loading quota and local compute…</p></div></div></section>

  const { quota, lanes, autonomy, autopilot } = payload
  const paused = autonomy.paused
  const hw = lanes.hardware || {}
  const models = sortModels(lanes.models)
  const protectedProjects = autonomy.projects.filter(project => project.protected)
  const openProjects = autonomy.projects.filter(project => !project.protected)

  return <section className="workflow-band capacity-band" id="capacity">
    <div className="band-heading">
      <span className="band-icon teal"><Cpu size={18} /></span>
      <div><h2>Capacity &amp; autonomy <em>{paused ? 'paused' : 'active'}</em></h2><p>Unattended work only uses quota above your reserve, one local model at a time.</p></div>
      <button className="band-action" onClick={refresh} disabled={Boolean(busy)}>{busy === 'refresh' ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}Refresh quota</button>
      <button className={`band-action ${paused ? 'resume' : 'pause'}`} onClick={() => save({ paused: !paused }, 'pause')} disabled={Boolean(busy)}>{paused ? <CirclePlay size={15} /> : <PauseCircle size={15} />}{paused ? 'Resume autonomy' : 'Pause autonomy'}</button>
    </div>
    <div className="capacity-grid">
      <article className="capacity-quota">
        <span className="health-label">Subscription quota</span>
        <label className="reserve-control">
          <span>Keep for me <strong>{reserve}%</strong></span>
          <input type="range" min="0" max="95" step="5" value={reserve}
            onChange={event => setReserve(clampReserve(event.target.value))}
            onPointerUp={event => save({ reserve_pct: clampReserve(event.currentTarget.value) }, 'reserve')}
            onKeyUp={event => save({ reserve_pct: clampReserve(event.currentTarget.value) }, 'reserve')}
            aria-label="Quota reserve percent" />
        </label>
        {quota.providers.map(row => <QuotaRow key={row.provider} row={row} reserve={quota.reserve_pct} />)}
        <label className="go-limit">
          <span>OpenCode Go monthly limit ($)</span>
          <input type="number" min="1" max="10000" step="1" defaultValue={quota.opencode_go_monthly_usd}
            key={quota.opencode_go_monthly_usd}
            onBlur={event => { const value = Number(event.currentTarget.value); if (value && value !== quota.opencode_go_monthly_usd) save({ opencode_go_monthly_usd: value }, 'go') }}
            aria-label="OpenCode Go monthly dollar limit" />
          <small>From your OpenCode console; 5 hours = 20%, week = 50%.</small>
        </label>
      </article>
      <article className="capacity-local">
        <span className="health-label">Local compute</span>
        <strong>{hw.gpu_name || 'No GPU detected'}</strong>
        <p>{hw.vram_used_mb ?? '?'} / {hw.vram_mb ?? '?'} MiB GPU · {Math.round((hw.ram_mb || 0) / 1024)} GB RAM · slot {lanes.slot_busy ? 'busy' : 'free'}</p>
        {lanes.ninjatrader_running && <p className="lane-warning">NinjaTrader is running: only models that fit the GPU start unattended.</p>}
        {(lanes.loaded || []).map(model => <p key={model.name} className="lane-loaded">Loaded: {model.name} · {model.size_gb} GB · {model.gpu_percent}% GPU</p>)}
        <ul className="lane-list">
          {models.map(model => <li key={model.name} data-lane={model.lane}>
            <span>{model.name}</span><small>{model.size_gb} GB{model.output_tps ? ` · ${model.output_tps} tok/s` : ''}</small><em>{LANE_LABELS[model.lane] || model.lane}</em>
          </li>)}
        </ul>
      </article>
      <article className="capacity-autonomy">
        <span className="health-label">Unattended work by project</span>
        {openProjects.map(project => <div className="autonomy-row" key={project.project_id}>
          <span>{project.name}</span>
          <select value={project.mode} disabled={busy === project.project_id} onChange={event => setMode(project.project_id, event.target.value)}>
            {autonomy.modes.map(mode => <option key={mode} value={mode} disabled={mode === 'integration'}>{MODE_LABELS[mode]}{mode === 'integration' ? ' (Phase 3)' : ''}</option>)}
          </select>
        </div>)}
        {protectedProjects.length > 0 && <div className="protected-list">
          <small><ShieldCheck size={12} /> Always excluded</small>
          {protectedProjects.map(project => <p key={project.project_id} title={project.protected_detail}><strong>{project.name}</strong> · {project.protected === 'apollo' ? 'production (Apollo)' : 'trading'}</p>)}
        </div>}
      </article>
    </div>
    {autopilot && <AutopilotStrip pilot={autopilot} busy={busy} onSave={save} onInbox={closeInboxItem} />}
  </section>
}
