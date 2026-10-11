// Encrypted save envelope: PBKDF2-SHA256 + AES-256-GCM, the header bound as AAD.
export const FORMAT = 'coherence-save';
export const VERSION = 1;
export const ITER_MIN = 600000;
export const ITER_MAX = 2000000;
const SALT_LEN = 16;
const IV_LEN = 12;

export class WrongPassphraseOrCorrupt extends Error {
  constructor() {
    super('wrong passphrase or corrupt data');
    this.name = 'WrongPassphraseOrCorrupt';
  }
}

const enc = new TextEncoder();
const dec = new TextDecoder();

export function b64(bytes) {
  let s = '';
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

export function unb64(str) {
  if (typeof str !== 'string' || !/^[A-Za-z0-9+/]*={0,2}$/.test(str)) throw new WrongPassphraseOrCorrupt();
  const s = atob(str);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

// the header in fixed key order; also the AAD
function headerOf(env) {
  return {
    format: env.format,
    v: env.v,
    kdf: { name: env.kdf.name, hash: env.kdf.hash, iterations: env.kdf.iterations, salt: env.kdf.salt },
    cipher: { name: env.cipher.name, iv: env.cipher.iv },
  };
}

const aad = (env) => enc.encode(JSON.stringify(headerOf(env)));

// A non-extractable PBKDF2 key: the page keeps this, never the passphrase string.
export function baseKey(passphrase) {
  return crypto.subtle.importKey('raw', enc.encode(passphrase), 'PBKDF2', false, ['deriveKey']);
}

export async function deriveKey(passphrase, salt, iterations = ITER_MIN) {
  return deriveFrom(await baseKey(passphrase), salt, iterations);
}

export async function deriveFrom(base, salt, iterations = ITER_MIN) {
  const key = await crypto.subtle.deriveKey(
    { name: 'PBKDF2', hash: 'SHA-256', salt, iterations },
    base,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  );
  return { key, salt, iterations };
}

// Derive once per passphrase; every seal() with the result uses a fresh IV.
export function makeKey(passphrase) {
  return deriveKey(passphrase, crypto.getRandomValues(new Uint8Array(SALT_LEN)));
}

export async function seal(obj, passOrKey) {
  const k = typeof passOrKey === 'string' ? await makeKey(passOrKey) : passOrKey;
  const iv = crypto.getRandomValues(new Uint8Array(IV_LEN));
  const env = {
    format: FORMAT,
    v: VERSION,
    kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations: k.iterations, salt: b64(k.salt) },
    cipher: { name: 'AES-GCM', iv: b64(iv) },
  };
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv, additionalData: aad(env) },
    k.key,
    enc.encode(JSON.stringify(obj)),
  );
  return { ...env, ct: b64(new Uint8Array(ct)) };
}

function checkShape(env) {
  const ok = env && typeof env === 'object'
    && env.format === FORMAT && env.v === VERSION
    && env.kdf && env.kdf.name === 'PBKDF2' && env.kdf.hash === 'SHA-256'
    && Number.isInteger(env.kdf.iterations)
    && env.kdf.iterations >= ITER_MIN && env.kdf.iterations <= ITER_MAX
    && env.cipher && env.cipher.name === 'AES-GCM'
    && typeof env.kdf.salt === 'string' && typeof env.cipher.iv === 'string'
    && typeof env.ct === 'string';
  if (!ok) throw new WrongPassphraseOrCorrupt();
  const salt = unb64(env.kdf.salt);
  const iv = unb64(env.cipher.iv);
  const ct = unb64(env.ct);
  if (salt.length !== SALT_LEN || iv.length !== IV_LEN || ct.length < 16) throw new WrongPassphraseOrCorrupt();
  return { salt, iv, ct };
}

export function isEnvelope(env) {
  try {
    checkShape(env);
    return true;
  } catch {
    return false;
  }
}

// Keys derived from one base key, one cached promise per salt and iteration count.
export function keyring(base) {
  const cache = new Map();
  const keep = (id, p) => {
    cache.set(id, p);
    p.catch(() => { if (cache.get(id) === p) cache.delete(id); });
    return p;
  };
  return {
    keyFor(saltB64, iterations) {
      const id = saltB64 + ':' + iterations;
      return cache.get(id) || keep(id, Promise.resolve().then(() => deriveFrom(base, unb64(saltB64), iterations)));
    },
    fresh() {
      const salt = crypto.getRandomValues(new Uint8Array(SALT_LEN));
      return keep(b64(salt) + ':' + ITER_MIN, deriveFrom(base, salt));
    },
  };
}

// Every failure surfaces as one error, and the shape is checked before deriving.
// The second argument is a passphrase string or a keyring.
export async function open(env, passOrRing) {
  const { salt, iv, ct } = checkShape(env);
  try {
    const k = typeof passOrRing === 'string'
      ? await deriveKey(passOrRing, salt, env.kdf.iterations)
      : await passOrRing.keyFor(env.kdf.salt, env.kdf.iterations);
    const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv, additionalData: aad(env) }, k.key, ct);
    return JSON.parse(dec.decode(plain));
  } catch {
    throw new WrongPassphraseOrCorrupt();
  }
}
