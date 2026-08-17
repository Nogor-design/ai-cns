import { useEffect, useRef, useState } from 'react'
import {
  ArrowLeft, ArrowRight, BookOpen, Check, FileSearch, LoaderCircle,
  ShieldCheck, X,
} from 'lucide-react'

function InterviewRail({ step }) {
  const active = step === 'questions' ? 1 : step === 'phase' ? 2 : 3
  return <div className="project-step-rail blueprint-interview-rail" aria-label={`Step ${active} of 3`}>
    {['Product intent', 'Current phase', 'Approve'].map((label, index) => <span key={label} className={active >= index + 1 ? 'active' : ''}><i>{active > index + 1 ? <Check size={10} /> : index + 1}</i>{label}</span>)}
  </div>
}

export default function BlueprintOnboardingModal({ project, onClose, onStart, onSave, onPreview, onApprove }) {
  const [step, setStep] = useState('questions')
  const [draft, setDraft] = useState(null)
  const [answers, setAnswers] = useState({})
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const loadHandlers = useRef({ onStart, onPreview })

  useEffect(() => {
    loadHandlers.current = { onStart, onPreview }
  }, [onStart, onPreview])

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const loaded = await loadHandlers.current.onStart(project.project_id)
        if (cancelled) return
        setDraft(loaded)
        setAnswers(loaded.answers || {})
        if (loaded.stage === 'review') {
          const prepared = loaded.preview || await loadHandlers.current.onPreview(project.project_id)
          if (!cancelled) { setPreview(prepared); setStep('review') }
        } else if (loaded.stage === 'planning') setStep('phase')
      } catch (err) { if (!cancelled) setError(err.message) } finally { if (!cancelled) setBusy(false) }
    }
    load()
    return () => { cancelled = true }
    // Project identity is the lifecycle boundary. Request handlers stay fresh
    // through the ref without restarting discovery after portfolio refreshes.
  }, [project.project_id])

  function update(key, value) { setAnswers(current => ({ ...current, [key]: value })) }
  function complete(keys) { return keys.every(key => String(answers[key] || '').trim()) }

  async function saveQuestions(event) {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const saved = await onSave(project.project_id, answers, 'planning')
      setDraft(saved); setStep('phase')
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function saveAndClose() {
    setBusy(true); setError('')
    try { await onSave(project.project_id, answers, step === 'questions' ? 'questions' : 'planning'); onClose() }
    catch (err) { setError(err.message); setBusy(false) }
  }

  async function buildPreview(event) {
    event.preventDefault(); setBusy(true); setError('')
    try {
      await onSave(project.project_id, answers, 'planning')
      const prepared = await onPreview(project.project_id)
      setPreview(prepared); setStep('review')
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  async function approve() {
    setBusy(true); setError('')
    try { await onApprove(project.project_id, preview); onClose() }
    catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  const questionKeys = (draft?.questions || []).map(question => question.key)
  const phaseKeys = (draft?.phase_questions || []).map(question => question.key)
  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><section className="blueprint-onboarding-modal" aria-labelledby="blueprint-onboarding-title">
    <div className="modal-head"><div><span className="eyebrow">Guided project design</span><h2 id="blueprint-onboarding-title">Build {project.name}'s blueprint</h2><p>Cortex asks only for intent it cannot discover from the repository.</p></div><button type="button" className="icon-button" onClick={onClose}><X size={18} /></button></div>
    <InterviewRail step={step} />
    {busy && !draft ? <div className="blueprint-loading"><LoaderCircle className="spin" size={18} />Inspecting bounded local evidence…</div> : <>
      {draft && <div className="blueprint-discovery-note"><FileSearch size={15} /><span><strong>Local discovery complete</strong>{draft.discovery.stack || 'Stack not detected'} · {draft.discovery.bounded_characters.toLocaleString()} bounded characters · <code>{draft.discovery_hash.slice(0, 10)}</code></span></div>}
      {step === 'questions' && draft && <form className="blueprint-question-form" onSubmit={saveQuestions}>
        {draft.questions.map(question => <label key={question.key}>{question.label}<textarea rows="3" value={answers[question.key] || ''} onChange={event => update(question.key, event.target.value)} required /><small>{question.help}</small></label>)}
        <div className="blueprint-local-guard"><ShieldCheck size={15} /><span>Answers are saved only in local Cortex. No AI provider is contacted and no blueprint file is written yet.</span></div>
        {error && <div className="project-form-error">{error}</div>}
        <div className="modal-actions"><button type="button" onClick={saveAndClose} disabled={busy}>Save and close</button><button className="primary" disabled={busy || !complete(questionKeys)}>{busy ? <LoaderCircle className="spin" size={14} /> : <ArrowRight size={14} />}Plan current phase</button></div>
      </form>}
      {step === 'phase' && draft && <form className="blueprint-question-form" onSubmit={buildPreview}>
        <div className="project-step-heading"><BookOpen size={20} /><div><h3>Define only the phase that is ready now</h3><p>Future work stays outcome-level until this phase produces accepted evidence.</p></div></div>
        {draft.phase_questions.map(question => <label key={question.key}>{question.label}<textarea rows={question.key === 'phase_exit_criteria' ? 4 : 2} value={answers[question.key] || ''} onChange={event => update(question.key, event.target.value)} required /></label>)}
        {error && <div className="project-form-error">{error}</div>}
        <div className="modal-actions"><button type="button" onClick={() => setStep('questions')}><ArrowLeft size={14} />Back</button><button className="primary" disabled={busy || !complete(phaseKeys)}>{busy ? <LoaderCircle className="spin" size={14} /> : <ArrowRight size={14} />}Review exact blueprint</button></div>
      </form>}
      {step === 'review' && preview && <div className="blueprint-review-step">
        <div className="blueprint-review-facts"><span><small>File action</small><strong>{preview.writes.create_file ? 'Create blueprint.md' : 'Revise managed blueprint'}</strong></span><span><small>Execution tasks</small><strong>None generated</strong></span><span><small>Provider contact</small><strong>None</strong></span></div>
        <label>Exact Markdown approval preview<textarea readOnly rows="16" value={preview.markdown} /></label>
        <div className="blueprint-phase-preview">{preview.phases.map(phase => <span key={phase.ordinal}><i>{phase.ordinal}</i><span><strong>{phase.name}</strong><small>{phase.outcome}</small></span><em>{phase.status}</em></span>)}</div>
        <div className="blueprint-fingerprint"><small>Preview fingerprint</small><code>{preview.preview_fingerprint}</code></div>
        <div className="blueprint-local-guard"><ShieldCheck size={15} /><span>Approval writes this exact Markdown, records an immutable revision, activates the first phase, and closes the onboarding PM session. It does not commit or dispatch work.</span></div>
        {error && <div className="project-form-error">{error}</div>}
        <div className="modal-actions"><button type="button" onClick={() => setStep('phase')}><ArrowLeft size={14} />Revise</button><button type="button" className="primary" onClick={approve} disabled={busy}>{busy ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}{busy ? 'Approving…' : 'Approve blueprint'}</button></div>
      </div>}
    </>}
  </section></div>
}
