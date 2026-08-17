import { useEffect, useRef, useState } from 'react'
import { Check, CheckCircle2, CircleAlert, Clock3, FileCheck2, LoaderCircle, Play, X } from 'lucide-react'

function EvidencePointer({ item }) {
  if (item.kind === 'run') {
    return <div className="phase-evidence-pointer"><Clock3 size={13} /><span><strong>Run {item.id}</strong><small>{item.model || 'default model'} · {item.outcome || 'unknown outcome'} · tests {item.tests_passed === 1 ? 'passed' : item.tests_passed === 0 ? 'failed' : 'not recorded'}</small></span></div>
  }
  return <div className="phase-evidence-pointer"><FileCheck2 size={13} /><span><strong>{item.title}</strong><small>Task {item.id} · {item.status}</small></span></div>
}

export default function PhaseEvidenceModal({ project, onClose, onLoad, onAccept, onMoveReview, onMoveComplete }) {
  const [overview, setOverview] = useState(null)
  const [busyKey, setBusyKey] = useState('')
  const [error, setError] = useState('')
  const loadRef = useRef(onLoad)
  loadRef.current = onLoad

  async function refresh() {
    const result = await loadRef.current(project.project_id)
    setOverview(result)
    return result
  }

  useEffect(() => {
    let cancelled = false
    setOverview(null)
    setError('')
    loadRef.current(project.project_id)
      .then(result => { if (!cancelled) setOverview(result) })
      .catch(err => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [project.project_id])

  async function accept(criterion) {
    setBusyKey(criterion.exit_criterion_ref)
    setError('')
    try {
      await onAccept(project.project_id, criterion.exit_criterion_ref, criterion.preview_fingerprint)
      await refresh()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyKey('')
    }
  }

  async function moveToReview() {
    setBusyKey('review')
    setError('')
    try {
      await onMoveReview(project.project_id, overview.transition.preview_fingerprint)
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyKey('')
    }
  }

  async function moveToComplete() {
    setBusyKey('complete')
    setError('')
    try {
      await onMoveComplete(project.project_id, overview.transition.preview_fingerprint)
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyKey('')
    }
  }

  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
    <section className="phase-evidence-modal" aria-labelledby="phase-evidence-title">
      <div className="modal-head"><div><span className="eyebrow">Evidence-based phase gate</span><h2 id="phase-evidence-title">Review exit evidence</h2><p>{project.name}</p></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div>
      {error && <div className="phase-decomposition-error"><CircleAlert size={15} /><span>{error}</span></div>}
      {!overview && !error ? <div className="blueprint-loading"><LoaderCircle className="spin" size={18} />Assembling recorded task and run evidence…</div> : overview && <>
        <div className="phase-evidence-progress"><div><small>Active phase</small><strong>{overview.phase.name}</strong><span>{overview.progress.accepted} of {overview.progress.total} exit criteria accepted by the owner</span></div><em>{overview.progress.percent}%</em><i><span style={{ width: `${overview.progress.percent}%` }} /></i><small>Progress basis: {overview.progress.basis}. Task percentages and model estimates do not count.</small></div>
        <div className="phase-evidence-list">
          {overview.criteria.map((criterion, index) => <article key={criterion.exit_criterion_ref} className={criterion.accepted ? 'accepted' : ''}>
            <div className="phase-evidence-head"><span>{criterion.accepted ? <CheckCircle2 size={15} /> : index + 1}</span><div><small>{criterion.exit_criterion_ref}</small><strong>{criterion.criterion_text}</strong></div><em>{criterion.accepted ? 'Accepted' : criterion.can_accept ? 'Ready for owner' : 'Not ready'}</em></div>
            {criterion.evidence.length > 0 && <div className="phase-evidence-pointers">{criterion.evidence.map(item => <EvidencePointer key={`${item.kind}-${item.id}`} item={item} />)}</div>}
            {criterion.blockers.length > 0 && <div className="phase-evidence-blockers">{criterion.blockers.map(blocker => <span key={blocker}><CircleAlert size={12} />{blocker}</span>)}</div>}
            {criterion.accepted ? <small className="phase-evidence-accepted"><Check size={12} />Accepted by {criterion.approving_actor} · {criterion.accepted_at}</small> : <button type="button" onClick={() => accept(criterion)} disabled={!criterion.can_accept || busyKey !== ''}>{busyKey === criterion.exit_criterion_ref ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}Accept recorded evidence</button>}
          </article>)}
        </div>
        <div className={`phase-review-gate ${overview.transition.can_transition ? 'ready' : ''}`}>
          <div><small>{overview.transition.to_status === 'complete' ? 'Next phase state' : 'Next phase state'}</small><strong>{overview.transition.can_transition ? `Ready to move from ${overview.transition.from_status} to ${overview.transition.to_status}` : 'Transition remains locked'}</strong>{overview.transition.blockers.map(blocker => <span key={blocker}>{blocker}</span>)}</div>{overview.transition.can_transition && <code>{overview.transition.preview_fingerprint}</code>}
          {overview.transition.next_phase && <small><em>Next phase:</em> {overview.transition.next_phase.name} (#{overview.transition.next_phase.ordinal})</small>}
          {overview.transition.next_phase && !overview.transition.writes?.activate_next_phase && <small className="phase-evidence-blockers"><CircleAlert size={12} />Next phase dependencies are still blocking activation.</small>}
        </div>
        <div className="modal-actions">
          <button type="button" onClick={onClose}>Close</button>
          {overview.transition.to_status === 'review'
            ? <button type="button" className="primary" onClick={moveToReview} disabled={!overview.transition.can_transition || busyKey !== ''}>{busyKey === 'review' ? <LoaderCircle className="spin" size={15} /> : <CheckCircle2 size={15} />}Move phase to review</button>
            : <button type="button" className="primary" onClick={moveToComplete} disabled={!overview.transition.can_transition || busyKey !== ''}>{busyKey === 'complete' ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />}Mark phase complete</button>
          }
        </div>
      </>}
    </section>
  </div>
}
