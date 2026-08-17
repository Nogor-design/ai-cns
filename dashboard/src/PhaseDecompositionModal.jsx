import { useEffect, useRef, useState } from 'react'
import { Check, CircleAlert, LoaderCircle, Sparkles, X } from 'lucide-react'

export default function PhaseDecompositionModal({ project, onClose, onPreview, onApprove }) {
  const [preview, setPreview] = useState(null)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const previewRef = useRef(onPreview)
  previewRef.current = onPreview

  useEffect(() => {
    let cancelled = false
    setPreview(null)
    setError('')
    previewRef.current(project.project_id)
      .then(result => { if (!cancelled) setPreview(result) })
      .catch(err => { if (!cancelled) setError(err.message) })
    return () => { cancelled = true }
  }, [project.project_id])

  async function approve() {
    if (!preview?.suggestions?.length) return
    setSaving(true)
    setError('')
    try {
      await onApprove(project.project_id, preview.preview_fingerprint)
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
    <section className="phase-decomposition-modal" aria-labelledby="phase-decomposition-title">
      <div className="modal-head"><div><span className="eyebrow">Owner-reviewed phase planning</span><h2 id="phase-decomposition-title">Break down current phase</h2><p>{project.name}</p></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div>
      {error && <div className="phase-decomposition-error"><CircleAlert size={15} /><span>{error}</span></div>}
      {!preview && !error ? <div className="blueprint-loading"><LoaderCircle className="spin" size={18} />Building a deterministic, no-token preview…</div> : preview && <>
        <div className="phase-decomposition-context"><small>Active phase</small><strong>{preview.phase.name}</strong><span>{preview.phase.outcome}</span></div>
        <div className="phase-decomposition-guard"><Check size={15} /><span><strong>Preview only.</strong> Approving creates ordinary Cortex suggestions. It does not create tasks, start agents, contact a provider, or change phase status.</span></div>
        <div className="phase-decomposition-list">
          {preview.suggestions.map((suggestion, index) => <article key={suggestion.exit_criterion_ref}>
            <span>{index + 1}</span><div><small>{suggestion.exit_criterion_ref} · {suggestion.type}</small><strong>{suggestion.title}</strong><p>{suggestion.why}</p><div><em>Done when</em>{suggestion.acceptance}</div></div>
          </article>)}
          {!preview.suggestions.length && <div className="phase-decomposition-empty"><Check size={16} /><span><strong>Every exit criterion already has linked work.</strong><small>Dismissed suggestions can be previewed again; active or converted work remains linked.</small></span></div>}
        </div>
        <div className="blueprint-fingerprint"><small>Exact approval fingerprint</small><code>{preview.preview_fingerprint}</code></div>
        <div className="modal-actions"><button type="button" onClick={onClose}>Close</button><button type="button" className="primary" onClick={approve} disabled={saving || !preview.suggestions.length}>{saving ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}Approve {preview.suggestions.length} suggestion{preview.suggestions.length === 1 ? '' : 's'}</button></div>
      </>}
    </section>
  </div>
}
