// Small display preferences, kept in this browser only.
export const PREFS_KEY = 'coherence.prefs';
export const DEFAULTS = { submitKey: 'enter' };
const SUBMIT_KEYS = ['enter', 'shift_enter'];

export function validate(raw) {
  const out = { ...DEFAULTS };
  if (raw && typeof raw === 'object' && !Array.isArray(raw) && SUBMIT_KEYS.includes(raw.submitKey)) {
    out.submitKey = raw.submitKey;
  }
  return out;
}

export function loadPrefs(store) {
  try {
    return validate(JSON.parse(store.getItem(PREFS_KEY)));
  } catch {
    return { ...DEFAULTS };
  }
}

export function savePrefs(store, prefs) {
  try {
    store.setItem(PREFS_KEY, JSON.stringify(validate(prefs)));
  } catch {
    // storage may be full or blocked; the choice then lasts for this page only
  }
}
