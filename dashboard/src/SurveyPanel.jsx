import { useState } from 'react'
import { AlertTriangle, BookOpenCheck, FileText, ListChecks, LoaderCircle, RefreshCw } from 'lucide-react'
import { checkedLabel, kindLabel, openCount, planSourceLabel, readingList, refreshOutcome, surveyTone } from './survey.js'

// What this project is, and where its own documents say it goes next. Read from
// the repository, never from a model run, so pressing Re-evaluate costs nothing
// and can be done as often as the owner likes.
export default function SurveyPanel({ survey, onRefresh }) {
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState('')
  const tone = surveyTone(survey)
  const documents = readingList(survey)

  async function refresh() {
    setBusy(true)
    setOutcome('')
    try {
      setOutcome(refreshOutcome(await onRefresh()))
    } catch (error) {
      setOutcome(error?.message || 'Could not read the repository.')
    } finally {
      setBusy(false)
    }
  }

  return <section className={`survey-panel survey-${tone}`}>
    <div className="drawer-section-head">
      <label><BookOpenCheck size={13} />What this is, and where it goes</label>
      <span className="survey-checked">{checkedLabel(survey?.checked_at)}</span>
    </div>

    {!survey ? <p className="survey-empty">
      Cortex has not read this repository yet. Re-evaluate reads the README, the plan
      or roadmap, and the git history — no agent and no tokens.
    </p> : <>
      <div className="survey-block">
        <small>Does</small>
        <p>{survey.does || 'Nothing in the repository says what this is.'}</p>
      </div>

      <div className="survey-block">
        <small>Plan says</small>
        <p>{survey.plan_says || 'No direction is stated.'}</p>
        <span className="survey-source"><FileText size={12} />{planSourceLabel(survey)}</span>
      </div>

      {survey.synthesis && <p className="survey-synthesis">{survey.synthesis}</p>}

      {!!(survey.next_steps || []).length && <div className="survey-block">
        <small><ListChecks size={12} />Open in the plan</small>
        <ul className="survey-steps">
          {survey.next_steps.map((step, index) => <li key={index}>{step}</li>)}
        </ul>
      </div>}

      {!!(survey.drift || []).length && <ul className="survey-drift">
        {survey.drift.map((note, index) => <li key={index}><AlertTriangle size={12} />{note}</li>)}
      </ul>}

      {!!documents.length && <details className="survey-documents">
        <summary>{documents.length} document{documents.length === 1 ? '' : 's'} read</summary>
        <ul>
          {documents.map(doc => <li key={doc.path}>
            <code>{doc.path}</code>
            <span>{kindLabel(doc.kind)}{openCount(doc) ? ` · ${openCount(doc)} open` : ''}</span>
          </li>)}
        </ul>
      </details>}
    </>}

    <button type="button" className="survey-refresh" onClick={refresh} disabled={busy}>
      {busy ? <LoaderCircle className="spin" size={14} /> : <RefreshCw size={14} />}
      {busy ? 'Reading the repository…' : 'Re-evaluate'}
    </button>
    {outcome && <small className="survey-outcome">{outcome}</small>}
  </section>
}
