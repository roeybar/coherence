import React from 'react';
import { t } from './copy.js';
import { errorText } from './api/client.js';
import { WrongPassphraseOrCorrupt } from './crypto.js';
import {
  exportFile, importFile, InvalidState, FILE_CAP, autosaveStatus, forkPlain,
} from './state.js';
import { createSlots, shouldAutoOpen, ownerOf } from './slots.js';
import { savedAfterWrite, noteKey } from './saveStatus.js';
import { openIdbStore, prepareStore, makeBus } from './slots-db.js';

const store = () => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

// "N responses" counts the GM entries of a save.
export function responsesOf(doc) {
  return ((doc && doc.history) || []).filter((h) => h && h.role === 'assistant').length;
}

// The first paragraph of the opening GM entry, cut to 240 characters.
export function summaryOf(doc, max = 240) {
  const first = ((doc && doc.history) || []).find((h) => h && h.role === 'assistant');
  const raw = first && (first.content_en || first.content);
  const text = (typeof raw === 'string' ? raw : '').trim();
  const para = text.split(/\n\s*\n/)[0].replace(/\s+/g, ' ').trim();
  return para.length > max ? para.slice(0, max - 3).trimEnd() + '...' : para;
}

// A summary is a cut of a game's text, and a cut can make words the whole text
// does not hold, so the summary as shown is checked on its own. It shows only
// once the engine has passed that exact text; a refusal or a failed check keeps
// it hidden. changed() is called when a verdict arrives.
export function summaryGate(check, changed) {
  const verdicts = new Map();
  return (doc) => {
    const text = summaryOf(doc);
    if (!text) return '';
    const verdict = verdicts.get(text);
    if (verdict === undefined) {
      verdicts.set(text, 'pending');
      Promise.resolve()
        .then(() => check({ history: [{ role: 'assistant', content: text }] }))
        .then((r) => (r && r.ok && r.data && r.data.ok === true ? 'ok'
          : r && !r.ok && r.error && r.error.kind === 'busy' ? 'busy' : 'hidden'), () => 'hidden')
        .then((v) => {
          // a busy server is asked again on the next render; nothing re-renders for it
          if (v === 'busy') {
            verdicts.delete(text);
            return;
          }
          verdicts.set(text, v);
          changed();
        });
    }
    return verdict === 'ok' ? text : '';
  };
}

// The engine refused to vouch for a save; error is the client error.
export class SaveRefused extends Error {
  constructor(error) {
    super(error.kind);
    this.error = error;
  }
}

const REFUSED = {
  save_blocked: () => t('error.save_blocked'),
  body_too_large: () => t('error.body_too_large'),
  bad_save: () => t('saves.bad_file'),
};

// the message for a save the engine did not vouch for; a busy server says so
export function refusedText(error) {
  const k = error.kind;
  if (Object.hasOwn(REFUSED, k)) return REFUSED[k]();
  if (k === 'text_too_large' || k === 'actions_rate_limited' || k === 'busy') return errorText(error);
  return t('saves.check_unavailable');
}

