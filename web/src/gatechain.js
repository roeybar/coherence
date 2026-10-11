export function mergeAnswers(prev, extra) {
  return { ...(prev || {}), ...(extra || {}) };
}
