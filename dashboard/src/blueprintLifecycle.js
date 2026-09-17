const lifecycle = {
  missing: {
    label: 'Blueprint needed',
    description: 'Cortex has no approved design and phase contract for this legacy project yet. Existing work remains available.',
    actionLabel: 'Build project blueprint',
  },
  draft: {
    label: 'Drafting',
    description: 'The guided blueprint interview is saved locally and can be resumed without repeating repository discovery.',
    actionLabel: 'Resume blueprint interview',
  },
  review: {
    label: 'Ready for owner review',
    description: 'An exact, restart-safe blueprint preview is waiting for owner approval. No execution work has been generated.',
    actionLabel: 'Reopen owner review',
  },
  approved: { label: 'Approved' },
  stale: { label: 'Drifted' },
}

export function blueprintLifecycleMeta(blueprint) {
  const status = blueprint?.status || 'missing'
  const fallback = {
    label: String(status).replaceAll('_', ' '),
    description: 'The project blueprint lifecycle can be inspected from this project.',
    actionLabel: 'Open blueprint',
  }
  const meta = lifecycle[status] || fallback
  return {
    ...meta,
    status,
    onboarding: ['missing', 'draft', 'review'].includes(status),
    previewFingerprint: blueprint?.draft?.preview_fingerprint || null,
  }
}

export function validateBlueprintDraft(loaded) {
  if (!loaded || typeof loaded !== 'object') {
    throw new Error('Cortex did not return a blueprint draft. Retry the interview or use the local CLI fallback below.')
  }
  if (!Array.isArray(loaded.questions) || !Array.isArray(loaded.phase_questions)) {
    throw new Error('Cortex returned an incomplete blueprint interview. Retry the interview or use the local CLI fallback below.')
  }
  if (!loaded.discovery || !loaded.discovery_hash) {
    throw new Error('Cortex could not confirm bounded repository discovery. Retry the interview or use the local CLI fallback below.')
  }
  return loaded
}

export function blueprintCliFallback(repoPath) {
  const quotedPath = String(repoPath || '').replaceAll('"', '`"')
  return `.\\scripts\\cortex-portfolio.ps1 project onboard "${quotedPath}" --draft-only`
}