// Browser autosave (encrypted envelopes, one slot per game) and save files. check asks the
// engine about a save; nothing of a stored or loaded game is shown until it says ok.
export function useSaves(doc, onLoad, check = async () => ({ ok: false, error: { kind: 'unknown' } })) {
  const [key, setKey] = React.useState(null);
  const [full, setFull] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState('');
  const [plain, setPlain] = React.useState(false);
  const [asking, setAsking] = React.useState(0);
  const [unlocking, setUnlocking] = React.useState(false);
  const [forcePass, setForcePass] = React.useState(null); // a passphrase refused only because older games are locked
  const [fileDoc, setFileDoc] = React.useState(null); // the doc last saved to or loaded from a file
  const [, setTick] = React.useState(0);
  const bump = () => setTick((n) => n + 1);
  // every summary shown, in a row or for the open game, goes through one gate
  const gate = React.useRef(null);
  if (!gate.current) gate.current = summaryGate(check, bump);
  const slotsRef = React.useRef(null);
  if (!slotsRef.current) {
    slotsRef.current = createSlots(openIdbStore().then((db) => prepareStore(db, store())), { bus: makeBus(), onChange: bump });
  }
  const slots = slotsRef.current;
  // the doc the page shows now: set when a doc is shown (show, track), and by a render with a
  // new doc; a render that still carries an older doc does not move it back
  const docRef = React.useRef(doc);
  const rendered = React.useRef(doc);
  if (rendered.current !== doc) {
    rendered.current = doc;
    docRef.current = doc;
  }
  const secure = typeof crypto !== 'undefined' && !!crypto.subtle;

  React.useEffect(() => { slots.refresh().then(bump); }, []);

  // the game each shown doc belongs to (see ownerOf)
  const owners = React.useRef(new WeakMap());
  const shown = React.useRef(null);
  React.useEffect(() => {
    const o = ownerOf(owners.current, doc, shown.current);
    if (doc) owners.current.set(doc, o);
    shown.current = o;
  }, [doc]);

  // writes only while the doc's game is still the open one
  React.useEffect(() => {
    if (!key || !doc || slots.slotNow() === null || owners.current.get(doc) !== slots.slotNow() || slots.isWritten(doc)) return;
    const gid = slots.slotNow();
    setSaved(false);
    setSaving(true);
    slots.write(doc).then(({ r }) => {
      if (r && r.kind === 'stale') return;
      if (gid === slots.slotNow()) {
        const st = autosaveStatus(r);
        setFull(st.full);
        setSaved(savedAfterWrite(doc, docRef.current, r));
        if (doc === docRef.current) setSaving(false);
      }
      bump();
    });
  }, [key, doc]);

  const fail = (e) => {
    if (e instanceof SaveRefused) setError(refusedText(e.error));
    else if (e instanceof WrongPassphraseOrCorrupt) setError(t('saves.wrong_passphrase'));
    else if (e instanceof InvalidState && e.kind === 'file_too_large') setError(t('saves.file_too_large'));
    else setError(t('saves.bad_file'));
  };

  const ready = (pass) => {
    setError('');
    if (!secure) { setError(t('saves.insecure_context')); return false; }
    if (!pass) { setError(t('saves.need_passphrase')); return false; }
    return true;
  };

  // the engine checks a game before any of it is shown; a refusal throws SaveRefused
  const vet = async (d) => {
    if (slots.isBlocked(d)) throw new SaveRefused({ kind: 'save_blocked' });
    if (slots.isVetted(d)) return;
    let r;
    try {
      r = await check(d);
    } catch {
      r = null;
    }
    if (r && r.ok && r.data && r.data.ok === true) {
      slots.markVetted(d);
      return;
    }
    const error = (r && !r.ok && r.error) || { kind: 'unknown' };
    if (error.kind === 'save_blocked') slots.markBlocked(d);
    throw new SaveRefused(error);
  };

  // expanding a stored game's row: a blocked one is only relabelled
  const vetSlot = async (id) => {
    const d = slots.docOf(id);
    if (!d) return;
    try {
      await vet(d);
    } catch (e) {
      if (!(e instanceof SaveRefused && e.error.kind === 'save_blocked')) fail(e);
    } finally {
      bump();
    }
  };

  // shows a doc of the open game at once, before the page renders it
  const show = (d) => {
    owners.current.set(d, slots.slotNow());
    docRef.current = d;
    onLoad(d);
  };

  const openSlot = async (id) => {
    setError('');
    const opening = slots.open(id, () => docRef.current, show, vet);
    bump();
    try {
      const d = await opening;
      if (d === 'busy') return false;
      show(d);
      setSaved(slots.isWritten(d));
      setSaving(false);
      setFull(false);
      bump();
      return true;
    } catch (e) {
      fail(e);
      return false;
    } finally {
      bump();
    }
  };

  // sets the passphrase for autosave; openLatest also opens the latest stored game
  const unlockWith = async (pass, openLatest, force = false) => {
    if (!ready(pass)) return;
    setForcePass(null);
    setUnlocking(true);
    try {
      const res = await slots.unlock(pass, !!docRef.current, { force });
      if (res.stale) return;
      if (!res.ok) {
        setError(t('saves.wrong_passphrase'));
        if (res.rule === 'ii') setForcePass(pass);
        return;
      }
      setKey(res.key);
      if (res.latest && (openLatest || shouldAutoOpen({ hasDoc: !!docRef.current, away: slots.away, latest: res.latest }))) {
        await openSlot(res.latest);
      }
      return true;
    } catch (e) {
      fail(e);
    } finally {
      setUnlocking(false);
      bump();
    }
  };

  // after a new or loaded game: with no passphrase set, ask for one
  const ask = () => {
    if (key) return false;
    setError(t('saves.need_passphrase'));
    setAsking((n) => n + 1);
    return true;
  };

  const saveFile = async (asPlain, target = doc, filename = 'coherence-save.json') => {
    setError('');
    if (!target) return null;
    const mode = forkPlain(key, asPlain);
    if (mode === 'need_passphrase' && !secure) { setError(t('saves.insecure_context')); return 'insecure_context'; }
    if (mode === 'need_passphrase') { setError(t('saves.need_passphrase')); return mode; }
    try {
      const text = await exportFile(target, mode === 'plain' ? null : key);
      const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setFileDoc(target);
    } catch {
      return 'failed';
    }
    return mode;
  };

  const adopt = (d) => {
    slots.markVetted(d);
    slots.adopt();
    show(d);
    setSaved(false);
    setSaving(false);
    setFull(false);
    bump();
  };

  const loadFile = async (file, pass) => {
    setError('');
    if (!file) return;
    if (file.size > FILE_CAP) { setError(t('saves.file_too_large')); return; }
    // the open game is left before the file is read; a file that does not load puts it back
    const sw = slots.keep(docRef.current);
    if (sw === 'busy') return;
    bump();
    try {
      let d;
      try {
        d = await importFile(await file.text(), pass);
        await vet(d);
      } catch (e) {
        const landed = slots.back(sw);
        if (landed) show(landed);
        fail(e);
        return;
      }
      adopt(d);
      setFileDoc(d);
      ask();
    } finally {
      slots.settle(sw);
      bump();
    }
  };

  // Save to file and remove. Where the browser has a save picker it is asked inside the
  // click, and the record goes only after the file is closed; elsewhere the download link.
  const removeSlot = (id) => {
    if (slots.switching) return Promise.resolve('busy');
    setError('');
    const picker = typeof window.showSaveFilePicker === 'function'
      ? window.showSaveFilePicker({ suggestedName: 'coherence-save.json' })
      : null;
    if (picker) picker.catch(() => {});
    const download = async (d) => {
      if (!picker) return ['plain', 'encrypted'].includes(await saveFile(plain, d, 'coherence-save.json'));
      const mode = forkPlain(key, plain);
      if (mode === 'need_passphrase') { setError(t('saves.need_passphrase')); return false; }
      const text = await exportFile(d, mode === 'plain' ? null : key);
      const w = await (await picker).createWritable();
      await w.write(text);
      await w.close();
      return true;
    };
    const removing = slots.remove(id, download);
    bump();
    return removing.catch(fail).finally(bump);
  };

  return {
    key, full, saved, saving, error, stored: slots.stored(), plain, setPlain, asking, unlocking, forcePass,
    savedToFile: !!doc && fileDoc === doc,
    switching: slots.switching,
    dropForce: () => setForcePass(null),
    unlock: (pass) => unlockWith(pass, false),
    open: (pass) => unlockWith(pass, true),
    force: (pass) => unlockWith(pass, false, true),
    ask, saveFile, loadFile, openSlot, adopt, removeSlot, vetSlot,
    summaryShown: (d) => gate.current(d),
    // a game's own text shows only once the engine has checked it
    rows: slots.rows().map((r) => {
      const ok = r.status === 'ok';
      return {
        id: r.id, kept: r.kept, removable: r.removable, status: r.status,
        name: ok ? r.doc.name || '' : r.status === 'blocked' ? t('saves.row.blocked') : t('saves.row.unchecked'),
        count: responsesOf(r.doc), summary: ok ? gate.current(r.doc) : '',
      };
    }),
    lockedCount: slots.lockedCount(),
    // starts a new game's switch: the switch, or null while another one is under way
    leave: () => {
      const sw = slots.keep(docRef.current);
      if (sw === 'busy') return null;
      setSaved(false);
      setSaving(false);
      setFull(false);
      bump();
      return sw;
    },
    settle: (sw) => { slots.settle(sw); bump(); },
    land: (gid, d) => { slots.markVetted(d); slots.land(gid, d); bump(); },
    track: (d) => { slots.markVetted(d); docRef.current = d; },
    slotNow: () => slots.slotNow(),
    reveal: () => setAsking((n) => n + 1),
  };
}

