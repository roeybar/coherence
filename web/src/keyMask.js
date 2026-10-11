// The provider key field shows the first characters of a key in clear and one mask
// character for each further one. The page keeps the real value; the field only ever
// holds the masked text, and edits are mapped back position by position.
export const MASK = '*';
export const CLEAR = 4;

export function mask(real) {
  const s = String(real ?? '');
  return s.slice(0, CLEAR) + MASK.repeat(Math.max(0, s.length - CLEAR));
}

// a single-line field drops line breaks, as a native text input does
export function clean(text) {
  return String(text ?? '').replace(/[\r\n]/g, '');
}

const clamp = (n, len) => Math.min(len, Math.max(0, Number.isFinite(n) ? Math.trunc(n) : len));

// one edit the browser asked for, applied to the real value
export function applyEdit(real, { type, data, start, end }) {
  const len = real.length;
  let a = clamp(start, len);
  let b = clamp(end, len);
  if (a > b) [a, b] = [b, a];
  const kind = String(type || '');
  const splice = (from, to, text = '') => ({ real: real.slice(0, from) + text + real.slice(to), caret: from + text.length });
  if (kind === 'insertLineBreak' || kind === 'insertParagraph' || kind.startsWith('history')) return { real, caret: a };
  if (kind.startsWith('insert')) return splice(a, b, clean(data));
  if (!kind.startsWith('delete')) return { real, caret: a };
  if (a !== b) return splice(a, b);
  if (kind === 'deleteContentBackward') return a > 0 ? splice(a - 1, a) : { real, caret: a };
  if (kind === 'deleteContentForward') return splice(a, Math.min(len, a + 1));
  if (kind.includes('Backward')) return splice(0, a);
  if (kind.includes('Forward')) return splice(a, len);
  return { real, caret: a };
}

// a change the browser made by itself (composition, autofill): diff the old and new
// field text around the caret and carry the unchanged parts over from the real value
export function reconcile(real, prev, next, caret) {
  const s = next.length - caret;
  const cap = Math.min(caret, prev.length - s);
  let p = 0;
  while (p < cap && prev[p] === next[p]) p += 1;
  return { real: real.slice(0, p) + next.slice(p, caret) + real.slice(prev.length - s), caret };
}
