import { useState } from 'react'
import {
  ArrowLeft, ArrowRight, Check, Cpu, FileCheck2, FolderGit2, FolderOpen, FolderPlus,
  LoaderCircle, ShieldCheck, X,
} from 'lucide-react'

const WORKERS = ['codex', 'claude', 'gemini', 'grok', 'ollama', 'perplexity']
const CLOUD_WORKERS = new Set(['codex', 'claude', 'gemini', 'grok', 'perplexity'])

function workerDefaults(privacy) {
  return privacy === 'restricted' ? ['ollama'] : [...WORKERS]
}

function StepRail({ step }) {
  const active = step === 'path' ? 1 : step === 'details' ? 2 : 3
  return <div className="project-step-rail" aria-label={`Step ${active} of 3`}>
    {['Choose folder', 'Project settings', 'Review'].map((label, index) => <span key={label} className={active >= index + 1 ? 'active' : ''}><i>{active > index + 1 ? <Check size={10} /> : index + 1}</i>{label}</span>)}
  </div>
}

function Fact({ icon: Icon, label, value, tone = '' }) {
  return <span className={`project-preview-fact ${tone}`}><Icon size={16} /><small>{label}</small><strong>{value}</strong></span>
}

export default function AddProjectModal({ onClose, onBrowse, onPreview, onRegister }) {
  const [step, setStep] = useState('path')
  const [repoPath, setRepoPath] = useState('')
  const [preview, setPreview] = useState(null)
  const [form, setForm] = useState(null)
  const [busy, setBusy] = useState(false)
  const [browsing, setBrowsing] = useState(false)
  const [error, setError] = useState('')

  async function browse() {
    setBrowsing(true); setError('')
    try {
      const result = await onBrowse(repoPath)
      if (result.selected) {
        setRepoPath(result.repo_path)
        setPreview(null)
      }
    } catch (err) { setError(err.message) } finally { setBrowsing(false) }
  }

  async function inspect(event) {
    event.preventDefault()
    setBusy(true); setError('')
    try {
      const result = await onPreview(repoPath)
      setPreview(result)
      setForm({
        repo_path: result.repo_path,
        name: result.suggested_name,
        program: 'general',
        priority: 3,
        privacy: result.default_privacy,
        stack: result.stack || '',
        current_goal: '',
        test_command: result.test_command || '',
        allowed_workers: result.default_workers,
        track_state: true,
        guided_blueprint: true,
      })
      if (!result.already_registered) setStep('details')
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  function updatePrivacy(privacy) {
    setForm(current => ({ ...current, privacy, allowed_workers: workerDefaults(privacy) }))
  }

  function toggleWorker(worker) {
    setForm(current => {
      const selected = new Set(current.allowed_workers)
      if (selected.has(worker)) selected.delete(worker); else selected.add(worker)
      return { ...current, allowed_workers: WORKERS.filter(name => selected.has(name)) }
    })
  }

  function continueToReview(event) {
    event.preventDefault()
    setError('')
    if (!form.name.trim()) { setError('Project name is required.'); return }
    if (!form.current_goal.trim()) { setError('Add one current goal so Cortex knows what success means.'); return }
    if (!form.allowed_workers.length) { setError('Allow at least one AI worker.'); return }
    setStep('review')
  }

  async function register() {
    setBusy(true); setError('')
    try { await onRegister(form); onClose() } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return <div className="modal-backdrop" onMouseDown={event => event.target === event.currentTarget && onClose()}><section className="add-project-modal" aria-labelledby="add-project-title">
    <div className="modal-head"><div><span className="eyebrow">Local portfolio setup</span><h2 id="add-project-title">Add a project</h2><p>Validate the folder, review what Cortex detected, then register it explicitly.</p></div><button type="button" className="icon-button" onClick={onClose} aria-label="Close add project"><X size={18} /></button></div>
    <StepRail step={step} />

    {step === 'path' && <form className="project-path-step" onSubmit={inspect}>
      <div className="project-step-heading"><FolderPlus size={20} /><div><h3>Which project should Cortex track?</h3><p>Choose a folder or enter its full local path. Cortex previews metadata without changing the project.</p></div></div>
      <label>Project folder<div className="project-path-input"><input autoFocus value={repoPath} onChange={event => setRepoPath(event.target.value)} placeholder="D:\\trader-dan" required /><button type="button" onClick={browse} disabled={busy || browsing} aria-label="Browse for project folder">{browsing ? <LoaderCircle className="spin" size={15} /> : <FolderOpen size={15} />}{browsing ? 'Opening…' : 'Browse'}</button></div></label>
      <div className="project-local-note"><ShieldCheck size={15} /><span>This stays on your computer. Preview does not contact GitHub, start AI, or write a state file.</span></div>
      {preview?.already_registered && <div className="project-form-error">This folder is already registered as <strong>{preview.existing_project_id}</strong>.</div>}
      {error && <div className="project-form-error">{error}</div>}
      <div className="modal-actions"><button type="button" onClick={onClose}>Cancel</button><button className="primary" disabled={busy || browsing || !repoPath.trim()}>{busy ? <LoaderCircle className="spin" size={14} /> : <ArrowRight size={14} />}{busy ? 'Checking…' : 'Preview project'}</button></div>
    </form>}

    {step === 'details' && form && <form className="project-details-step" onSubmit={continueToReview}>
      <div className="project-preview-grid">
        <Fact icon={FolderGit2} label="Folder" value={preview.repo_path} tone={preview.is_git ? 'good' : ''} />
        <Fact icon={Cpu} label="Detected stack" value={preview.stack || 'No stack detected'} />
        <Fact icon={FileCheck2} label="Test command" value={preview.test_command || 'Not detected'} />
      </div>
      {!preview.is_git && <div className="project-guidance">This folder is not currently a Git repository. Cortex can still track it, but Git synchronization evidence will remain unavailable.</div>}
      {preview.suggested_id_exists && <div className="project-guidance warning">That suggested project ID already exists. Choose a distinct project name before continuing.</div>}
      <div className="project-form-grid">
        <label>Project name<input value={form.name} onChange={event => setForm({ ...form, name: event.target.value })} required /></label>
        <label>Program / lane<input value={form.program} onChange={event => setForm({ ...form, program: event.target.value })} placeholder="monetization" /></label>
        <label>Priority<select value={form.priority} onChange={event => setForm({ ...form, priority: Number(event.target.value) })}>{[1,2,3,4,5].map(value => <option key={value} value={value}>P{value}{value === 1 ? ' · highest' : value === 5 ? ' · lowest' : ''}</option>)}</select></label>
        <label>Privacy<select value={form.privacy} onChange={event => updatePrivacy(event.target.value)}><option value="public">Public</option><option value="internal">Internal</option><option value="restricted">Restricted · local by default</option></select></label>
        <label className="project-wide-field">Current goal<textarea rows="2" value={form.current_goal} onChange={event => setForm({ ...form, current_goal: event.target.value })} placeholder="One concrete outcome Cortex should move forward" required /></label>
        <label>Stack<input value={form.stack} onChange={event => setForm({ ...form, stack: event.target.value })} placeholder="Languages, frameworks, services" /></label>
        <label>Test command<input value={form.test_command} onChange={event => setForm({ ...form, test_command: event.target.value })} placeholder="npm test" /></label>
      </div>
      <fieldset className="project-worker-picker"><legend>AI workers allowed to read this project</legend><p>{form.privacy === 'restricted' ? 'Restricted starts local-only. Select a cloud worker only if you intend to permit repository access.' : 'You can tighten this allowlist later from the project drawer.'}</p><div>{WORKERS.map(worker => <label key={worker} className={form.allowed_workers.includes(worker) ? 'selected' : ''}><input type="checkbox" checked={form.allowed_workers.includes(worker)} onChange={() => toggleWorker(worker)} /><span>{worker}</span>{CLOUD_WORKERS.has(worker) ? <small>cloud</small> : <small>local</small>}</label>)}</div></fieldset>
      <label className="project-state-choice"><input type="checkbox" checked={form.track_state} onChange={event => setForm({ ...form, track_state: event.target.checked })} /><span><strong>Track project state in the repository</strong><small>{preview.state_exists ? `Preserve the existing ${preview.state_path}` : `Create ${preview.state_path}`}</small></span></label>
      <label className="project-state-choice"><input type="checkbox" checked={form.guided_blueprint} onChange={event => setForm({ ...form, guided_blueprint: event.target.checked })} /><span><strong>Build the project blueprint next</strong><small>Recommended: answer five product-intent questions, define the current phase, and approve an exact preview before guided execution.</small></span></label>
      {error && <div className="project-form-error">{error}</div>}
      <div className="modal-actions"><button type="button" onClick={() => { setStep('path'); setError('') }}><ArrowLeft size={14} />Back</button><button className="primary"><ArrowRight size={14} />Review project</button></div>
    </form>}

    {step === 'review' && form && <div className="project-review-step">
      <div className="project-review-title"><span className="project-monogram large">{form.name.slice(0,2).toUpperCase()}</span><div><h3>{form.name}</h3><p>{form.repo_path}</p></div><em>P{form.priority}</em></div>
      <dl><div><dt>Current goal</dt><dd>{form.current_goal}</dd></div><div><dt>Program</dt><dd>{form.program || 'general'}</dd></div><div><dt>Privacy</dt><dd>{form.privacy}</dd></div><div><dt>Stack</dt><dd>{form.stack || 'Not recorded'}</dd></div><div><dt>Tests</dt><dd>{form.test_command || 'Not recorded'}</dd></div><div><dt>Allowed workers</dt><dd>{form.allowed_workers.join(', ')}</dd></div><div><dt>State file</dt><dd>{form.track_state ? preview.state_exists ? 'Preserve existing state file' : 'Create starter state file' : 'Deferred'}</dd></div><div><dt>Next step</dt><dd>{form.guided_blueprint ? 'Guided blueprint interview' : 'Quick registration only · Blueprint needed'}</dd></div></dl>
      <div className="project-local-note"><ShieldCheck size={15} /><span>Registration updates local Cortex and optionally creates one starter state file. It does not create GitHub items or start an AI worker.</span></div>
      {error && <div className="project-form-error">{error}</div>}
      <div className="modal-actions"><button type="button" onClick={() => { setStep('details'); setError('') }}><ArrowLeft size={14} />Edit settings</button><button type="button" className="primary" onClick={register} disabled={busy}>{busy ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}{busy ? 'Registering…' : form.guided_blueprint ? 'Register & build blueprint' : 'Register project'}</button></div>
    </div>}
  </section></div>
}