const mono = "'IBM Plex Mono', ui-monospace, Menlo, monospace";
const note = { fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 12.5, lineHeight: 1.5, color: 'rgba(237,235,230,0.55)', margin: '6px 0' };

const NOTES = { 'saves.armed': () => t('saves.armed'), 'saves.on': () => t('saves.on'), 'saves.saving': () => t('saves.saving'), 'saves.found': () => t('saves.found'), 'saves.off': () => t('saves.off') };

export function SavesSection({ s, doc, onUndo, onDone = () => {} }) {
  const [pass, setPass] = React.useState('');
  const found = s.stored && !s.key;
  return (
    <div data-testid="saves" style={{ paddingBottom: 12 }}>
      <div style={note} data-testid="saves-note">{NOTES[noteKey({ ...s, hasDoc: !!doc })]()}</div>
      {s.key && s.lockedCount > 0 && <div style={note} data-testid="saves-locked">{t('saves.locked')}</div>}
      <input className="co-field" type="password" autoComplete="off" data-testid="save-passphrase"
        aria-label={t('saves.passphrase.label')} placeholder={t('saves.passphrase.label')}
        value={pass} onChange={(e) => { setPass(e.target.value); s.dropForce(); }} />
      <div style={note}>{t('saves.passphrase.hint')}</div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {found && doc && <button className="co-btn" data-testid="save-open" disabled={s.unlocking || s.switching} onClick={async () => { if (await s.open(pass)) onDone(t('toast.saves.autosave_on')); }}>{t('saves.open')}</button>}
        <button className="co-btn" data-testid="save-unlock" disabled={s.unlocking || s.switching} onClick={async () => { if (await s.unlock(pass)) onDone(t('toast.saves.autosave_on')); }}>
          {found && !doc ? t('saves.open') : t('saves.start')}
        </button>
        {s.forcePass !== null && s.forcePass === pass && (
          <button className="co-btn quiet" data-testid="save-new-passphrase" disabled={s.unlocking || s.switching} onClick={async () => { if (await s.force(pass)) onDone(t('toast.saves.autosave_on')); }}>{t('saves.new_passphrase')}</button>
        )}
      </div>
      <div style={{ height: 12 }}></div>
      <label style={{ ...note, display: 'flex', alignItems: 'center', gap: 8, fontStyle: 'normal' }}>
        <input type="checkbox" data-testid="save-plain" checked={s.plain} onChange={(e) => s.setPlain(e.target.checked)} />
        <span>{t('saves.plain')}</span>
      </label>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 6 }}>
        <button className="co-btn" data-testid="save-file" disabled={!doc} onClick={async () => { if (['plain', 'encrypted'].includes(await s.saveFile(s.plain))) onDone(t('toast.saves.file')); }}>{t('saves.save_file')}</button>
        <button className="co-btn quiet" data-testid="undo" disabled={!doc || !doc.turn || s.switching} onClick={onUndo}>{t('saves.undo')}</button>
      </div>
      <div style={{ ...note, fontFamily: mono, fontStyle: 'normal', marginTop: 12 }}>{t('saves.load_file')}</div>
      <input type="file" accept=".json,application/json" data-testid="load-file" disabled={s.switching} style={{ color: 'rgba(237,235,230,0.7)', fontSize: 12 }}
        onChange={(e) => { s.loadFile(e.target.files[0], pass); e.target.value = ''; }} />
      {s.error && <div role="alert" data-testid="saves-error" style={{ ...note, color: '#e05252', fontStyle: 'normal' }}>{s.error}</div>}
    </div>
  );
}
