export const KEY_KINDS = ['byok_required', 'byok_rejected', 'key_store_locked', 'key_other_provider'];

export function opensSettings(error) {
  return !!error && KEY_KINDS.includes(error.kind);
}

export function newGameKey(error) {
  if (!error) return null;
  if (error.kind === 'output_blocked') return 'error.output_blocked_new';
  if (error.kind === 'content_blocked') return 'error.content_blocked_new';
  if (['provider_unavailable', 'internal', 'unknown', 'busy'].includes(error.kind)) return 'error.new_failed';
  return null;
}

// The seasonal greeting runs on the player's own calendar, Dec 24 to 26.
export function isSeason(date) {
  return date.getMonth() === 11 && date.getDate() >= 24 && date.getDate() <= 26;
}

// True when the chapter number of the save went up between two saves.
export function milestone(prevDoc, nextDoc) {
  const chapter = (d) => (d && d.state && typeof d.state.chapter === 'number' ? d.state.chapter : NaN);
  const a = chapter(prevDoc);
  const b = chapter(nextDoc);
  return Number.isFinite(a) && Number.isFinite(b) && b > a;
}

// At most one alert at a time; pickAlert checks them in a fixed priority.
// error: a parsed client error or null; engineDown: the engine cannot be reached;
// milestone: the chapter just rose; now: a Date.
export function pickAlert({ error, engineDown, milestone: rose, now }) {
  if (engineDown || (error && error.kind === 'engine_down')) return { kind: 'maint' };
  if (error && BLOCKED.includes(error.kind)) return { kind: '451' };
  if (error) return { kind: '500' };
  if (rose) return { kind: isSeason(now || new Date()) ? 'xmas' : 'grant' };
  return null;
}

const BLOCKED = ['content_blocked', 'output_blocked', 'save_blocked'];
const OUTPUT_BLOCKED = {
  turn: 'error.output_blocked', regenerate: 'error.output_blocked_regenerate', new: 'error.output_blocked_new',
};

// The 451 body: a blocked reply names what was asked for (error.where).
export function blockedKey(error) {
  if (!error || error.kind === 'content_blocked') return 'alert.451.body';
  if (error.kind === 'output_blocked') return OUTPUT_BLOCKED[error.where] || 'error.output_blocked_other';
  return 'error.' + error.kind;
}

// "The realm shuddered" is for server errors only; a 4xx keeps the plain label.
export function shudders(error) {
  return !!error && Number.isFinite(error.status) && error.status >= 500;
}

// The maintenance copy: the saved-story text only while the open game is stored
// in this browser. Otherwise the player is told to save to file, and no reload
// control is offered.
export function maintCopy(stored) {
  return stored
    ? { body: 'alert.maint.body', refresh: true }
    : { body: 'maintenance.unsaved', refresh: false };
}
