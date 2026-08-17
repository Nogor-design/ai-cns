import { useState } from 'react'
import { Check, GitMerge, LoaderCircle, ShieldCheck, X } from 'lucide-react'

import {
  dependencyCandidates,
  editableDependencyPhases,
  selectedDependencyOrdinals,
  toggleDependencyOrdinal,
} from './phaseDependencies.js'

export default function PhaseDependencyModal({ project, onClose, onSave }) {
  const phases = project?.blueprint?.phases || []
  const editable = editableDependencyPhases(phases)
  const [phaseId, setPhaseId] = useState(editable[0]?.id || '')
  const phase = editable.find(item => item.id === phaseId) || editable[0]
  const candidates = dependencyCandidates(phases, phase)
  const [selected, setSelected] = useState(() => selectedDependencyOrdinals(phase))
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  function choosePhase(nextPhaseId) {
    const nextPhase = editable.find(item => item.id === nextPhaseId)
    setPhaseId(nextPhaseId)
    setSelected(selectedDependencyOrdinals(nextPhase))
    setError('')
  }

  async function save() {
    if (!phase || saving) return
    setSaving(true)
    setError('')
    try {
      await onSave(project.project_id, phase.id, selected)
      onClose()
    } catch (err) {
      setError(err.message)
      setSaving(false)
    }
  }

  if (!project || !phase) return null
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}>
    <section className="phase-dependency-modal" aria-labelledby="phase-dependency-title">
      <div className="modal-head"><div><span className="eyebrow">Approved phase plan</span><h2 id="phase-dependency-title">Manage phase dependencies</h2><p>{project.name}</p></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div>
      <div className="phase-dependency-note"><ShieldCheck size={16} /><span>This replaces the selected phase's blockers in the approved revision. It does not rewrite the blueprint, create tasks, or start agents.</span></div>
      <label className="phase-dependency-select"><small>Phase to update</small><select value={phase.id} onChange={event => choosePhase(event.target.value)}>{editable.map(item => <option key={item.id} value={item.id}>Phase {item.ordinal} · {item.name}</option>)}</select></label>
      <div className="phase-dependency-target"><small>Dependency edges for phase {phase.ordinal}</small><strong><GitMerge size={15} />{phase.name}</strong><span>{phase.outcome}</span></div>
      <fieldset className="phase-dependency-options"><legend>Must wait for</legend>
        {candidates.map(candidate => {
          const checked = selected.includes(Number(candidate.ordinal))
          return <label key={candidate.id} className={checked ? 'selected' : ''}><input type="checkbox" checked={checked} onChange={() => setSelected(current => toggleDependencyOrdinal(current, candidate.ordinal))} /><span>{checked ? <Check size={14} /> : candidate.ordinal}</span><div><strong>Phase {candidate.ordinal} · {candidate.name}</strong><small>{candidate.status} · {candidate.outcome}</small></div></label>
        })}
      </fieldset>
      <small className="phase-dependency-summary">Saving will atomically replace this phase's dependencies with: {selected.length ? selected.map(ordinal => `phase ${ordinal}`).join(', ') : 'no blockers'}.</small>
      {error && <div className="phase-dependency-error">{error}</div>}
      <div className="modal-actions"><button type="button" onClick={onClose} disabled={saving}>Cancel</button><button type="button" className="primary" onClick={save} disabled={saving}>{saving ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}Save dependencies</button></div>
    </section>
  </div>
}
