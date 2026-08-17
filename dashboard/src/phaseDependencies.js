export function editableDependencyPhases(phases = []) {
  return phases.filter(phase => Number(phase.ordinal) > 1)
}

export function dependencyCandidates(phases = [], phase) {
  const ordinal = Number(phase?.ordinal || 0)
  return phases.filter(candidate => Number(candidate.ordinal) < ordinal)
}

export function selectedDependencyOrdinals(phase) {
  return (phase?.depends_on || [])
    .map(dependency => Number(dependency.depends_on_ordinal))
    .filter(Number.isInteger)
    .sort((left, right) => left - right)
}

export function toggleDependencyOrdinal(ordinals, ordinal) {
  const next = new Set(ordinals.map(Number))
  const value = Number(ordinal)
  if (next.has(value)) next.delete(value)
  else next.add(value)
  return [...next].sort((left, right) => left - right)
}
