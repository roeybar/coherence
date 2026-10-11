// Save documents: validation, the browser envelope and the save file.
import { seal, open, isEnvelope, FORMAT, VERSION } from './crypto.js';

// Slot ids in the browser store: 16 hex digits, or the migrated single save.
export const ID_RE = /^(?:[0-9a-f]{16}|legacy|legacy-local)$/;
export const FILE_CAP = 134217728;

export class InvalidState extends Error {
  constructor(kind) {
    super(kind);
    this.name = 'InvalidState';
    this.kind = kind;
  }
}

const OBJECTS = ['profile', 'state', 'config', 'glossary', 'seed_roster', 'checkpoint_state'];
const ARRAYS = ['undo_stack', 'checkpoint_history', 'items', 'memory', 'physics_cache'];
const STRINGS = ['name', 'created', 'updated'];
const NUMBERS = ['turn', 'checkpoint_turn', 'checkpoint_len', 'session_in', 'session_out'];
const REQUIRED = ['profile', 'state', 'config'];
const POISON = new Set(['__proto__', 'constructor', 'prototype']);

export function parseJson(text) {
  return JSON.parse(text, (k, v) => (POISON.has(k) ? undefined : v));
}

const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

// Takes the plaintext {save: doc}; returns a clean doc with allowlisted keys only.
export function validate(plain) {
  if (!isObj(plain) || !isObj(plain.save)) throw new InvalidState('bad_file');
  const src = plain.save;
  const out = {};
  for (const k of OBJECTS) {
    if (src[k] === undefined) continue;
    if (!isObj(src[k])) throw new InvalidState('bad_file');
    out[k] = src[k];
  }
  for (const k of ARRAYS) {
    if (src[k] === undefined) continue;
    if (!Array.isArray(src[k])) throw new InvalidState('bad_file');
    out[k] = src[k];
  }
  for (const k of STRINGS) {
    if (src[k] === undefined) continue;
    if (typeof src[k] !== 'string' || src[k].length > 200) throw new InvalidState('bad_file');
    out[k] = src[k];
  }
  for (const k of NUMBERS) {
    if (src[k] === undefined) continue;
    if (!Number.isFinite(src[k])) throw new InvalidState('bad_file');
    out[k] = src[k];
  }
  if (src.read_only !== undefined) out.read_only = src.read_only === true;
  if (src.shadow_of !== undefined && src.shadow_of !== null) {
    if (typeof src.shadow_of !== 'string') throw new InvalidState('bad_file');
    out.shadow_of = src.shadow_of;
  }
  if (!Array.isArray(src.history)) throw new InvalidState('bad_file');
  if (!src.history.every((h) => isObj(h) && typeof h.role === 'string')) throw new InvalidState('bad_file');
  out.history = src.history;
  if (!REQUIRED.every((k) => out[k])) throw new InvalidState('bad_file');
  return out;
}

export function newSlotId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, '0')).join('');
}

// The browser store interface: ids() -> sorted ids; get(id) -> text or null if absent;
// cas(id, want, next) writes next (null deletes) only while the stored text equals want
// (null = absent) and resolves to 'ok', 'conflict' or 'full'.

// One stored game: missing, damaged or over-cap records throw InvalidState; a valid envelope
// that does not open throws WrongPassphraseOrCorrupt.
export async function readSlot(store, id, ring) {
  const text = store ? await store.get(id) : null;
  if (typeof text !== 'string' || text.length > FILE_CAP) throw new InvalidState('bad_file');
  let env;
  try {
    env = parseJson(text);
  } catch {
    throw new InvalidState('bad_file');
  }
  if (!isEnvelope(env)) throw new InvalidState('bad_file');
  return { doc: validate(await open(env, ring)), text };
}

// Browser storage holds only the envelope. A failed write changes nothing else.
// live() is checked after sealing; a superseded or cancelled persist never writes.
// want is the text this page last read or wrote for the slot, or null when it expects none.
export async function persist(store, id, doc, key, live = () => true, want = null) {
  // the envelope is longer than the plain text, so an over-cap game is refused before sealing
  if (JSON.stringify({ save: doc }).length > FILE_CAP) return { ok: false, kind: 'storage_full' };
  const env = await seal({ save: doc }, key);
  if (!live()) return { ok: false, kind: 'stale' };
  const text = JSON.stringify(env);
  if (text.length > FILE_CAP) return { ok: false, kind: 'storage_full' };
  let r;
  try {
    r = await store.cas(id, want, text);
  } catch {
    r = 'full';
  }
  if (r === 'ok') return { ok: true, bytes: text.length, text };
  if (r === 'conflict') return { ok: false, kind: 'conflict' };
  return { ok: false, kind: 'storage_full' };
}

// key is a derived key object for an encrypted file, or null for plain JSON
export async function exportFile(doc, key) {
  if (key) return JSON.stringify(await seal({ save: doc }, key));
  return JSON.stringify({ format: FORMAT, v: VERSION, save: doc });
}

// Parse, open, validate: all or nothing.
export async function importFile(text, passphrase) {
  if (typeof text !== 'string' || text.length > FILE_CAP) throw new InvalidState('file_too_large');
  let parsed;
  try {
    parsed = parseJson(text);
  } catch {
    throw new InvalidState('bad_file');
  }
  if (isObj(parsed) && parsed.ct !== undefined) return validate(await open(parsed, passphrase));
  if (isObj(parsed) && parsed.format === FORMAT && parsed.v === VERSION) return validate(parsed);
  throw new InvalidState('bad_file');
}

// Hands out generation tokens; a token is live until a newer one is issued or cancel() runs.
export function generations() {
  let n = 0;
  return {
    next() { const mine = ++n; return () => mine === n; },
    cancel() { n += 1; },
  };
}

// Autosave status after a persist; null (storage blocked) counts the same as full.
export function autosaveStatus(result) {
  return result && result.ok ? { saved: true, full: false } : { saved: false, full: true };
}

// Save to file and Fork share one rule: plain ticked writes plain JSON, with or
// without a passphrase; otherwise the file is encrypted, which needs a passphrase.
export function forkPlain(key, plain) {
  if (plain) return 'plain';
  return key ? 'encrypted' : 'need_passphrase';
}
