import React from 'react';
import { t } from '../copy.js';
import { mask, applyEdit, reconcile } from '../keyMask.js';

// A provider key field that password managers leave alone and that never holds the key
// itself: React does not set its value; the page writes the masked text and maps each edit.
export function KeyField({ value, onChange, disabled, style }) {
  const ref = React.useRef(null);
  const real = React.useRef(value);
  const composing = React.useRef(false);
  const cb = React.useRef(onChange);
  cb.current = onChange;

  React.useEffect(() => {
    const el = ref.current;
    real.current = value;
    if (el && el.value !== mask(value)) el.value = mask(value);
  }, [value]);

  React.useEffect(() => {
    const el = ref.current;
    const commit = (next, caret) => {
      real.current = next;
      el.value = mask(next);
      if (document.activeElement === el) el.setSelectionRange(caret, caret);
      cb.current(next);
    };
    const sync = () => {
      const shown = mask(real.current);
      if (el.value === shown) return;
      const r = reconcile(real.current, shown, el.value, el.selectionEnd ?? el.value.length);
      commit(r.real, r.caret);
    };
    const onBefore = (e) => {
      if (composing.current || e.isComposing || !e.cancelable) return;
      e.preventDefault();
      const data = e.data ?? (e.dataTransfer ? e.dataTransfer.getData('text/plain') : '');
      const r = applyEdit(real.current, { type: e.inputType, data, start: el.selectionStart, end: el.selectionEnd });
      commit(r.real, r.caret);
    };
    const onPaste = (e) => {
      e.preventDefault();
      const data = e.clipboardData ? e.clipboardData.getData('text') : '';
      const r = applyEdit(real.current, { type: 'insertFromPaste', data, start: el.selectionStart, end: el.selectionEnd });
      commit(r.real, r.caret);
    };
    const onDrop = (e) => e.preventDefault();
    const onStart = () => { composing.current = true; };
    const onEnd = () => { composing.current = false; sync(); };
    const onInput = () => { if (!composing.current) sync(); };
    const on = [['beforeinput', onBefore], ['paste', onPaste], ['drop', onDrop],
      ['compositionstart', onStart], ['compositionend', onEnd], ['input', onInput]];
    for (const [k, f] of on) el.addEventListener(k, f);
    return () => { for (const [k, f] of on) el.removeEventListener(k, f); };
  }, []);

  return (
    <input ref={ref} className="co-field" type="text" id="provider-key" name="provider-key"
      autoComplete="off" spellCheck={false} autoCapitalize="off" autoCorrect="off"
      data-1p-ignore="" data-lpignore="true" data-bwignore="" data-form-type="other" data-testid="apikey-input"
      aria-label={t('keys.field.label')} placeholder={t('keys.field.placeholder')}
      disabled={disabled} style={style} defaultValue={mask(value)} />
  );
}
