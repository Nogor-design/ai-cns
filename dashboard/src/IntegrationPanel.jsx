import { useCallback, useEffect, useState } from 'react'
import { CircleCheck, CircleHelp, GitMerge, LoaderCircle, RotateCcw, ShieldCheck, TriangleAlert } from 'lucide-react'
import { blockingChecks, branchLabel, canRevert, checkLabel, gateSummary, passedCount, reviewerLabel, statusLabel, statusTone } from './integration.js'

// What the verification gate has decided, and where each project's integration
// branch stands. Nothing here starts work: it reports what already happened and
// lets the owner undo one merge.
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

function StatusIcon({ status }) {
  if (status === 'merged') return <CircleCheck size={14} />
  if (status === 'needs_owner') return <CircleHelp size={14} />
  if (status === 'verified') return <ShieldCheck size={14} />
  return <TriangleAlert size={14} />
}

function VerificationRow({ item, busy, onRevert }) {
  const [open, setOpen] = useState(false)
  const blockers = blockingChecks(item)
  return <li className={`gate-row gate-${statusTone(item.status)}`}>
    <button type="button" className="gate-head" onClick={() => setOpen(value => !value)}>
      <StatusIcon status={item.status} />
      <span className="gate-status">{statusLabel(item.status)}</span>
      <span className="gate-task">{item.task_title || item.task_id}</span>
      <span className="gate-project">{item.project_name}</span>
      <span className="gate-when">{(item.created_at || '').slice(0, 16).replace('T', ' ')}</span>
    </button>
    <p className="gate-summary">{gateSummary(item)}</p>
    {open && <div className="gate-detail">
      <ul className="gate-checks">
        {(item.checks || []).map((check, index) => <li key={index} className={`check-${check.status}`}>
          <span>{checkLabel(check.name)}</span>
          <em>{check.detail}</em>
        </li>)}
      </ul>
      {item.reverted_at && <small className="gate-reverted">Reverted {item.reverted_at.slice(0, 16).replace('T', ' ')}</small>}
    </div>}
    <div className="gate-actions">
      <small>{passedCount(item)} passed{blockers.length ? ` · ${blockers.length} blocking` : ''}</small>
      {canRevert(item) && <button type="button" disabled={Boolean(busy)} onClick={() => onRevert(item)}>
        {busy === item.id ? <LoaderCircle className="spin" size={13} /> : <RotateCcw size={13} />}
        Undo this merge
      </button>}
    </div>
  </li>
}

export default function IntegrationPanel({ token, onError }) {
  const [payload, setPayload] = useState(null)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      setPayload(await call('/api/integration'))
    } catch (error) {
      onError?.(error.message)
    }
  }, [onError])

  useEffect(() => { load() }, [load])

  async function revert(item) {
    setBusy(item.id)
    try {
      setPayload(await call(`/api/verifications/${item.id}/revert`, { token, method: 'POST', body: {} }))
    } catch (error) {
      onError?.(error.message)
    } finally {
      setBusy('')
    }
  }

  async function setReview(required) {
    setBusy('review')
    try {
      setPayload(await call('/api/integration/settings', {
        token, method: 'POST', body: { review_required: required },
      }))
    } catch (error) {
      onError?.(error.message)
    } finally {
      setBusy('')
    }
  }

  if (!payload) return null
  const mergeable = (payload.projects || []).filter(project => project.merge_allowed)
  const verifications = payload.verifications || []

  return <section className="workflow-band integration-band" id="integration">
    <div className="band-heading">
      <span className="band-icon teal"><GitMerge size={18} /></span>
      <div>
        <h2>Verified work <em>{mergeable.length ? `${mergeable.length} auto-merging` : 'owner merges'}</em></h2>
        <p>
          Agent writes land on <code>cortex/integration</code> and nowhere else: committed,
          checked for scope, protected files, secrets and tests, merged <code>--no-ff</code>,
          re-tested merged, then judged by a model that did not write them. Nothing is pushed
          and conflicts stay yours.
        </p>
      </div>
      <button className="band-action" disabled={Boolean(busy)}
        onClick={() => setReview(!payload.review_required)}>
        <ShieldCheck size={15} />{payload.review_required ? 'Second model: required' : 'Second model: off'}
      </button>
    </div>

    <div className="integration-body">
      <p className="integration-reviewer">{reviewerLabel(payload)}</p>

      {mergeable.length === 0
        ? <p className="integration-empty">
            No project is in integration mode yet, so every merge still waits for you. The gate
            still runs on write work and records what it found.
          </p>
        : <ul className="integration-branches">
            {mergeable.map(project => <li key={project.project_id}>
              <strong>{project.name}</strong>
              <span>{branchLabel(project)}</span>
            </li>)}
          </ul>}

      {verifications.length === 0
        ? <p className="integration-empty">No write run has been judged yet.</p>
        : <ul className="gate-list">
            {verifications.map(item => <VerificationRow key={item.id} item={item}
              busy={busy === item.id ? item.id : ''} onRevert={revert} />)}
          </ul>}
    </div>
  </section>
}
