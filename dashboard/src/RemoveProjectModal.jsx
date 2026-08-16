import { useEffect, useState } from 'react'
import {
  AlertTriangle, FileCheck2, FolderGit2, Github, LoaderCircle,
  ShieldCheck, Trash2, X,
} from 'lucide-react'

function Count({ label, value }) {
  return <span><strong>{value}</strong><small>{label}</small></span>
}

export default function RemoveProjectModal({ project, onClose, onPreview, onRemove }) {
  const [preview, setPreview] = useState(null)
  const [confirmName, setConfirmName] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    setError('')
    onPreview(project.project_id)
      .then(result => { if (active) setPreview(result) })
      .catch(err => { if (active) setError(err.message) })
    return () => { active = false }
  }, [project.project_id, onPreview])

  async function remove() {
    setBusy(true); setError('')
    try {
      await onRemove(project.project_id, {
        confirm_name: confirmName,
        acknowledge_permanent: acknowledged,
      })
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const counts = preview?.deleted_counts || {}
  const canRemove = preview && !preview.blocked && acknowledged
    && confirmName === preview.project_name && !busy

  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><section className="remove-project-modal" aria-labelledby="remove-project-title">
    <div className="modal-head"><div><span className="eyebrow">Local portfolio change</span><h2 id="remove-project-title">Remove {project.name} from Cortex?</h2><p>This permanently deletes its local Cortex history. Your project files and external services stay untouched.</p></div><button type="button" className="icon-button" onClick={onClose} aria-label="Close remove project"><X size={18} /></button></div>

    {!preview && !error && <div className="project-removal-loading"><LoaderCircle className="spin" size={18} />Counting local Cortex records…</div>}

    {preview && <>
      <div className="project-removal-warning"><AlertTriangle size={18} /><span><strong>This cannot be undone inside Cortex.</strong><small>{preview.deleted_total} local record{preview.deleted_total === 1 ? '' : 's'} will be deleted, while a removal audit entry will remain in the portfolio timeline.</small></span></div>
      <div className="project-removal-counts">
        <Count label="tasks" value={counts.tasks || 0} />
        <Count label="executions" value={counts.runs || 0} />
        <Count label="activity events" value={counts.activity_events || 0} />
        <Count label="other local records" value={Math.max(0, preview.deleted_total - (counts.tasks || 0) - (counts.runs || 0) - (counts.activity_events || 0))} />
      </div>
      <section className="project-removal-preserved"><div><ShieldCheck size={16} /><span><strong>These stay exactly where they are</strong><small>Cortex does not delete or modify any of them.</small></span></div><ul><li><FolderGit2 size={14} /><span>Repository</span><code>{preview.preserved.repository}</code></li><li><FileCheck2 size={14} /><span>State file</span><code>{preview.preserved.state_exists ? preview.preserved.state_path : 'No state file exists'}</code></li><li><Github size={14} /><span>External resources</span><small>GitHub Projects/issues, branches, and linked Codex tasks</small></li></ul></section>
      {preview.blocked && <div className="project-removal-blocked"><AlertTriangle size={15} /><span><strong>Removal is blocked.</strong>{preview.blockers.map(blocker => <small key={blocker}>{blocker}</small>)}</span></div>}
      {!preview.blocked && <div className="project-removal-confirm"><label>Type <strong>{preview.project_name}</strong> to confirm<input autoFocus autoComplete="off" spellCheck="false" aria-label="Type project name to confirm" value={confirmName} onChange={event => setConfirmName(event.target.value)} /></label><label className="project-removal-ack"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} /><span>I understand this permanently removes the project’s local Cortex history.</span></label></div>}
    </>}

    {error && <div className="project-form-error">{error}</div>}
    <div className="modal-actions"><button type="button" onClick={onClose}>Cancel</button><button type="button" className="danger-action" onClick={remove} disabled={!canRemove}>{busy ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}{busy ? 'Removing…' : 'Remove from Cortex'}</button></div>
  </section></div>
}
