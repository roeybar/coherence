// The status icon at the top right: what the page knows about the game server and the key.
import { t } from './copy.js';
import { PROVIDERS } from './api/client.js';

// replies that mean the store is locked; a store with no file yet answers the same way
export const LOCKED_KINDS = ['byok_required', 'key_store_locked'];

// one key-store status read, sorted into the state the icon and Settings show
export function storeFromRead(r) {
  if (r && r.ok) {
    const data = r.data || {};
    return { unlocked: data.unlocked !== false, keys: data.keys || data, error: null };
  }
  const kind = r && r.error ? r.error.kind : undefined;
  if (LOCKED_KINDS.includes(kind)) return { unlocked: false, keys: {}, error: null };
  if (kind === 'engine_down') return { unlocked: false, keys: {}, error: 'engine_down' };
  if (kind === 'key_store_unavailable') return { unlocked: false, keys: {}, error: 'unreadable' };
  return { unlocked: false, keys: {}, error: 'unchecked' };
}

// null while nothing is known yet; otherwise { level, key, vars } for the icon
export function linkStatus({ cfg, engineDown, hostedKey, hostedKeys, store, provider }) {
  if (engineDown) return { level: 'error', key: 'status.engine_down' };
  if (!cfg) return null;
  if (cfg.mode === 'hosted') {
    if (hostedKey) return { level: 'ok', key: 'status.ok.hosted' };
    const held = Object.values(hostedKeys || {}).some(Boolean);
    return held ? { level: 'attention', key: 'status.no_key', vars: { provider } } : { level: 'attention', key: 'status.no_key.hosted' };
  }
  if (!store) return null;
  if (store.error === 'engine_down') return { level: 'error', key: 'status.engine_down' };
  if (store.error === 'unreadable') return { level: 'attention', key: 'status.store_unreadable' };
  if (store.error === 'unchecked') return { level: 'attention', key: 'status.unchecked' };
  if (!store.unlocked) return { level: 'attention', key: 'status.locked' };
  if (!(store.keys || {})[provider]) {
    const others = PROVIDERS.filter((p) => p !== provider && (store.keys || {})[p]);
    if (others.length) return { level: 'attention', key: 'status.other_key', vars: { provider, stored: others } };
    return { level: 'attention', key: 'status.no_key', vars: { provider } };
  }
  return { level: 'ok', key: 'status.ok' };
}

export function statusText(s) {
  if (!s) return '';
  const v = s.vars || {};
  const vars = {};
  if (v.provider) vars.provider = t('provider.' + v.provider + '.label');
  if (v.stored) vars.stored = v.stored.map((p) => t('provider.' + p + '.label')).join(', ');
  return t(s.key, vars);
}
