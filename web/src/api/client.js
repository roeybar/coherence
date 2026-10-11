// Thin client for the engine. Every call resolves to {ok, data} or {ok:false, error}.
import { t } from '../copy.js';

export const PROVIDERS = ['anthropic', 'openai', 'gemini', 'xai'];
export const DEFAULT_INTERVAL = 15;
const KINDS = new Set([
  'byok_required', 'byok_rejected', 'rate_limited', 'bad_save', 'bad_session',
  'provider_unavailable', 'provider_rate_limited', 'body_too_large',
  'wrong_passphrase', 'content_blocked', 'intervention_missing', 'bad_pack', 'bad_request',
  'unknown_provider', 'internal', 'busy', 'key_store_unavailable',
  'output_blocked', 'save_blocked', 'text_too_large', 'context_too_large', 'actions_rate_limited',
  'key_store_locked', 'key_other_provider',
]);
// the web server answers this when it cannot reach the engine
const DOWN = 'engine_unavailable';

// A random UUIDv4 for the page load; kept in memory only.
export function newSessionId(rand = (n) => crypto.getRandomValues(new Uint8Array(n))) {
  const b = rand(16);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
  return [h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20)].join('-');
}

// The one error parser: reads body.detail.error and retry_after.
export function parseError(status, body) {
  const d = body && typeof body === 'object' && body.detail && typeof body.detail === 'object' ? body.detail : {};
  return {
    status,
    kind: KINDS.has(d.error) ? d.error : d.error === DOWN ? 'engine_down' : 'unknown',
    provider: typeof d.provider === 'string' ? d.provider : null,
    retryAfter: Number.isFinite(d.retry_after) ? d.retry_after : null,
    stored: Array.isArray(d.stored) ? d.stored.filter((p) => PROVIDERS.includes(p)) : [],
  };
}

// A hosted server checks less save text; its own copy says so.
export function errorText(err, seconds = DEFAULT_INTERVAL) {
  const key = err.kind === 'text_too_large' && err.hosted ? 'error.text_too_large_hosted' : 'error.' + err.kind;
  return t(key, { seconds, retry_after: err.retryAfter === null ? seconds : err.retryAfter,
    provider: err.provider && PROVIDERS.includes(err.provider) ? t('provider.' + err.provider + '.label') : '',
    stored: (err.stored || []).map((p) => t('provider.' + p + '.label')).join(', ') });
}

// a save over the history cap gets the plain too-large text, which names no number
const historyCapped = (body) => !!body && typeof body === 'object' && !!body.detail
  && typeof body.detail === 'object' && body.detail.scope === 'history';

export function createClient({ fetchImpl, base = '' } = {}) {
  const doFetch = fetchImpl || ((...a) => globalThis.fetch(...a));
  const sessionId = newSessionId();
  const st = { mode: 'selfhost', provider: 'anthropic', key: null };

  function headers(json) {
    const h = { 'X-Coherence-Session': sessionId, 'X-Provider': st.provider };
    if (json) h['Content-Type'] = 'application/json';
    if (st.mode === 'hosted' && st.key) h['X-Provider-Key'] = st.key;
    return h;
  }

  async function call(method, path, body) {
    let res;
    try {
      res = await doFetch(base + path, {
        method,
        headers: headers(body !== undefined),
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      return { ok: false, error: { ...parseError(0, null), kind: 'engine_down' } };
    }
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    if (!res.ok) {
      const error = parseError(res.status, data);
      if (error.kind === 'text_too_large' && st.mode === 'hosted' && !historyCapped(data)) error.hosted = true;
      return { ok: false, error };
    }
    return { ok: true, data };
  }

  const post = (path, body) => call('POST', path, body);
  const known = (p) => PROVIDERS.includes(p);

  return {
    sessionId,
    configure: (patch) => Object.assign(st, patch),
    config: () => call('GET', '/api/config'),
    packs: () => call('GET', '/api/packs'),
    newGame: (body) => post('/api/game/new', body),
    turn: (save, playerInput, extra = {}) => post('/api/game/turn', { save, player_input: playerInput, ...extra }),
    undo: (save) => post('/api/game/undo', { save }),
    undoTo: (save, turnIndex) => post('/api/game/undo-to-turn', { save, turn_index: turnIndex }),
    regenerate: (save, feedback = '') => post('/api/game/regenerate', { save, feedback: String(feedback).slice(0, 1000) }),
    checkSave: (save) => post('/api/game/check', { save }),
    health: () => call('GET', '/api/health'),
    keys: {
      status: () => call('GET', '/api/keys/status'),
      unlock: (passphrase) => post('/api/keys/unlock', { passphrase }),
      lock: () => post('/api/keys/lock', {}),
      put: (provider, key) => (known(provider) ? call('PUT', '/api/keys/' + provider, { key }) : Promise.resolve({ ok: false, error: parseError(0, null) })),
      remove: (provider) => (known(provider) ? call('DELETE', '/api/keys/' + provider) : Promise.resolve({ ok: false, error: parseError(0, null) })),
    },
  };
}

// A 200 turn response that asks the player something carries a gate kind.
// Each option's `extra` is merged into the re-sent turn body.
export function gateOf(resp) {
  const pick = (field) => (o) => ({ label: o.label || String(o), extra: { [field]: o.value !== undefined ? o.value : o } });
  const opts = (field) => (Array.isArray(resp.options) ? resp.options.map(pick(field)) : []);
  const title = resp.message || resp.flavor || '';
  switch (resp.kind) {
    case 'addressee_clarification_needed': return { kind: resp.kind, title, options: opts('addressee') };
    case 'ability_intent_clarification_needed': return { kind: resp.kind, title, options: opts('ability_intent') };
    case 'reframe_confirmation_needed': return { kind: resp.kind, title, options: opts('reframe_confirm') };
    case 'world_rule_shift_pending': return { kind: resp.kind, title, options: opts('world_rule_confirm') };
    case 'escalation_consent_needed':
      return { kind: resp.kind, title, options: [
        { label: 'Continue', extra: { consent_token: resp.consent_token } },
        { label: 'Keep it small', extra: { decline_escalation: true } },
      ] };
    case 'craft_roll_needed': return { kind: resp.kind, title, options: [], roll: 'craft_roll', die: 20 };
    case 'dice_roll_needed': return { kind: resp.kind, title, options: [], roll: 'dice_roll', die: resp.dice_sides || 20 };
    default: return null;
  }
}
