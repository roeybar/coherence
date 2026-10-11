import React from 'react';
import { t } from './copy.js';
import { createClient, errorText, gateOf, DEFAULT_INTERVAL } from './api/client.js';
import { KeysSection } from './settings/keys.jsx';
import { SavesSection, useSaves, responsesOf } from './saves.jsx';
import { knownPlaces } from './places.js';
import { npcCards } from './npcs.js';
import { itemIcon } from './itemIcons.js';
import { pickAlert, milestone, shudders, maintCopy, blockedKey, opensSettings, newGameKey } from './alerts.js';
import { loadPrefs, savePrefs } from './prefs.js';
import { actionsFor, turnOf, forkDoc } from './turnactions.js';
import { linkStatus, statusText } from './status.js';
import { period, nextLeft, downAfter, DOWN_SECONDS } from './healthPoll.js';
import { shouldWarnOnLeave } from './saveStatus.js';
import { mergeAnswers } from './gatechain.js';

function CircleButton({ label, children, onClick, lit, pressable, red, shake, disabled }) {
  return (
    <button aria-label={label} onClick={onClick} disabled={disabled}
      className={'orb-btn ' + (red ? 'orb-red' : lit ? 'orb-teal' : 'orb-gunmetal') + (pressable ? ' pressable' : '') + (shake ? ' co-shake' : '')}>
      {children}
    </button>
  );
}

// + tray - turn-composition helpers (dice/craft rolls are the GM's call, not here)
const TRAY_ACTIONS = [
  { id: 'suggest', label: 'Suggested move', kind: 'cycle', glyph: (
    <svg width="18" height="18" viewBox="0 0 16 16">
      <path d="M3 4.5h10M3 8h6.5M3 11.5h10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" fill="none"></path>
    </svg>
  )},
  { id: 'ask', label: 'Ask GameMaster', kind: 'prefix', text: '@GameMaster ', glyph: (
    <svg width="21" height="21" viewBox="0 0 16 16">
      <path d="M5.6 5.8a2.4 2.4 0 1 1 3.3 2.2c-.8.35-1.3 1-1.3 1.9v.3" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" fill="none"></path>
      <circle cx="7.7" cy="13" r="0.9" fill="currentColor"></circle>
    </svg>
  )},
  { id: 'quote', label: 'Quote pair', kind: 'wrap', open: '"', close: '"', glyph: (
    <svg width="22" height="22" viewBox="0 0 16 16">
      {/* drawn as paths (not font glyphs) so they sit centered on every phone */}
      <g fill="currentColor">
        <path transform="translate(2.6,4.4)" d="M2.2 0a2.2 2.2 0 0 0 0 4.4c.15 0 .3-.01.44-.04C2.3 6 1.4 7.1 0 7.8l.6 1.2C3.3 7.9 4.4 5.9 4.4 3.4V2.2A2.2 2.2 0 0 0 2.2 0Z"></path>
        <path transform="translate(8.6,4.4)" d="M2.2 0a2.2 2.2 0 0 0 0 4.4c.15 0 .3-.01.44-.04C2.3 6 1.4 7.1 0 7.8l.6 1.2C3.3 7.9 4.4 5.9 4.4 3.4V2.2A2.2 2.2 0 0 0 2.2 0Z"></path>
      </g>
    </svg>
  )},
  { id: 'secret', label: 'Secret intention', kind: 'wrap', open: '[', close: ']', glyph: (
    <svg width="18" height="18" viewBox="0 0 16 16">
      <path d="M6 2.5H3.5v11H6M10 2.5h2.5v11H10" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
    </svg>
  )},
  { id: 'npc', label: 'Address NPC', kind: 'npc-pick', glyph: (
    <svg width="18" height="18" viewBox="0 0 16 16">
      <circle cx="8" cy="5.4" r="2.6" stroke="currentColor" strokeWidth="1.6" fill="none"></circle>
      <path d="M3.4 13.6a4.6 4.6 0 0 1 9.2 0" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" fill="none"></path>
    </svg>
  )},
];

// dice with a drawn face; any other die falls back to its name
const DICE_FACES = new Set([4, 6, 8, 10, 12, 20, 100, 'inf']);

function Composer({ onSend, gate, onResolve, onRephrase, suggestions, addressable, busy, alert, compose, onCompose, onCancelCompose, maint, shake, onMaintSend, submitKey, switching }) {
  const [draft, setDraft] = React.useState('');
  const [trayOpen, setTrayOpen] = React.useState(false);
  const [npcPick, setNpcPick] = React.useState(false); // @-address picker
  const taRef = React.useRef(null);
  const update = setDraft; // the draft lives in memory only, never in browser storage

  // grow with the draft up to a third of the screen, then scroll inside
  React.useLayoutEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = '0px';
    ta.style.height = Math.min(ta.scrollHeight, 270) + 'px'; /* 874 x 33% ~ 288 box */
  }, [draft]);

  // the Suggested-move sphere cycles through state.suggested_moves from the
  // last response, replacing the draft on each tap
  const suggestIdx = React.useRef(0);
  const npcAnchor = React.useRef(null);
  // insert text at the caret (replacing any selection) so tray actions never
  // clobber a turn already in progress; caret lands after the inserted text
  const insertAtCursor = (text) => {
    const ta = taRef.current;
    const start = (ta && ta.selectionStart != null) ? ta.selectionStart : draft.length;
    const end = (ta && ta.selectionEnd != null) ? ta.selectionEnd : draft.length;
    const next = draft.slice(0, start) + text + draft.slice(end);
    update(next);
    const pos = start + text.length;
    setTimeout(() => { const t = taRef.current; if (t) { t.focus(); t.setSelectionRange(pos, pos); } }, 0);
  };
  const trayAct = (a, ev) => {
    const ta = taRef.current;
    // burst ring on a document-level layer - never clipped by the pill
    if (ev && ev.currentTarget) {
      const r = ev.currentTarget.getBoundingClientRect();
      const s = document.createElement('span');
      s.className = 'co-burst-free';
      s.style.left = r.left + 'px';
      s.style.top = r.top + 'px';
      s.style.width = r.width + 'px';
      s.style.height = r.height + 'px';
      document.body.appendChild(s);
      setTimeout(() => s.remove(), 650);
    }
    if (a.kind === 'cycle') {
      if (suggestions.length) {
        update(suggestions[suggestIdx.current % suggestions.length]);
        suggestIdx.current += 1;
      }
    } else if (a.kind === 'prefix') {
      insertAtCursor(a.text);
    } else if (a.kind === 'npc-pick') {
      // drop an "@" at the caret (unless one is already sitting right before it)
      const pos = (ta && ta.selectionStart != null) ? ta.selectionStart : draft.length;
      if (draft.slice(0, pos).endsWith('@')) { npcAnchor.current = pos; }
      else { insertAtCursor('@'); npcAnchor.current = pos + 1; }
      setNpcPick(true);
      setTrayOpen(false);
    } else if (a.kind === 'wrap') {
      const pos = (ta && ta.selectionStart != null) ? ta.selectionStart : draft.length;
      const next = draft.slice(0, pos) + a.open + a.close + draft.slice(pos);
      update(next);
      setTimeout(() => { if (ta) { ta.focus(); ta.setSelectionRange(pos + 1, pos + 1); } }, 0);
      return;
    }
    setTimeout(() => { if (ta) ta.focus(); }, 0);
  };

  const pickAddressee = (name) => {
    // replace from the "@" anchor to the caret with "@Name " - keeps the rest
    // of the player's turn intact instead of appending to the tail
    const ta = taRef.current;
    const anchor = (npcAnchor.current != null) ? npcAnchor.current : draft.length;
    const caret = (ta && ta.selectionStart != null) ? ta.selectionStart : draft.length;
    const before = draft.slice(0, Math.max(0, anchor - 1)); // drop the bare "@"
    const after = draft.slice(Math.max(caret, anchor));
    const ins = '@' + name + ' ';
    const next = before + ins + after;
    update(next);
    const newPos = before.length + ins.length;
    setNpcPick(false);
    npcAnchor.current = null;
    setTimeout(() => { const t = taRef.current; if (t) { t.focus(); t.setSelectionRange(newPos, newPos); } }, 0);
  };

  const canSend = draft.trim().length > 0 && !busy && !maint;

  // a turn that errors or opens a gate keeps the draft
  const send = async () => {
    // maintenance: refuse with a shake, the draft stays
    if (maint) { onMaintSend(); return; }
    if (!canSend) return;
    const ok = compose ? await onCompose(draft.trim()) : await onSend(draft.trim());
    if (ok) setDraft('');
  };

  const resolveGate = async (opt) => {
    if (busy || switching) return;
    if (await onResolve(opt)) setDraft('');
  };

  return (
    <div data-screen-label="Composer" className={alert === 'grant' ? 'co-grant-flash' : alert === 'xmas' ? 'co-xmas-flash' : undefined} style={{
      display: 'flex', flexDirection: 'column', justifyContent: 'flex-end',
      height: 'auto', minHeight: 56, boxSizing: 'border-box', overflow: 'hidden',
      margin: '0 20px 14px',
      padding: 4,
      borderRadius: 28, /* closed: 56 tall - arcs meet; open: concentric rounded rect */
      border: '2px solid transparent',
      background: 'linear-gradient(#0c1414, #0c1414) padding-box, ' + (alert === 'grant'
        ? 'linear-gradient(180deg, #f0d894, #b8892e)' /* a chapter turns: warm gold outliner (a gift, not an error) */
        : alert === 'xmas'
        ? 'linear-gradient(180deg, #9fe0b0, #2e8a4a)' /* seasonal greeting: evergreen outliner */
        : alert
        ? 'linear-gradient(180deg, #e05252, #8a2525)' /* maintenance and errors: red outliner */
        : 'linear-gradient(180deg, #e8e8ec, #a8adb5 50%, rgb(98, 103, 110))') + ' border-box',
      boxShadow: '0 14px 32px rgba(0,0,0,0.55), 0 4px 12px rgba(0,0,0,0.45), 0 0 24px rgba(64,224,208,0.05)',
      opacity: 1, /* stays solid during send - dimming read as disabled/confusing */
      transition: 'opacity 0.18s ease, height 0.32s cubic-bezier(0.34, 1.2, 0.64, 1)',
    }}>
      {/* gate - the clarification lives in the composer, no popup */}
      <div aria-hidden={!gate} style={{
        flexShrink: 0,
        maxHeight: gate ? 264 : 0, marginBottom: gate ? 12 : 0, overflow: 'hidden',
        opacity: gate ? 1 : 0,
        pointerEvents: gate ? 'auto' : 'none',
        transition: 'max-height 0.32s cubic-bezier(0.34, 1.2, 0.64, 1), margin-bottom 0.32s cubic-bezier(0.34, 1.2, 0.64, 1), opacity 0.2s ease',
      }}>
        {gate && (
          <div style={{ padding: '8px 10px 0', textAlign: 'left' }}>
            <div style={{
              fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
              fontSize: 9.5, letterSpacing: '0.12em', textTransform: 'uppercase',
              color: '#a8adb5', marginBottom: 6,
            }}>GameMaster - clarification</div>
            <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 16, lineHeight: 1.4, color: '#d4c07a' }}>{gate.title}</div>
            {draft.trim().length > 0 && (
              <div style={{
                fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 13, lineHeight: 1.5,
                color: 'rgba(64,224,208,0.75)', marginTop: 5,
                display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
              }}>{'"' + draft.trim() + '"'}</div>
            )}
            <div style={{
              display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 10, marginTop: 10,
              ...(gate.die ? { flexDirection: 'column', width: '100%' } : {}),
            }}>
              {gate.die && (
                <button aria-label={'Roll d' + gate.die} title={'Roll d' + gate.die} disabled={busy || switching} onClick={() => resolveGate({})} style={{
                  width: 92, height: 92, borderRadius: 46, flexShrink: 0,
                  border: '3px solid transparent', cursor: 'pointer', padding: 0,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  background:
                    'radial-gradient(circle at 50% 94%, #5df2df 0%, rgba(64,224,208,0.5) 42%, rgba(64,224,208,0) 75%) padding-box,' +
                    'radial-gradient(circle at 50% 32%, #38b7a8 0%, rgb(21, 128, 122) 55%, #0a4a46 100%) padding-box,' +
                    'linear-gradient(180deg, #ecd393, #b8944c 50%, #7d6128) border-box',
                  boxShadow: '0 12px 26px rgba(0,0,0,0.55), 0 0 22px rgba(64,224,208,0.12)',
                }}>
                  {DICE_FACES.has(gate.die)
                    ? <img src={'/dice/d' + gate.die + '.svg'} width="58" height="58" alt={'d' + gate.die} style={{ display: 'block', filter: 'drop-shadow(0 1px 2px rgba(0,0,0,0.6))' }} />
                    : <span style={{ fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace", fontSize: 17, color: '#e8fffb' }}>{'d' + gate.die}</span>}
                </button>
              )}
              {gate.options.map((o) => (
                <span key={o.label} role="button" aria-disabled={busy || switching ? true : undefined}
                  onClick={() => { if (!(busy || switching)) resolveGate(o); }} style={{
                  fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
                  fontSize: 9.5, letterSpacing: '0.08em', textTransform: 'uppercase',
                  padding: '6px 12px', borderRadius: 999, cursor: 'pointer',
                  color: '#0c1414', background: '#40E0D0',
                }}>{o.label}</span>
              ))}
              <span role="button" onClick={onRephrase} style={{
                fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
                fontSize: 9.5, letterSpacing: '0.08em', textTransform: 'uppercase',
                padding: '6px 12px', borderRadius: 999, cursor: 'pointer',
                color: 'rgba(237,235,230,0.6)', background: 'rgba(255,255,255,0.06)',
                border: '1px solid rgba(232,232,236,0.16)',
              }}>Rephrase</span>
            </div>
          </div>
        )}
      </div>
      {/* feedback / flag - the textbox itself changes, no popup */}
      <div aria-hidden={!compose} style={{
        flexShrink: 0,
        maxHeight: compose ? 32 : 0, marginBottom: compose ? 8 : 0, overflow: 'hidden',
        opacity: compose ? 1 : 0,
        pointerEvents: compose ? 'auto' : 'none',
        transition: 'max-height 0.28s cubic-bezier(0.34, 1.2, 0.64, 1), margin-bottom 0.28s cubic-bezier(0.34, 1.2, 0.64, 1), opacity 0.18s ease',
      }}>
        {compose && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px 0' }}>
            <span data-testid="compose-mode" style={{
              fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
              fontSize: 9.5, letterSpacing: '0.1em', textTransform: 'uppercase',
              color: compose.kind === 'flag' ? 'hsl(25, 70%, 62%)' : '#40E0D0',
            }}>{compose.kind === 'flag' ? <Icon name="flag" label="turn.flag" size={10} /> : <Icon name="comment" label="turn.comment" size={10} />}{' ' + (compose.kind === 'flag' ? t('composer.mode.flag') : t('composer.mode.comment')) + ' | ' + t('composer.mode.turn', { who: compose.who })}</span>
            <span role="button" aria-label={t('composer.mode.cancel')} onClick={onCancelCompose} style={{
              marginInlineStart: 'auto', cursor: 'pointer', color: 'rgba(237,235,230,0.5)',
              fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace", fontSize: 11, padding: '0 4px',
            }}>{'x'}</span>
          </div>
        )}
      </div>
      {/* tray - morphs in above the writeable row; reverses order in RTL */}
      <div aria-hidden={!trayOpen} style={{
        display: 'flex', gap: 12, justifyContent: 'center',
        marginBottom: trayOpen ? 12 : 0,
        maxHeight: trayOpen ? 100 : 0,
        overflow: trayOpen ? 'visible' : 'hidden',
        flexShrink: 0,
        transition: 'max-height 0.3s cubic-bezier(0.34, 1.2, 0.64, 1), margin-bottom 0.3s cubic-bezier(0.34, 1.2, 0.64, 1)',
        pointerEvents: trayOpen ? 'auto' : 'none',
      }}>
        {TRAY_ACTIONS.map((a, i) => (
          <div key={a.id} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5, width: 58 }}>
            <button aria-label={a.label} title={a.label}
              onClick={(e) => trayAct(a, e)}
              className="orb-btn orb-gunmetal pressable"
              style={{
                width: 48, height: 48,
                opacity: trayOpen ? 1 : 0,
                transform: trayOpen ? 'translateY(0) scale(1)' : 'translateY(10px) scale(0.6)',
                transition: 'opacity 0.22s ease, transform 0.34s cubic-bezier(0.34, 1.56, 0.64, 1)',
                transitionDelay: trayOpen ? (i * 40) + 'ms' : '0ms',
              }}>
              {a.glyph}
            </button>
            <span style={{ fontSize: 10.5, lineHeight: 1.25, color: 'rgba(237,235,230,0.6)', textAlign: 'center' }}>{a.label}</span>
          </div>
        ))}
      </div>
      {/* @-address picker - GameMaster first + the NPCs from the last turn */}
      <div aria-hidden={!npcPick} style={{
        flexShrink: 0,
        maxHeight: npcPick ? 120 : 0, marginBottom: npcPick ? 10 : 0, overflow: 'hidden',
        opacity: npcPick ? 1 : 0,
        pointerEvents: npcPick ? 'auto' : 'none',
        transition: 'max-height 0.28s cubic-bezier(0.34, 1.2, 0.64, 1), margin-bottom 0.28s cubic-bezier(0.34, 1.2, 0.64, 1), opacity 0.18s ease',
      }}>
        <div style={{ padding: '8px 10px 0' }}>
          <div style={{
            fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
            fontSize: 9.5, letterSpacing: '0.12em', textTransform: 'uppercase',
            color: 'rgba(237,235,230,0.45)', marginBottom: 7,
          }}>Addressing</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {addressable.map((n, idx) => (
              <span key={n} role="button" onClick={() => pickAddressee(n)} style={{
                fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
                fontSize: 9.5, letterSpacing: '0.08em', textTransform: 'uppercase',
                padding: '6px 12px', borderRadius: 999, cursor: 'pointer',
                color: idx === 0 ? '#0c1414' : 'rgba(237,235,230,0.7)',
                background: idx === 0 ? CONSTRUCTION_YELLOW : 'rgba(255,255,255,0.06)',
                border: idx === 0 ? '1px solid transparent' : '1px solid rgba(232,232,236,0.16)',
              }}>{n}</span>
            ))}
          </div>
        </div>
      </div>
      {/* writeable row - pinned to the bottom; swaps sides in RTL */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8 }}>
      <CircleButton label="Actions" lit={!trayOpen} pressable onClick={() => setTrayOpen(o => !o)}>
        <svg width="16" height="16" viewBox="0 0 16 16" style={{
          transform: trayOpen ? 'rotate(45deg)' : 'rotate(0deg)',
          transition: 'transform 0.28s cubic-bezier(0.34, 1.2, 0.64, 1)',
        }}>
          <path d="M8 2v12M2 8h12" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"></path>
        </svg>
      </CircleButton>
      <textarea
        ref={taRef}
        rows="1"
        className="npc-scroll"
        value={draft}
        onChange={(e) => update(e.target.value)}
        onKeyDown={(e) => {
          // the Submit key setting decides which of Enter and Shift+Enter sends; the other adds a line
          if (e.key === 'Enter' && e.shiftKey === (submitKey === 'shift_enter')) { e.preventDefault(); send(); }
        }}
        placeholder={maint ? t('composer.placeholder.maint') : compose ? t(compose.kind === 'flag' ? 'composer.placeholder.flag' : 'composer.placeholder.comment') : t('composer.placeholder')}
        disabled={maint}
        style={{
          flex: 1, resize: 'none', border: 'none', outline: 'none',
          boxSizing: 'border-box',
          overflowY: draft ? 'auto' : 'hidden', /* placeholder never scrolls - keeps the cool slider for drafts only */
          background: 'transparent',
          color: '#EDEBE6',
          fontFamily: 'inherit', fontSize: 16, lineHeight: '22px',
          padding: '11px 4px', margin: 0,
          caretColor: '#40E0D0',
        }}
      ></textarea>
      <CircleButton label="Send" onClick={send} lit={canSend} red={maint} shake={shake} disabled={switching}>
        <svg width="16" height="16" viewBox="0 0 16 16">
          <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
        </svg>
      </CircleButton>
      </div>
    </div>
  );
}
function DuoButton({ onMore, moreOpen, onMenu, menuOpen, logo }) {
  // infinity duo - two orbs filling their gold rings (no moat), rings just barely
  // interlocking at the waist; the right ring passes over the left
  const w = 94, h = 50, cy = 25;
  const cx1 = 25, cx2 = 69;
  const orbR = 19.2;
  const btn = (side) => ({
    position: 'absolute', [side]: 0, top: 0, width: 50, height: 50,
    background: 'none', border: 'none', cursor: 'pointer', padding: 0,
  });
  // three strokes: C (left ring, open at the waist), mirrored C (right ring),
  // and an integral joining left-bottom to right-top; the C's far ends tuck under it
  const arcL = 'M45.8,22.1 A21,21 0 1 0 42.2,37';
  const arcR = 'M51.8,13 A21,21 0 1 1 48.2,27.9';
  const integralS = 'M42.2,37 C46.8,30.4 47.2,19.6 51.8,13';
  const bandStack = (d, casing) => (
    <g>
      {casing && <path d={d} fill="none" stroke="#0c0e0e" strokeWidth="6.8" strokeLinecap="round"></path>}
      <path d={d} fill="none" stroke="rgb(51, 54, 59)" strokeWidth="4.8"></path>
      <path d={d} fill="none" stroke="url(#metalBand)" strokeWidth="3.2"></path>
    </g>
  );
  const orb = (x) => (
    <g>
      <circle cx={x} cy={cy} r={orbR} fill="url(#orbBase)"></circle>
      <circle cx={x} cy={cy} r={orbR} fill="url(#orbGlow)"></circle>
      <circle cx={x} cy={cy} r={orbR} fill="none" stroke="rgb(6, 48, 44)" strokeWidth="1.4"></circle>
      <circle cx={x} cy={cy} r={orbR - 1.3} fill="none" stroke="rgba(120,255,235,0.35)" strokeWidth="0.9"></circle>
      <path d={`M${x - 16},${cy - 4.5} a16,16 0 0,1 32,0 a22,9 0 0,1 -32,0`} fill="url(#orbGloss)"></path>
    </g>
  );
  return (
    <div data-screen-label="Duo button" style={{ position: 'relative', width: w, height: h, pointerEvents: 'auto' }}>
      <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ position: 'absolute', inset: 0, overflow: 'visible', opacity: menuOpen ? 0 : 1, transition: menuOpen ? 'opacity 0.25s ease' : 'opacity 0.2s ease 0.48s' /* wait for the sphere to land back */ }}>
        <defs>
          <radialGradient id="orbBase" cx="50%" cy="35%" r="80%">
            <stop offset="0%" stopColor="#38b7a8"></stop>
            <stop offset="55%" stopColor="rgb(21, 128, 122)"></stop>
            <stop offset="100%" stopColor="#0a4a46"></stop>
          </radialGradient>
          <radialGradient id="orbGlow" cx="50%" cy="94%" r="62%">
            <stop offset="0%" stopColor="#5df2df"></stop>
            <stop offset="45%" stopColor="rgba(64,224,208,0.5)"></stop>
            <stop offset="80%" stopColor="rgba(64,224,208,0)"></stop>
          </radialGradient>
          <linearGradient id="orbGloss" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgba(255,255,255,0.38)"></stop>
            <stop offset="100%" stopColor="rgba(255,255,255,0.02)"></stop>
          </linearGradient>
          <linearGradient id="metalBand" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#e8e8ec"></stop>
            <stop offset="50%" stopColor="#a8adb5"></stop>
            <stop offset="100%" stopColor="rgb(98, 103, 110)"></stop>
          </linearGradient>
          <filter id="orbBlur" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation="1.6"></feGaussianBlur>
          </filter>
        </defs>
        {/* ground shadows */}
        <ellipse cx={cx1} cy="47" rx="15" ry="2.6" fill="black" opacity="0.5" filter="url(#orbBlur)"></ellipse>
        <ellipse cx={cx2} cy="47" rx="15" ry="2.6" fill="black" opacity="0.5" filter="url(#orbBlur)"></ellipse>
        {/* C + mirrored C, then orbs, then the integral crossing on top */}
        {bandStack(arcL)}
        {bandStack(arcR)}
        {orb(cx1)}
        {orb(cx2)}
        {bandStack(integralS)}
        {/* glyphs */}
        {!logo && <path d="M19.5 20.2h11M19.5 25h11M19.5 29.8h11" stroke="rgba(255,255,255,0.92)" strokeWidth="1.7" strokeLinecap="round" fill="none"></path>}
        {!logo && <circle cx="64.5" cy={cy} r="1.4" fill="rgba(255,255,255,0.92)"></circle>}
        {!logo && <circle cx="69" cy={cy} r="1.4" fill="rgba(255,255,255,0.92)"></circle>}
        {!logo && <circle cx="73.5" cy={cy} r="1.4" fill="rgba(255,255,255,0.92)"></circle>}
      </svg>
      {!logo && <button aria-label="Menu" aria-expanded={!!menuOpen} onClick={onMenu} style={btn('left')}></button>}
      {!logo && <button aria-label="More" aria-expanded={!!moreOpen} onClick={onMore} disabled={!!menuOpen} style={btn('right')}></button>}
    </div>
  );
}

// speech palette
const VOICES_KEY = 'coherence-v2-voices';
// hue (deg) + brightness (lightness); saturation fixed so everything stays on-theme
const VOICE_SAT = 0.55;
const VOICE_DEFAULTS = {
  dm:     { h: 46,  l: 0.66 },
  player: { h: 174, l: 0.56 },
  npc:    { h: 268, l: 0.78 },
};

// hsl -> hex, for the brightness fader
function hslToHex(h, s, l) {
  const f = (p, q, t) => {
    if (t < 0) t += 1; if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const to = (x) => Math.round(f(p, q, x) * 255).toString(16).padStart(2, '0');
  return '#' + to(h + 1 / 3) + to(h) + to(h - 1 / 3);
}
const voiceColor = (v) => hslToHex(v.h / 360, VOICE_SAT, v.l);
const RAINBOW = 'linear-gradient(90deg, hsl(0,55%,55%), hsl(60,55%,55%), hsl(120,55%,55%), hsl(180,55%,55%), hsl(240,55%,55%), hsl(300,55%,55%), hsl(360,55%,55%))';

const npcHue = (h) => `hsl(${h}, 65%, 70%)`;

// horizontal fader - gunmetal sphere thumb on a silver-edged track
function HSlider({ value, onChange, track }) {
  const ref = React.useRef(null);
  const set = (clientX) => {
    const r = ref.current.getBoundingClientRect();
    onChange(Math.max(0, Math.min(1, (clientX - r.left) / r.width)));
  };
  const down = (e) => {
    e.preventDefault(); set(e.clientX);
    const mv = (ev) => set(ev.clientX);
    const up = () => { window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
  };
  return (
    <div ref={ref} onPointerDown={down} style={{ position: 'relative', flex: 1, height: 22, cursor: 'pointer', touchAction: 'none' }}>
      <div style={{ position: 'absolute', left: 0, right: 0, top: 7.5, height: 7, borderRadius: 3.5, background: track, boxShadow: 'inset 0 0 0 1px rgba(232,232,236,0.22)' }}></div>
      <div style={{
        position: 'absolute', top: 3, left: `calc(${value * 100}% - 8px)`, width: 16, height: 16, borderRadius: 8,
        background: 'radial-gradient(circle at 50% 30%, #6b7076 0%, rgb(67, 71, 77) 55%, rgb(38, 41, 46) 100%)',
        boxShadow: '0 0 0 1.5px #9ba1a9',
      }}></div>
    </div>
  );
}

// split narration into plain, quoted and [secret] runs
const SEG_RE = /(\[[^\]]*\]|"[^"]*"|\u201c[^\u201d]*\u201d)/;
const segments = (text) => String(text).split(SEG_RE).filter(Boolean).map((s) => ({
  text: s, secret: s[0] === '[' && s.endsWith(']'), quote: s[0] === '"' || s[0] === '\u201c',
}));

// narrative blocks - adapted from old Coherence's ink-bleed treatment
function InkBleed({ children, size = { x: 80, y: 50 } }) {
  return (
    <div style={{ position: 'relative' }}>
      <div aria-hidden="true" style={{
        position: 'absolute',
        inset: `${-size.y - 6}px ${-size.x - 12}px ${-size.y + 8}px ${-size.x - 12}px`,
        background: 'radial-gradient(60% 70% at 45% 55%, rgba(19,32,30,0.92), rgba(19,32,30,0.45) 55%, transparent 82%)',
        filter: 'blur(14px)', pointerEvents: 'none',
      }}></div>
      <div style={{ position: 'relative' }}>{children}</div>
    </div>
  );
}

const blockLabel = {
  fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
  fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase',
  marginBottom: 6,
};

// typewriter - DM streams at ~28 cps, punctuation pauses, reduced-motion jumps
function TypeText({ text, cps = 60, onGrow }) {
  const [n, setN] = React.useState(() => {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches ? text.length : 0; } catch { return 0; }
  });
  React.useEffect(() => {
    if (n >= text.length) return;
    const prev = text[n - 1] || '';
    const delay = /[.!?\u2026\u2014]/.test(prev) ? 140 : 1000 / cps;
    const t = setTimeout(() => { setN(n + 1); if (onGrow) onGrow(); }, delay);
    return () => clearTimeout(t);
  }, [n, text]);
  return <span>{text.slice(0, n)}{n < text.length && <span className="co-caret" aria-hidden="true"></span>}</span>;
}

function DmBlock({ label, body, colors, stream, onGrow }) {
  return (
    <InkBleed size={{ x: 90, y: 60 }}>
      <div style={{ ...blockLabel, color: '#a8adb5' }}>{label}</div>
      <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 19, lineHeight: 1.55, color: colors.dm }}>
        {stream ? <TypeText text={body} onGrow={onGrow} /> : segments(body).map((s, i) => <span key={i} style={s.quote ? { color: colors.npc } : undefined}>{s.text}</span>)}
      </div>
    </InkBleed>
  );
}

const CONSTRUCTION_YELLOW = '#f2c230'; // ask-the-GM lane - distinct default

function PlayerBlock({ label, body, playerColor, revealed }) {
  const c = playerColor || '#40E0D0';
  const segs = segments(body).map((s, i) => s.secret
    ? <span key={i} className="co-secret">{s.text}</span>
    : s.quote
      ? <span key={i} style={{ color: c }}>{s.text}</span>
      : <React.Fragment key={i}>{s.text}</React.Fragment>);
  return (
    <div className={'co-player-bubble' + (revealed ? ' revealed' : '')} style={{
      borderRadius: 16,
      border: '1px solid transparent',
      background: `linear-gradient(${'#0c1414'}, ${'#0c1414'}) padding-box, linear-gradient(180deg, #e8e8ec, #a8adb5 50%, rgb(98, 103, 110)) border-box`,
      padding: '10px 14px 12px',
    }}>
      <div style={{ ...blockLabel, color: c + '99' }}>{label}</div>
      <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 16.5, lineHeight: 1.6, color: 'rgba(237,235,230,0.72)' }}>{segs}</div>
    </div>
  );
}

// ask-the-GameMaster lane - construction yellow, out-of-world
function AskBlock({ label, body }) {
  return (
    <div style={{
      borderRadius: 16,
      border: `1px dashed ${CONSTRUCTION_YELLOW}66`,
      padding: '10px 14px 12px',
    }}>
      <div style={{ ...blockLabel, color: CONSTRUCTION_YELLOW + 'B3' }}>{label}</div>
      <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 15.5, lineHeight: 1.55, color: CONSTRUCTION_YELLOW + 'D9' }}>{body}</div>
    </div>
  );
}

function GmAsideBlock({ label, body }) {
  return (
    <InkBleed size={{ x: 70, y: 50 }}>
      <div style={{ ...blockLabel, color: CONSTRUCTION_YELLOW + 'B3' }}>{label}</div>
      <div style={{
        fontFamily: "'Newsreader', Georgia, serif", fontSize: 17, lineHeight: 1.55,
        color: CONSTRUCTION_YELLOW,
        borderInlineStart: `2px dashed ${CONSTRUCTION_YELLOW}59`, paddingInlineStart: 12,
      }}>{body}</div>
    </InkBleed>
  );
}

// character shaping - optional, world-agnostic: appearance archetype, detail.
// backend: rides along in the save-creation payload as `character`; every field
// optional - absent means "let the GameMaster improvise".
const CHAR_ARCHETYPES = [
  { id: 'ash',    name: 'Ash',    hint: 'pale, lean, scarred at the temple' },
  { id: 'briar',  name: 'Briar',  hint: 'tall, copper-skinned, ink at the wrists' },
  { id: 'hollow', name: 'Hollow', hint: 'small, sharp, eyes too quiet' },
  { id: 'cinder', name: 'Cinder', hint: 'broad, sunburnt, slow to laugh' },
];

// inline new-save creator - lives as the last row of the Menu panel. `open`/`onToggle`
// are lifted to MenuPanel so it can minimise the saves list while the form is up.
function NewSaveForm({ menuOpen, open, onToggle, onCreate, packs, creating, gate, failed, onOpenKeys, interval }) {
  const ease = 'cubic-bezier(0.34, 1.2, 0.64, 1)';
  const [world, setWorld] = React.useState(null);
  const [vals, setVals] = React.useState({}); // dynamic field values, keyed by field id
  const [name, setName] = React.useState(''); // save name (distinct from the character name field)
  // optional character shaping - survives a world switch (it's world-agnostic)
  const [charOpen, setCharOpen] = React.useState(false);
  const [arch, setArch] = React.useState(null);
  const [archDetail, setArchDetail] = React.useState('');
  // reset the whole draft whenever the menu (or the form) folds shut
  React.useEffect(() => { if (!menuOpen || !open) { setWorld(null); setVals({}); setName(''); setCharOpen(false); setArch(null); setArchDetail(''); } }, [menuOpen, open]);
  React.useEffect(() => {
    if (menuOpen && open && failed && failed.draft) {
      const d = failed.draft;
      setWorld(d.world); setVals(d.vals); setName(d.name); setCharOpen(d.charOpen);
      setArch(d.arch); setArchDetail(d.archDetail);
    }
  }, [failed, menuOpen, open]);
  const w = world ? packs.find((x) => x.id === world) : null;
  const setVal = (id, v) => setVals((s) => ({ ...s, [id]: v }));
  const chooseWorld = (id) => {
    const wd = packs.find((x) => x.id === id);
    setWorld(id); setVals({}); setName(wd ? wd.name : '');
  };
  // ready when a pack is chosen and every engine-flagged required field has a value
  const ready = !!(w && !creating && vals.char && vals.char.trim());
  const secLabel = { ...blockLabel, fontSize: 9, margin: '13px 0 7px', color: 'rgba(237,235,230,0.4)' };
  const inputStyle = {
    width: '100%', boxSizing: 'border-box', background: 'rgba(255,255,255,0.045)',
    border: '1px solid rgba(232,232,236,0.16)', borderRadius: 12, padding: '10px 13px',
    color: '#EDEBE6', fontFamily: "'Newsreader', Georgia, serif", fontSize: 15, outline: 'none',
  };
  const Chip = ({ active, onClick, children }) => (
    <button onClick={onClick} style={{
      fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
      fontSize: 11, letterSpacing: '0.02em', padding: '7px 12px', borderRadius: 999, cursor: 'pointer',
      background: active ? 'rgba(64,224,208,0.14)' : 'rgba(255,255,255,0.045)',
      border: active ? '1px solid rgba(64,224,208,0.55)' : '1px solid rgba(232,232,236,0.16)',
      color: active ? '#8ff0e6' : 'rgba(237,235,230,0.72)',
      boxShadow: active ? '0 0 10px rgba(64,224,208,0.12)' : 'none',
      transition: 'background 0.2s ease, border-color 0.2s ease, color 0.2s ease, box-shadow 0.2s ease',
    }}>{children}</button>
  );
  // the one required question: the character's name
  const nameField = (
    <div>
      <div style={secLabel}>You are called<span style={{ color: 'rgba(64,224,208,0.6)' }}> *</span></div>
      <input value={vals.char || ''} onChange={(e) => setVal('char', e.target.value)} placeholder="a name for your character" style={inputStyle} />
    </div>
  );
  return (
    <div style={{ borderTop: '1px solid rgba(232,232,236,0.08)' }}>
      <button onClick={onToggle} style={{
        display: 'flex', alignItems: 'center', gap: 9, width: '100%', textAlign: 'start',
        background: 'none', border: 'none', cursor: 'pointer', padding: '13px 4px', boxSizing: 'border-box',
        fontFamily: "'Newsreader', Georgia, serif", fontSize: 16.5, color: '#EDEBE6',
      }}>
        <span style={{
          width: 21, height: 21, borderRadius: 11, flexShrink: 0,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          border: '1px solid rgba(64,224,208,0.45)', background: 'rgba(64,224,208,0.08)',
          transform: open ? 'rotate(135deg)' : 'rotate(0deg)', transition: 'transform 0.32s ' + ease,
        }}>
          <svg width="10" height="10" viewBox="0 0 10 10"><path d="M5 1v8M1 5h8" stroke="#40E0D0" strokeWidth="1.5" strokeLinecap="round"></path></svg>
        </span>
        <span>New save</span>
        <svg width="10" height="10" viewBox="0 0 10 10" style={{ marginInlineStart: 'auto', flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.25s ease' }}>
          <path d="M2 3.5 5 6.5 8 3.5" stroke="rgba(237,235,230,0.5)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
        </svg>
      </button>
      <div style={{ maxHeight: open ? 2200 : 0, overflow: 'hidden', opacity: open ? 1 : 0, transition: 'max-height 0.46s ' + ease + ', opacity 0.3s ease' }}>
        <div style={{ padding: '2px 2px 12px' }}>
          <div style={secLabel}>{world ? 'Your world' : 'Choose a world'}</div>
          {packs.map((wd) => {
            const sel = world === wd.id;
            const hidden = world && !sel; // once a pack is picked, the others fold away
            return (
              <div key={wd.id} style={{ maxHeight: hidden ? 0 : 400, opacity: hidden ? 0 : 1, overflow: 'hidden', transition: 'max-height 0.4s ' + ease + ', opacity 0.26s ease', marginBottom: hidden ? 0 : 9 }}>
                <button onClick={() => chooseWorld(wd.id)} style={{
                  display: 'block', width: '100%', textAlign: 'start', cursor: 'pointer', color: 'inherit', fontFamily: 'inherit',
                  background: sel ? 'rgba(64,224,208,0.06)' : 'rgba(255,255,255,0.03)',
                  border: sel ? '1px solid rgba(64,224,208,0.5)' : '1px solid rgba(232,232,236,0.14)',
                  borderRadius: 14, padding: '12px 14px', boxSizing: 'border-box',
                  boxShadow: sel ? '0 0 14px rgba(64,224,208,0.1)' : 'none',
                  transition: 'background 0.22s ease, border-color 0.22s ease, box-shadow 0.22s ease',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                    <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 16.5, color: '#EDEBE6' }}>{wd.name}</span>
                    <span style={{
                      marginInlineStart: 'auto', width: 19, height: 19, borderRadius: 10, flexShrink: 0,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      border: sel ? 'none' : '1px solid rgba(237,235,230,0.3)',
                      background: sel ? '#40E0D0' : 'transparent', transition: 'background 0.2s ease',
                    }}>
                      {sel && <svg width="11" height="11" viewBox="0 0 11 11"><path d="M2 5.6 4.4 8 9 3" stroke="#0c1414" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" fill="none"></path></svg>}
                    </span>
                  </div>
                  <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 13, lineHeight: 1.5, color: 'rgba(237,235,230,0.6)', marginTop: 5 }}>{wd.description}</div>
                </button>
              </div>
            );
          })}
          {world && (
            <button onClick={() => { setWorld(null); setVals({}); }} style={{
              background: 'none', border: 'none', cursor: 'pointer', padding: '2px 2px 0',
              fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace", fontSize: 10, letterSpacing: '0.06em',
              textTransform: 'uppercase', color: 'rgba(64,224,208,0.7)',
            }}>{'<- Choose a different world'}</button>
          )}
          <div style={{ maxHeight: w ? 2000 : 0, overflow: 'hidden', opacity: w ? 1 : 0, transition: 'max-height 0.44s ' + ease + ', opacity 0.3s ease 0.06s' }}>
            {/* engine-driven questions for this pack */}
            {w && nameField}
            {/* optional character shaping - collapsed by default, everything inside is skippable */}
            <div style={{
              marginTop: 15, borderRadius: 14, boxSizing: 'border-box',
              border: charOpen ? '1px solid rgba(232,232,236,0.2)' : '1px dashed rgba(232,232,236,0.18)',
              background: charOpen ? 'rgba(255,255,255,0.028)' : 'transparent',
              transition: 'border-color 0.25s ease, background 0.25s ease',
            }}>
              <button onClick={() => setCharOpen((v) => !v)} style={{
                display: 'flex', alignItems: 'center', gap: 9, width: '100%', textAlign: 'start',
                background: 'none', border: 'none', cursor: 'pointer', padding: '11px 13px', boxSizing: 'border-box',
                fontFamily: "'Newsreader', Georgia, serif", fontSize: 15.5, color: '#EDEBE6',
              }}>
                <svg width="15" height="15" viewBox="0 0 15 15" style={{ flexShrink: 0, opacity: 0.75 }}>
                  <circle cx="7.5" cy="5" r="2.6" fill="none" stroke="#40E0D0" strokeWidth="1.3"></circle>
                  <path d="M2.5 13.2c0.8-3 2.7-4.5 5-4.5s4.2 1.5 5 4.5" fill="none" stroke="#40E0D0" strokeWidth="1.3" strokeLinecap="round"></path>
                </svg>
                <span>Shape your character</span>
                <span style={{ ...blockLabel, fontSize: 8.5, margin: 0, color: 'rgba(237,235,230,0.38)' }}>optional</span>
                <svg width="10" height="10" viewBox="0 0 10 10" style={{ marginInlineStart: 'auto', flexShrink: 0, transform: charOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.25s ease' }}>
                  <path d="M2 3.5 5 6.5 8 3.5" stroke="rgba(237,235,230,0.5)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
                </svg>
              </button>
              <div style={{ maxHeight: charOpen ? 700 : 0, overflow: 'hidden', opacity: charOpen ? 1 : 0, transition: 'max-height 0.42s ' + ease + ', opacity 0.28s ease' }}>
                <div style={{ padding: '0 13px 13px' }}>
                  <div style={{ ...secLabel, marginTop: 4 }}>A starting shape</div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7 }}>
                    {CHAR_ARCHETYPES.map((a) => (
                      <Chip key={a.id} active={arch === a.id} onClick={() => setArch(arch === a.id ? null : a.id)}>{a.name}</Chip>
                    ))}
                  </div>
                  <div style={{
                    fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 13, lineHeight: 1.5,
                    color: 'rgba(237,235,230,0.55)', marginTop: 7, minHeight: 20,
                    opacity: arch ? 1 : 0, transition: 'opacity 0.25s ease',
                  }}>{arch ? (CHAR_ARCHETYPES.find((a) => a.id === arch) || {}).hint : ''}</div>
                  <div style={secLabel}>Anything else the GameMaster should know</div>
                  <textarea rows={2} value={archDetail} onChange={(e) => setArchDetail(e.target.value)}
                    placeholder={'a scar, a tic, a bond - anything at all'}
                    style={{ ...inputStyle, resize: 'none', lineHeight: 1.5, minHeight: 48, fontSize: 13.5 }} />
                </div>
              </div>
            </div>
            <div style={secLabel}>Name this save</div>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder={w ? w.name : ''} style={inputStyle} />
            {failed && <div role="alert" data-testid="new-error">
              <div style={{ ...secLabel, color: '#e05252' }}>{t('error.new.label')}</div>
              <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 13.5, lineHeight: 1.55, color: 'rgba(237,235,230,0.65)' }}>
                {newGameKey(failed.error) ? t(newGameKey(failed.error)) : errorText(failed.error, interval)}
              </div>
              {opensSettings(failed.error) && <button className="co-btn" data-testid="new-error-settings" onClick={onOpenKeys} style={{ marginTop: 8 }}>{t('error.open_settings')}</button>}
            </div>}
            <button disabled={!ready || !!gate} onClick={() => { if (ready && !gate) onCreate({ name: (name || w.name).trim() || w.name, pack: w, character: vals.char.trim(),
              notes: [arch && (CHAR_ARCHETYPES.find((a) => a.id === arch) || {}).hint, archDetail.trim()].filter(Boolean).join('; '),
              draft: { world, vals, name, charOpen, arch, archDetail } }); }} style={{
              marginTop: 16, width: '100%', boxSizing: 'border-box', cursor: ready && !gate ? 'pointer' : 'default',
              padding: '13px', borderRadius: 14, border: 'none',
              fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace", fontSize: 12, letterSpacing: '0.11em', textTransform: 'uppercase', fontWeight: 600,
              color: ready && !gate ? '#0c1414' : 'rgba(237,235,230,0.32)',
              background: ready && !gate ? 'linear-gradient(180deg, #5df2df, #2fb9ab)' : 'rgba(255,255,255,0.05)',
              boxShadow: ready && !gate ? '0 0 22px rgba(64,224,208,0.25)' : 'none',
              transition: 'background 0.25s ease, color 0.25s ease, box-shadow 0.25s ease',
            }}>Begin story</button>
            {gate && <div data-testid="begin-reason" style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 13, lineHeight: 1.5, color: 'rgba(237,235,230,0.6)', marginTop: 8 }}>{gate}</div>}
            {gate && <button className="co-btn" data-testid="begin-settings" onClick={onOpenKeys} style={{ marginTop: 6 }}>{t('error.open_settings')}</button>}
          </div>
        </div>
      </div>
    </div>
  );
}
// one game in the save list, in the design's save-row look; the open game's controls open inside its row
function SaveRow({ info, active, kept, testid = 'save-row', open, onToggle, onEnter, disabled, children }) {
  const name = info ? info.name : t('saves.row.empty_name');
  const meta = info ? t('saves.row.meta', { count: info.count }) : t('saves.row.empty_meta');
  const summary = info ? info.summary : '';
  return (
    <div style={{
      width: '100%', textAlign: 'start',
      background: 'rgba(255,255,255,0.045)', border: '1px solid rgba(232,232,236,0.16)',
      borderRadius: 14, padding: '12px 14px', marginBottom: 10, color: 'inherit',
      fontFamily: 'inherit', boxSizing: 'border-box',
    }}>
      <button data-testid={testid} aria-expanded={open} aria-label={t('saves.row.controls')} onClick={onToggle} style={{
        display: 'block', width: '100%', textAlign: 'start', background: 'none', border: 'none', padding: 0,
        cursor: 'pointer', color: 'inherit', fontFamily: 'inherit',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 17, color: '#EDEBE6' }}>{name}</span>
          {active && (
            <span style={{
              fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
              fontSize: 9, letterSpacing: '0.1em', textTransform: 'uppercase',
              color: '#40E0D0', border: '1px solid rgba(64,224,208,0.35)', borderRadius: 999, padding: '2px 7px',
            }}>{t('saves.row.active')}</span>
          )}
          {kept && (
            <span data-testid="slot-kept" style={{
              fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
              fontSize: 9, letterSpacing: '0.1em', textTransform: 'uppercase',
              color: 'rgba(237,235,230,0.55)', border: '1px solid rgba(232,232,236,0.25)', borderRadius: 999, padding: '2px 7px',
            }}>{t('storage.full.label')}</span>
          )}
          {info && (
            <span role="button" data-testid={testid !== 'save-row' ? testid + '-enter' : undefined} aria-label={'Enter ' + name} title={'Enter ' + name}
              aria-disabled={disabled ? true : undefined} onClick={(e) => { e.stopPropagation(); if (!disabled) onEnter(); }}
              style={{
                marginInlineStart: 'auto', flexShrink: 0, width: 30, height: 30, borderRadius: 15,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: 'radial-gradient(circle at 50% 30%, #6b7076 0%, rgb(67, 71, 77) 55%, rgb(38, 41, 46) 100%)',
                boxShadow: '0 0 0 1.5px rgb(118, 124, 132)',
              }}>
              <svg width="14" height="14" viewBox="0 0 14 14">
                <path d="M8 1.8h4v10.4H8" stroke="rgba(255,255,255,0.9)" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
                <path d="M1.5 7h6.5M5.5 4.5 8 7l-2.5 2.5" stroke="rgba(255,255,255,0.9)" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
              </svg>
            </span>
          )}
          <svg width="10" height="10" viewBox="0 0 10 10" style={{ marginInlineStart: info ? 0 : 'auto', flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform 0.25s ease' }}>
            <path d="M2 3.5 5 6.5 8 3.5" stroke="rgba(237,235,230,0.5)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
          </svg>
        </div>
        <div style={{ ...blockLabel, fontSize: 9, marginBottom: 0, marginTop: 5 }}>{meta}</div>
      </button>
      <div style={{
        maxHeight: open ? 1100 : 0, overflow: 'hidden', opacity: open ? 1 : 0,
        transition: 'max-height 0.32s cubic-bezier(0.34, 1.2, 0.64, 1), opacity 0.22s ease',
      }}>
        {summary && (
          <React.Fragment>
            <div style={{ ...blockLabel, fontSize: 9, margin: '10px 0 4px', color: 'rgba(237,235,230,0.4)' }}>{t('saves.row.summary')}</div>
            <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 13.5, lineHeight: 1.55, color: 'rgba(237,235,230,0.65)' }}>{summary}</div>
          </React.Fragment>
        )}
        {children}
      </div>
    </div>
  );
}

// main menu - spawns from the left sphere, covers the story area
function MenuPanel({ open, onClose, onCreate, packs, creating, saves, doc, ask, rows, onOpenSlot, onRemoveSlot, onVetSlot, summaryShown, switching, beginReason, newFailed, onOpenKeys, interval }) {
  const [rowOpen, setRowOpen] = React.useState(false);
  const [slotOpen, setSlotOpen] = React.useState(null); // id of the expanded stored game
  const [newOpen, setNewOpen] = React.useState(false); // is the New-save form up? minimises the saves list
  React.useEffect(() => { if (!open) { setNewOpen(false); setRowOpen(false); setSlotOpen(null); } }, [open]);
  React.useEffect(() => { if (open && newFailed) setNewOpen(true); }, [open, newFailed]);
  // each new ask for a passphrase opens the save controls once so the prompt is in view
  React.useEffect(() => { if (open && ask) setRowOpen(true); }, [ask]);
  const geoEase = 'cubic-bezier(0.34, 1.2, 0.64, 1)';
  const geo = ['left', 'top', 'width', 'height', 'border-radius', 'box-shadow']
    .map((p) => `${p} 0.36s ${geoEase}`).join(', ');
  const shape = open
    ? { left: 20, top: 2, width: 'calc(100% - 40px)', height: 620, borderRadius: 24 }
    : { left: 22, top: 2, width: 46, height: 46, borderRadius: 23 };
  return (
    <div data-screen-label="Menu panel" aria-hidden={!open} style={{
      position: 'absolute', boxSizing: 'border-box', zIndex: 6,
      ...shape,
      border: '2px solid transparent',
      background: 'linear-gradient(#0c1414, #0c1414) padding-box, linear-gradient(180deg, #e8e8ec, #a8adb5 50%, rgb(98, 103, 110)) border-box',
      boxShadow: open
        ? '0 24px 48px rgba(0,0,0,0.6), 0 8px 20px rgba(0,0,0,0.5), 0 0 32px rgba(64,224,208,0.07)'
        : 'none',
      overflow: 'hidden',
      pointerEvents: open ? 'auto' : 'none',
      opacity: open ? 1 : 0,
      transition: geo + (open ? ', opacity 0.01s linear' : ', opacity 0.15s ease 0.26s'),
    }}>
      <div aria-hidden="true" style={{
        position: 'absolute', pointerEvents: 'none',
        left: 0, top: 0, width: 42, height: 42, borderRadius: 21,
        boxShadow: open ? '0 0 0 2px #9ba1a9' : 'none',
        background:
          'radial-gradient(circle at 50% 94%, #5df2df 0%, rgba(64,224,208,0.5) 42%, rgba(64,224,208,0) 75%),' +
          'radial-gradient(circle at 50% 32%, #38b7a8 0%, rgb(21, 128, 122) 55%, #0a4a46 100%)',
        transition: 'box-shadow 0.36s ' + geoEase,
      }}></div>
      <div style={{
        position: 'relative', padding: '12px 16px 14px',
        opacity: open ? 1 : 0,
        transform: (open ? 'translateY(0)' : 'translateY(6px)') + ' var(--co-unflip, scaleX(1))',
        direction: 'var(--co-dir, ltr)',
        transition: open ? 'opacity 0.24s ease 0.16s, transform 0.3s cubic-bezier(0.102, 0.045, 0.360, 0.909) 0.16s' : 'opacity 0.12s ease, transform 0.12s ease',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', marginInlineStart: 44, marginBottom: 14, minHeight: 26 }}>
          <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 17, color: '#EDEBE6' }}>Menu</span>
        </div>
        <div className="npc-scroll" style={{ maxHeight: 520, overflowY: 'auto', paddingRight: 6 }}>
          <div style={{ ...blockLabel, fontSize: 9.5, color: 'rgba(237,235,230,0.45)', marginBottom: 8, opacity: newOpen ? 0 : 1, transition: 'opacity 0.2s ease' }}>Saves</div>
          <div style={{ maxHeight: newOpen ? 0 : 2000, opacity: newOpen ? 0 : 1, overflow: 'hidden', pointerEvents: newOpen ? 'none' : 'auto', transition: 'max-height 0.42s ' + geoEase + ', opacity 0.26s ease' }}>
            <SaveRow info={doc ? { name: doc.name, count: responsesOf(doc), summary: summaryShown(doc) } : null} active={!!doc}
              open={rowOpen} onToggle={() => setRowOpen((o) => !o)} onEnter={onClose}>{saves}</SaveRow>
            {rows.map((r) => (
              <SaveRow key={r.id} testid="save-slot" info={r} active={false} kept={r.kept} open={slotOpen === r.id} disabled={switching}
                onToggle={() => {
                  // opening a row the engine has not checked asks it first
                  if (slotOpen !== r.id && r.status === 'unchecked') onVetSlot(r.id);
                  setSlotOpen((o) => (o === r.id ? null : r.id));
                }}
                onEnter={async () => { if (await onOpenSlot(r.id)) onClose(); else setRowOpen(true); }}>
                {r.removable && (
                  <button className="co-btn quiet" data-testid="save-slot-remove" disabled={switching} style={{ marginTop: 10 }}
                    onClick={() => onRemoveSlot(r.id)}>{t('saves.row.remove')}</button>
                )}
              </SaveRow>
            ))}
          </div>
          <NewSaveForm menuOpen={open} open={newOpen} onToggle={() => setNewOpen((o) => !o)} onCreate={onCreate} packs={packs} creating={creating}
            gate={beginReason} failed={newFailed} onOpenKeys={onOpenKeys} interval={interval} />
        </div>
      </div>
      {/* the sphere in the corner is still the menu button - tap to fold back */}
      <button aria-label="Close menu" onClick={onClose} style={{
        position: 'absolute', left: 0, top: 0, width: 44, height: 44,
        background: 'none', border: 'none', cursor: 'pointer', padding: 0,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'rgba(255,255,255,0.92)',
        opacity: open ? 1 : 0,
        transition: 'opacity 0.2s ease 0.1s',
        pointerEvents: open ? 'auto' : 'none',
      }}>
        <svg width="16" height="16" viewBox="0 0 16 16">
          <path d="M2.5 4.2h11M2.5 8h11M2.5 11.8h11" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"></path>
        </svg>
      </button>
    </div>
  );
}
function MorePanel({ open, onClose, voices, colors, onVoice, saveName, doc }) {
  const [mixer, setMixer] = React.useState(false);
  const [mapOpen, setMapOpen] = React.useState(false);
  const [npcOpen, setNpcOpen] = React.useState(false);
  const [invOpen, setInvOpen] = React.useState(false);
  const [expandedNpc, setExpandedNpc] = React.useState(null);
  const st = (doc && doc.state) || {};
  const npcs = npcCards(doc);
  const invItems = ((doc && doc.items) || []).filter((it) => it && it.holder === 'player')
    .map((it, i) => ({ id: it._id || i, emoji: it.emoji || '', name: it.name, sub: it.description || '' }));
  const responses = ((doc && doc.history) || []).filter((h) => h.role === 'assistant').length;
  const places = knownPlaces(doc);
  React.useEffect(() => { if (!open) { setMixer(false); setMapOpen(false); setNpcOpen(false); setInvOpen(false); setExpandedNpc(null); } }, [open]);
  const items = [
    { id: 'inventory', label: 'Inventory', onClick: () => { setInvOpen((v) => !v); setMixer(false); setNpcOpen(false); setMapOpen(false); }, glyph: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <path d="M4.8 5.5h6.4l1 8H3.8l1-8zM6 5.5V4.3a2 2 0 0 1 4 0v1.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
      </svg>
    )},
    { id: 'npcs', label: 'NPC book', onClick: () => { setNpcOpen((n) => !n); setMixer(false); setInvOpen(false); setMapOpen(false); }, glyph: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <rect x="4" y="2.5" width="8" height="11" rx="1.2" stroke="currentColor" strokeWidth="1.5" fill="none"></rect>
        <path d="M6.3 2.5v11" stroke="currentColor" strokeWidth="1.5"></path>
      </svg>
    )},
    { id: 'map', label: 'Map', onClick: () => { setMapOpen((m) => !m); setMixer(false); setNpcOpen(false); setInvOpen(false); }, glyph: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <path d="M2.5 4.2 6.2 2.7l3.6 1.5 3.7-1.5v8.6l-3.7 1.5-3.6-1.5-3.7 1.5V4.2z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" fill="none"></path>
        <path d="M6.2 2.7v8.6M9.8 4.2v8.6" stroke="currentColor" strokeWidth="1.4"></path>
      </svg>
    )},
    { id: 'colour', label: 'Colours', onClick: () => { setMixer((m) => !m); setNpcOpen(false); setInvOpen(false); setMapOpen(false); }, glyph: (
      <svg width="16" height="16" viewBox="0 0 16 16">
        <circle cx="8" cy="8" r="6.6" stroke="rgba(255,255,255,0.35)" strokeWidth="1" fill="none"></circle>
        <circle cx="8" cy="8" r="4.2" fill={colors.player}></circle>
      </svg>
    )},
  ];
  const geoEase = 'cubic-bezier(0.34, 1.2, 0.64, 1)';
  const geo = ['left', 'top', 'width', 'height', 'border-radius', 'box-shadow']
    .map((p) => `${p} 0.36s ${geoEase}`).join(', ');
  // closed = exactly over the right sphere; open = the sphere's own corner
  // becomes the box's top-left, hovering out to the screen edge
  const shape = open
    ? { left: 64, top: 2, width: 'calc(100% - 84px)', height: mixer ? 336 : npcOpen ? 484 : invOpen ? 484 : mapOpen ? 484 : 196, borderRadius: 24 }
    : { left: 66, top: 2, width: 46, height: 46, borderRadius: 23 };
  return (
    <div data-screen-label="More panel" aria-hidden={!open} style={{
      position: 'absolute', boxSizing: 'border-box', zIndex: 5,
      ...shape,
      border: '2px solid transparent',
      background: 'linear-gradient(#0c1414, #0c1414) padding-box, linear-gradient(180deg, #e8e8ec, #a8adb5 50%, rgb(98, 103, 110)) border-box',
      boxShadow: open
        ? '0 24px 48px rgba(0,0,0,0.6), 0 8px 20px rgba(0,0,0,0.5), 0 0 32px rgba(64,224,208,0.07)'
        : 'none',
      overflow: 'hidden',
      pointerEvents: open ? 'auto' : 'none',
      opacity: open ? 1 : 0,
      transition: geo + (open ? ', opacity 0.01s linear' : ', opacity 0.15s ease 0.26s'),
    }}>
      {/* sphere skin - a sphere the whole time; the box unfolds from behind it */}
      <div aria-hidden="true" style={{
        position: 'absolute', pointerEvents: 'none',
        left: 0, top: 0, width: 42, height: 42, borderRadius: 21,
        boxShadow: open ? '0 0 0 2px #9ba1a9' : 'none',
        background:
          'radial-gradient(circle at 50% 94%, #5df2df 0%, rgba(64,224,208,0.5) 42%, rgba(64,224,208,0) 75%),' +
          'radial-gradient(circle at 50% 32%, #38b7a8 0%, rgb(21, 128, 122) 55%, #0a4a46 100%)',
        transition: 'box-shadow 0.36s ' + geoEase,
      }}></div>
      {/* contents - arrive once the box has taken shape */}
      <div style={{
        position: 'relative', padding: '12px 16px 14px',
        opacity: open ? 1 : 0,
        transform: (open ? 'translateY(0)' : 'translateY(6px)') + ' var(--co-unflip, scaleX(1))',
        direction: 'var(--co-dir, ltr)',
        transition: open ? 'opacity 0.24s ease 0.16s, transform 0.3s ease 0.16s' : 'opacity 0.12s ease, transform 0.12s ease',
      }}>
      {/* save header - clears the corner where the sphere folded in */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginInlineStart: 44, marginBottom: 12, minHeight: 26 }}>
        <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 17, color: '#EDEBE6' }}>{saveName}</span>
        {/* turns plaque - same silver texture, top right */}
        <span style={{
          marginInlineStart: 'auto',
          fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
          fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase',
          color: '#e4e6ea', padding: '5px 10px', borderRadius: 8,
          border: '1px solid transparent',
          background: 'linear-gradient(rgb(20, 27, 27), #0c1414) padding-box, linear-gradient(180deg, #e8e8ec, #a8adb5 50%, rgb(98, 103, 110)) border-box',
          boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.08)',
          whiteSpace: 'nowrap',
        }}>{(doc && doc.turn) || 0} turns</span>
      </div>
      {/* info - everything about the save, nothing about the turn */}
      <div style={{ display: 'flex', gap: 22, marginBottom: 12 }}>
        <div>
          <div style={{ ...blockLabel, fontSize: 9.5, marginBottom: 3, color: 'rgba(237,235,230,0.45)' }}>Responses</div>
          <div style={{ fontSize: 14, color: '#EDEBE6' }}>{responses}</div>
        </div>
        <div>
          <div style={{ ...blockLabel, fontSize: 9.5, marginBottom: 3, color: 'rgba(237,235,230,0.45)' }}>Scene</div>
          <div style={{ fontSize: 14, color: '#EDEBE6' }}>{st.location || '-'}</div>
        </div>
      </div>
      <div aria-hidden="true" style={{ height: 1, marginBottom: 12, background: 'linear-gradient(90deg, transparent, rgba(232,232,236,0.2), transparent)' }}></div>
      {/* buttons */}
      <div style={{ display: 'flex', gap: 13 }}>
        {items.map((it, i) => (
          <div key={it.id} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5 }}>
            <button aria-label={it.label} onClick={it.onClick} className="orb-btn orb-gunmetal pressable"
              style={{
                opacity: open ? 1 : 0,
                transform: open ? 'scale(1)' : 'scale(0.6)',
                transition: 'opacity 0.2s ease, transform 0.3s cubic-bezier(0.34, 1.56, 0.64, 1)',
                transitionDelay: open ? (60 + i * 45) + 'ms' : '0ms',
              }}>{it.glyph}</button>
            <span style={{ fontSize: 11, color: 'rgba(237,235,230,0.55)', textAlign: 'center' }}>{it.label}</span>
          </div>
        ))}
      </div>
      {/* colour mixer - revealed as the box extends down */}
      <div aria-hidden={!mixer} style={{
        marginTop: mixer ? 14 : 0,
        maxHeight: mixer ? 180 : 0,
        overflow: mixer ? 'visible' : 'hidden',
        opacity: mixer ? 1 : 0,
        transition: (mixer ? 'opacity 0.24s ease 0.14s' : 'opacity 0.12s ease') + ', max-height 0.36s cubic-bezier(0.34, 1.2, 0.64, 1), margin-top 0.36s cubic-bezier(0.34, 1.2, 0.64, 1)',
        pointerEvents: mixer ? 'auto' : 'none',
      }}>
        <div aria-hidden="true" style={{ height: 1, marginBottom: 12, background: 'linear-gradient(90deg, transparent, rgba(232,232,236,0.2), transparent)' }}></div>
        <div style={{ ...blockLabel, fontSize: 9.5, color: 'rgba(237,235,230,0.45)', marginBottom: 10 }}>Colours - hue and brightness</div>
        {[{ k: 'dm', label: 'GM' }, { k: 'player', label: 'You' }, { k: 'npc', label: 'NPC' }].map(({ k, label }) => (
          <div key={k} style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12 }}>
            <span style={{ ...blockLabel, fontSize: 9.5, marginBottom: 0, width: 32, flexShrink: 0, color: colors[k] }}>{label}</span>
            <HSlider value={voices[k].h / 360} onChange={(v) => onVoice(k, { h: Math.round(v * 360) })} track={RAINBOW} />
            <HSlider value={(voices[k].l - 0.25) / 0.6} onChange={(v) => onVoice(k, { l: 0.25 + v * 0.6 })}
              track={`linear-gradient(90deg, ${hslToHex(voices[k].h / 360, VOICE_SAT, 0.25)}, ${hslToHex(voices[k].h / 360, VOICE_SAT, 0.85)})`} />
          </div>
        ))}
      </div>
      {/* npc book - cards: silhouette left, stats right, tap to expand */}
      <div aria-hidden={!npcOpen} style={{
        marginTop: npcOpen ? 14 : 0,
        maxHeight: npcOpen ? 320 : 0,
        overflow: npcOpen ? 'visible' : 'hidden',
        opacity: npcOpen ? 1 : 0,
        transition: (npcOpen ? 'opacity 0.24s ease 0.14s' : 'opacity 0.12s ease') + ', max-height 0.36s cubic-bezier(0.34, 1.2, 0.64, 1), margin-top 0.36s cubic-bezier(0.34, 1.2, 0.64, 1)',
        pointerEvents: npcOpen ? 'auto' : 'none',
      }}>
        <div aria-hidden="true" style={{ height: 1, marginBottom: 12, background: 'linear-gradient(90deg, transparent, rgba(232,232,236,0.2), transparent)' }}></div>
        <div style={{ ...blockLabel, fontSize: 9.5, color: 'rgba(237,235,230,0.45)', marginBottom: 10 }}>NPC book</div>
        <div className="npc-scroll" style={{ maxHeight: 236, overflowY: 'auto', paddingRight: 6 }}>
        {npcs.map((n) => {
          const c = npcHue(n.hue);
          const isOpen = expandedNpc === n.key;
          return (
            <button key={n.key} onClick={() => setExpandedNpc((e) => e === n.key ? null : n.key)} style={{
              display: 'flex', gap: 12, alignItems: 'flex-start', width: '100%', textAlign: 'start',
              background: 'rgba(255,255,255,0.045)', border: '1px solid rgba(232,232,236,0.16)',
              borderRadius: 14, padding: 10, marginBottom: 10, cursor: 'pointer', color: 'inherit',
              fontFamily: 'inherit', boxSizing: 'border-box',
            }}>
              {/* silhouette - placeholder for the npc portrait */}
              <div aria-hidden="true" style={{
                width: 64, height: 64, borderRadius: 12, flexShrink: 0, position: 'relative', overflow: 'hidden',
                background: `repeating-linear-gradient(45deg, rgba(255,255,255,0.05) 0 6px, rgba(255,255,255,0.02) 6px 12px), rgb(16, 24, 23)`,
              }}>
                <svg width="64" height="64" viewBox="0 0 64 64" style={{ position: 'absolute', inset: 0 }}>
                  <circle cx="32" cy="25" r="11" fill={c} opacity="0.35"></circle>
                  <ellipse cx="32" cy="56" rx="19" ry="14" fill={c} opacity="0.35"></ellipse>
                </svg>
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 16, color: c }}>{n.name}</span>
                  <svg width="10" height="10" viewBox="0 0 10 10" style={{ marginInlineStart: 'auto', flexShrink: 0, transform: isOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.25s ease' }}>
                    <path d="M2 3.5 5 6.5 8 3.5" stroke="rgba(237,235,230,0.5)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
                  </svg>
                </div>
                <div style={{ ...blockLabel, fontSize: 9, marginBottom: 2, marginTop: 6, color: 'rgba(237,235,230,0.4)' }}>Items</div>
                <div style={{ fontSize: 12.5, color: '#EDEBE6' }}>{n.itemsText}</div>
                <div style={{ ...blockLabel, fontSize: 9, marginBottom: 2, marginTop: 6, color: 'rgba(237,235,230,0.4)' }}>Last seen</div>
                <div style={{ fontSize: 12.5, color: '#EDEBE6' }}>{n.lastSeen}</div>
                <div style={{
                  maxHeight: isOpen ? 80 : 0, overflow: 'hidden', opacity: isOpen ? 1 : 0,
                  transition: 'max-height 0.3s cubic-bezier(0.34, 1.2, 0.64, 1), opacity 0.22s ease',
                }}>
                  <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 13, lineHeight: 1.5, color: 'rgba(237,235,230,0.6)', marginTop: 8 }}>{n.persona}</div>
                </div>
              </div>
            </button>
          );
        })}
        </div>
      </div>
      {/* inventory - 3 item cards per row */}
      <div aria-hidden={!invOpen} style={{
        marginTop: invOpen ? 14 : 0,
        maxHeight: invOpen ? 300 : 0,
        overflow: invOpen ? 'visible' : 'hidden',
        opacity: invOpen ? 1 : 0,
        transition: (invOpen ? 'opacity 0.24s ease 0.14s' : 'opacity 0.12s ease') + ', max-height 0.36s cubic-bezier(0.34, 1.2, 0.64, 1), margin-top 0.36s cubic-bezier(0.34, 1.2, 0.64, 1)',
        pointerEvents: invOpen ? 'auto' : 'none',
      }}>
        <div aria-hidden="true" style={{ height: 1, marginBottom: 12, background: 'linear-gradient(90deg, transparent, rgba(232,232,236,0.2), transparent)' }}></div>
        <div style={{ ...blockLabel, fontSize: 9.5, color: 'rgba(237,235,230,0.45)', marginBottom: 10 }}>Inventory</div>
        <div className="npc-scroll" style={{ maxHeight: 236, overflowY: 'auto', paddingRight: 6, display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 10, alignContent: 'start' }}>
          {invItems.map((it) => (
            <div key={it.id} style={{
              display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
              background: 'rgba(255,255,255,0.045)', border: '1px solid rgba(232,232,236,0.16)',
              borderRadius: 12, padding: '12px 8px 10px', boxSizing: 'border-box',
            }}>
              <div aria-hidden="true" style={{
                width: 40, height: 40, borderRadius: 10, display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: 20, lineHeight: 1, color: '#EDEBE6',
                background: 'repeating-linear-gradient(45deg, rgba(255,255,255,0.05) 0 6px, rgba(255,255,255,0.02) 6px 12px), rgb(16, 24, 23)',
              }}>
                <svg width="20" height="20" viewBox="0 0 16 16">
                  {itemIcon(it.emoji).map((d) => (
                    <path key={d} d={d} stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
                  ))}
                </svg>
              </div>
              <div style={{ fontSize: 11.5, color: '#EDEBE6', textAlign: 'center' }}>{it.name}</div>
              <div style={{ fontSize: 9.5, color: 'rgba(237,235,230,0.45)', textAlign: 'center' }}>{it.sub}</div>
            </div>
          ))}
        </div>
      </div>
      {/* map - the place the story is in now, plus the places met so far */}
      <div aria-hidden={!mapOpen} data-testid="map" style={{
        marginTop: mapOpen ? 14 : 0,
        maxHeight: mapOpen ? 260 : 0,
        overflow: mapOpen ? 'visible' : 'hidden',
        opacity: mapOpen ? 1 : 0,
        transition: (mapOpen ? 'opacity 0.24s ease 0.14s' : 'opacity 0.12s ease') + ', max-height 0.36s cubic-bezier(0.34, 1.2, 0.64, 1), margin-top 0.36s cubic-bezier(0.34, 1.2, 0.64, 1)',
        pointerEvents: mapOpen ? 'auto' : 'none',
      }}>
        <div aria-hidden="true" style={{ height: 1, marginBottom: 12, background: 'linear-gradient(90deg, transparent, rgba(232,232,236,0.2), transparent)' }}></div>
        <div style={{ ...blockLabel, fontSize: 9.5, color: 'rgba(237,235,230,0.45)', marginBottom: 10 }}>Map</div>
        <div className="npc-scroll" style={{ maxHeight: 236, overflowY: 'auto', paddingRight: 6 }}>
        <div style={{
          height: 150, borderRadius: 12, border: '1px solid rgba(232,232,236,0.16)',
          background: 'repeating-linear-gradient(45deg, rgba(255,255,255,0.04) 0 8px, rgba(255,255,255,0.015) 8px 16px), rgb(16, 24, 23)',
          display: 'flex', flexDirection: 'column', gap: 6, alignItems: 'center', justifyContent: 'center', flexShrink: 0, textAlign: 'center', padding: '0 14px',
        }}>
          {st.district ? <span style={{ fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace", fontSize: 10, letterSpacing: '0.08em', color: 'rgba(237,235,230,0.35)' }}>{st.district}</span> : null}
          <span style={{ fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace", fontSize: 10, letterSpacing: '0.08em', color: 'rgba(237,235,230,0.35)' }}>{st.location || '-'}</span>
        </div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 10 }}>
          {places.map((p) => (
            <span key={p.name} data-testid={p.here ? 'place-here' : 'place'} style={{
              fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
              fontSize: 9.5, letterSpacing: '0.08em', textTransform: 'uppercase',
              padding: '4px 9px', borderRadius: 999,
              color: p.here ? '#0c1414' : 'rgba(237,235,230,0.6)',
              background: p.here ? colors.player : 'rgba(255,255,255,0.06)',
              border: p.here ? '1px solid transparent' : '1px solid rgba(232,232,236,0.16)',
            }}>{p.name}</span>
          ))}
        </div>
        </div>
      </div>
      </div>
      {/* the sphere in the corner is still the ... button - tap to fold back */}
      <button aria-label="Close" onClick={onClose} style={{
        position: 'absolute', left: 0, top: 0, width: 44, height: 44,
        background: 'none', border: 'none', cursor: 'pointer', padding: 0,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: 'rgba(255,255,255,0.92)',
        opacity: open ? 1 : 0,
        transition: 'opacity 0.2s ease 0.1s',
        pointerEvents: open ? 'auto' : 'none',
      }}>
        <svg width="16" height="16" viewBox="0 0 16 16">
          <circle cx="3.5" cy="8" r="1.4" fill="currentColor"></circle>
          <circle cx="8" cy="8" r="1.4" fill="currentColor"></circle>
          <circle cx="12.5" cy="8" r="1.4" fill="currentColor"></circle>
        </svg>
      </button>
    </div>
  );
}

// settings - spawns from the travelled sphere; its corner is the bottom-right
function SettingsPanel({ open, apiKeys, prefs, onPrefs, forgetLabel, onForget, reveal, forgetDisabled }) {
  const [expanded, setExpanded] = React.useState(null);
  React.useEffect(() => { if (!open) setExpanded(null); }, [open]);
  // after a lock, the API key section opens on the locked store
  React.useEffect(() => { if (reveal) setExpanded('apikey'); }, [reveal]);
  const geoEase = 'cubic-bezier(0.34, 1.2, 0.64, 1)';
  const geo = ['left', 'top', 'width', 'height', 'border-radius', 'box-shadow']
    .map((p) => `${p} 0.36s ${geoEase}`).join(', ');
  const shape = open
    ? { left: 20, top: 250, width: 'calc(100% - 40px)', height: 372, borderRadius: 24 }
    : { left: 334, top: 574, width: 46, height: 46, borderRadius: 23 };
  const chip = (label, sel, onClick, disabled) => (
    <span key={label} role="button" aria-disabled={disabled ? true : undefined} aria-pressed={sel} onClick={disabled ? undefined : onClick} style={{
      fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
      fontSize: 9.5, letterSpacing: '0.08em', textTransform: 'uppercase',
      padding: '4px 10px', borderRadius: 999, cursor: disabled ? 'default' : 'pointer',
      opacity: disabled ? 0.45 : 1,
      color: sel ? '#0c1414' : 'rgba(237,235,230,0.6)',
      background: sel ? '#40E0D0' : 'rgba(255,255,255,0.06)',
      border: sel ? '1px solid transparent' : '1px solid rgba(232,232,236,0.16)',
    }}>{label}</span>
  );
  const sections = [
    { id: 'apikey', label: t('settings.apikey'), maxH: 360, body: apiKeys },
    { id: 'playmode', label: t('settings.playmode'), body: (
      <div style={{ display: 'flex', gap: 8 }}>
        {chip(t('settings.playmode.text'), true, () => {})}
        {chip(t('settings.playmode.visual'), false, () => {}, true)}
      </div>
    )},
    { id: 'submitkey', label: t('settings.submit'), body: (
      <div style={{ display: 'flex', gap: 8 }}>
        {chip(t('settings.submit.enter'), prefs.submitKey === 'enter', () => onPrefs({ submitKey: 'enter' }))}
        {chip(t('settings.submit.shift_enter'), prefs.submitKey === 'shift_enter', () => onPrefs({ submitKey: 'shift_enter' }))}
      </div>
    )},
  ];
  return (
    <div data-screen-label="Settings panel" aria-hidden={!open} style={{
      position: 'absolute', boxSizing: 'border-box', zIndex: 6,
      ...shape,
      border: '2px solid transparent',
      background: 'linear-gradient(#0c1414, #0c1414) padding-box, linear-gradient(180deg, #e8e8ec, #a8adb5 50%, rgb(98, 103, 110)) border-box',
      boxShadow: open
        ? '0 24px 48px rgba(0,0,0,0.6), 0 8px 20px rgba(0,0,0,0.5), 0 0 32px rgba(64,224,208,0.07)'
        : 'none',
      overflow: 'hidden',
      pointerEvents: open ? 'auto' : 'none',
      opacity: open ? 1 : 0,
      transition: geo + (open ? ', opacity 0.01s linear' : ', opacity 0.15s ease 0.26s'),
    }}>
      <div style={{
        position: 'relative', padding: '14px 16px',
        opacity: open ? 1 : 0,
        transition: open ? 'opacity 0.24s ease 0.16s' : 'opacity 0.12s ease',
      }}>
        <div style={{ ...blockLabel, fontSize: 9.5, color: 'rgba(237,235,230,0.45)', marginBottom: 4 }}>{t('settings.title')}</div>
        <div className="npc-scroll" style={{ maxHeight: 268, overflowY: 'auto', paddingRight: 6, paddingBottom: 8 }}>
          {sections.map((s) => {
            const isOpen = expanded === s.id;
            return (
              <div key={s.id} style={{ borderBottom: '1px solid rgba(232,232,236,0.08)' }}>
                <button data-testid={'settings-' + s.id} aria-expanded={isOpen} onClick={() => setExpanded((e) => e === s.id ? null : s.id)} style={{
                  display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'start',
                  background: 'none', border: 'none', cursor: 'pointer', padding: '13px 4px',
                  boxSizing: 'border-box',
                  fontFamily: "'Newsreader', Georgia, serif", fontSize: 16.5, color: '#EDEBE6',
                }}>
                  <span>{s.label}</span>
                  <svg width="10" height="10" viewBox="0 0 10 10" style={{ marginInlineStart: 'auto', flexShrink: 0, transform: isOpen ? 'rotate(180deg)' : 'none', transition: 'transform 0.25s ease' }}>
                    <path d="M2 3.5 5 6.5 8 3.5" stroke="rgba(237,235,230,0.4)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
                  </svg>
                </button>
                <div style={{
                  maxHeight: isOpen ? (s.maxH || 120) : 0, overflow: 'hidden', opacity: isOpen ? 1 : 0,
                  transition: 'max-height 0.3s cubic-bezier(0.34, 1.2, 0.64, 1), opacity 0.22s ease',
                }}>
                  <div style={{ padding: '0 4px 12px' }}>{s.body}</div>
                </div>
              </div>
            );
          })}
          <button data-testid="settings-forget" onClick={onForget} disabled={forgetDisabled} style={{
            display: 'flex', alignItems: 'center', gap: 8, width: '100%', textAlign: 'start',
            background: 'none', border: 'none', cursor: forgetDisabled ? 'default' : 'pointer', padding: '13px 4px',
            fontFamily: "'Newsreader', Georgia, serif", fontSize: 16.5, color: '#EDEBE6',
            opacity: forgetDisabled ? 0.45 : 1,
          }}>{forgetLabel}</button>
        </div>
      </div>
    </div>
  );
}

// the ... sphere disconnects from the knot and travels a mirrored-integral path
// to the bottom-right of the open menu, becoming the Settings button
function TravelSphere({ open, onClick, settingsOpen }) {
  return (
    <button aria-label="Settings" onClick={onClick} aria-expanded={!!settingsOpen} style={{
      position: 'absolute', left: 0, top: 0, width: 46, height: 46, borderRadius: 23,
      border: 'none', cursor: 'pointer', padding: 0, zIndex: 7,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      color: 'rgba(255,255,255,0.92)',
      background:
        'radial-gradient(circle at 50% 94%, #5df2df 0%, rgba(64,224,208,0.5) 42%, rgba(64,224,208,0) 75%),' +
        'radial-gradient(circle at 50% 32%, #38b7a8 0%, rgb(21, 128, 122) 55%, #0a4a46 100%)',
      boxShadow: '0 0 0 2px #9ba1a9',
      offsetPath: "path('M 89 25 C 89 250 357 358 357 597')", /* clean cubic S: leaves straight down, arrives straight down */
      offsetRotate: '0deg',
      offsetDistance: open ? '100%' : '0%',
      opacity: open ? 1 : 0,
      pointerEvents: open ? 'auto' : 'none',
      transition: open
        ? 'offset-distance 0.55s cubic-bezier(0.45, 0, 0.25, 1) 0.05s, opacity 0.01s linear'
        : 'offset-distance 0.45s cubic-bezier(0.45, 0, 0.25, 1), opacity 0.1s ease 0.38s',
    }}>
      <svg width="16" height="16" viewBox="0 0 16 16" style={{ position: 'absolute', opacity: open ? 0 : 1, transition: 'opacity 0.2s ease' }}>
        <circle cx="3.5" cy="8" r="1.4" fill="currentColor"></circle>
        <circle cx="8" cy="8" r="1.4" fill="currentColor"></circle>
        <circle cx="12.5" cy="8" r="1.4" fill="currentColor"></circle>
      </svg>
      <svg width="20" height="20" viewBox="0 0 16 16" style={{ opacity: open ? 1 : 0, transition: 'opacity 0.25s ease ' + (open ? '0.4s' : '0s') }}>
        <circle cx="8" cy="8" r="3" stroke="currentColor" strokeWidth="1.5" fill="none"></circle>
        <path d="M8 1.6v2.2M8 12.2v2.2M1.6 8h2.2M12.2 8h2.2M3.5 3.5l1.5 1.5M11 11l1.5 1.5M12.5 3.5 11 5M5 11l-1.5 1.5" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"></path>
      </svg>
    </button>
  );
}

// small line icons, drawn like the glyphs above; the label is a copy key
const ICON_PATHS = {
  comment: 'M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2Z',
  flag: 'M4 14.5V2M4 2.5h8.5l-2 3 2 3H4',
  good: 'M2.5 7h2.5v6.5H2.5ZM5 7l2.6-4.8c1 0 1.7.8 1.5 1.8L8.7 6h3.8c.8 0 1.4.8 1.2 1.6l-1.2 4.8c-.2.7-.8 1.1-1.5 1.1H5',
  meh: 'M2 8a6 6 0 1 0 12 0a6 6 0 1 0-12 0ZM5.8 6.2v.6M10.2 6.2v.6M5.5 10.5h5',
  chapter: 'M8 1.5 9.4 6.6 14.5 8 9.4 9.4 8 14.5 6.6 9.4 1.5 8 6.6 6.6Z',
  season: 'M8 1.5v13M2.4 4.75l11.2 6.5M2.4 11.25l11.2-6.5',
};
ICON_PATHS.bad = ICON_PATHS.good;

function Icon({ name, label, size = 14 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" role="img" aria-label={t(label)} style={{ flexShrink: 0, verticalAlign: '-0.15em' }}>
      <path d={ICON_PATHS[name]} transform={name === 'bad' ? 'rotate(180 8 8)' : undefined}
        stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none"></path>
    </svg>
  );
}

// long-press turn actions - 3 categories: feedback / history / rating
const TURN_ACTION_GROUPS = [
  { id: 'feedback', items: [
    { id: 'feedback', label: 'turn.comment', icon: 'comment', hue: 174, need: 'comment' },
    { id: 'flag', label: 'turn.flag', icon: 'flag', hue: 25, need: 'flag' },
  ]},
  { id: 'history', items: [
    { id: 'fork', label: 'turn.fork', hue: 265, need: 'fork' },
    { id: 'undo', label: 'turn.undo', hue: 210, need: 'undo' },
  ]},
  { id: 'rating', items: [
    { id: 'rate-up', label: 'turn.good', icon: 'good', hue: 130, need: 'rate' },
    { id: 'rate-meh', label: 'turn.meh', icon: 'meh', hue: 48, need: 'rate' },
    { id: 'rate-down', label: 'turn.bad', icon: 'bad', hue: 0, need: 'rate' },
  ]},
];

function TurnActions({ open, onAction, enabled, mark }) {
  let i = -1;
  return (
    <div aria-hidden={!open} data-testid="turn-actions" data-open={open ? 'true' : 'false'} style={{
      display: 'flex', flexWrap: 'wrap', gap: 10,
      maxHeight: open ? 96 : 0, marginTop: open ? 12 : 0,
      overflow: open ? 'visible' : 'hidden',
      opacity: open ? 1 : 0,
      pointerEvents: open ? 'auto' : 'none',
      transition: 'max-height 0.3s cubic-bezier(0.34, 1.2, 0.64, 1), margin-top 0.3s cubic-bezier(0.34, 1.2, 0.64, 1), opacity 0.18s ease',
    }}>
      {TURN_ACTION_GROUPS.map((g) => (
        <div key={g.id} style={{
          display: 'flex', gap: 6, padding: 4, borderRadius: 999,
          background: 'rgba(255,255,255,0.045)', border: '1px solid rgba(232,232,236,0.14)',
        }}>
          {g.items.map((a) => {
            i += 1;
            const on = !!(enabled && enabled[a.need]);
            return (
              <button key={a.id} aria-label={t(a.label)} title={t(a.label)} data-testid={'turn-' + a.id}
                disabled={!on || !open} aria-pressed={mark === a.id ? true : undefined}
                onClick={(e) => { e.stopPropagation(); if (onAction) onAction(a.id); }}
                style={{
                  width: 28, height: 28, borderRadius: 14, flexShrink: 0,
                  border: 'none', cursor: on ? 'pointer' : 'default', padding: 0,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  color: 'rgba(255,255,255,0.9)',
                  background: `radial-gradient(circle at 50% 30%, hsl(${a.hue}, 32%, 44%) 0%, hsl(${a.hue}, 36%, 28%) 55%, hsl(${a.hue}, 40%, 15%) 100%)`,
                  boxShadow: (mark === a.id ? '0 0 0 2px #40E0D0, ' : '0 0 0 1.5px rgb(118, 124, 132), ') + '0 6px 14px rgba(0,0,0,0.5)',
                  opacity: open ? (on ? 1 : 0.35) : 0,
                  transform: open ? 'translateY(0) scale(1)' : 'translateY(8px) scale(0.5)',
                  transition: 'opacity 0.2s ease, transform 0.32s cubic-bezier(0.34, 1.56, 0.64, 1)',
                  transitionDelay: open ? (i * 35) + 'ms' : '0ms',
                }}>
                {a.icon
                  ? <Icon name={a.icon} label={a.label} size={14} />
                  : a.id === 'fork'
                    ? <svg width="17" height="17" viewBox="0 0 24 24"><path d="M7 5C7 3.89543 7.89543 3 9 3C10.1046 3 11 3.89543 11 5C11 5.74028 10.5978 6.38663 10 6.73244V14.0396H11.7915C12.8961 14.0396 13.7915 13.1441 13.7915 12.0396V10.7838C13.1823 10.4411 12.7708 9.78837 12.7708 9.03955C12.7708 7.93498 13.6662 7.03955 14.7708 7.03955C15.8753 7.03955 16.7708 7.93498 16.7708 9.03955C16.7708 9.77123 16.3778 10.4111 15.7915 10.7598V12.0396C15.7915 14.2487 14.0006 16.0396 11.7915 16.0396H10V17.2676C10.5978 17.6134 11 18.2597 11 19C11 20.1046 10.1046 21 9 21C7.89543 21 7 20.1046 7 19C7 18.2597 7.4022 17.6134 8 17.2676V6.73244C7.4022 6.38663 7 5.74028 7 5Z" fill="currentColor"></path></svg>
                    : <svg width="15" height="15" viewBox="0 0 16 16"><path d="M6.5 3 3.5 6l3 3M3.5 6h6a4 4 0 0 1 0 8H8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none"></path></svg>}
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}

// long-press wrapper - 450ms hold opens the action row under the message
function TurnWrap({ children, open, onLongPress, onAction, enabled, mark }) {
  const timer = React.useRef(null);
  const fired = React.useRef(false);
  const clear = () => { if (timer.current) { clearTimeout(timer.current); timer.current = null; } };
  const start = () => {
    clear();
    timer.current = setTimeout(() => { fired.current = true; onLongPress(); }, 450);
  };
  return (
    <div
      data-testid="turn"
      onPointerDown={start}
      onPointerUp={clear}
      onPointerMove={clear}
      onPointerLeave={clear}
      onContextMenu={(e) => e.preventDefault()}
      onClickCapture={(e) => { if (fired.current) { e.stopPropagation(); fired.current = false; } }}
      style={{ WebkitUserSelect: 'none', userSelect: 'none', WebkitTouchCallout: 'none' }}
    >
      {children}
      <TurnActions open={open} onAction={onAction} enabled={enabled} mark={mark} />
    </div>
  );
}

// turn progress - thin strip in the chin: 3 rapid ticks (backend organizes the
// scene), one long crawl (model round-trip), 2 quick ticks (frontend orders it)
const TURN_STAGES = ['classifying your action', 'gathering the scene', 'staging the cast', 'the GameMaster writes...', 'ordering the scene', 'setting the stage'];
const TURN_TICKS = [0, 6, 12, 18, 86, 93, 100];
function TurnProgress() {
  const [vis, setVis] = React.useState(false);
  const [fade, setFade] = React.useState(false);
  const [w, setW] = React.useState({ pct: 0, dur: 0.2 });
  const [label, setLabel] = React.useState('');
  const timers = React.useRef([]);
  const later = (fn, ms) => timers.current.push(setTimeout(fn, ms));
  React.useEffect(() => {
    const clear = () => { timers.current.forEach(clearTimeout); timers.current = []; };
    const onStart = () => {
      clear(); setVis(true); setFade(false); setW({ pct: 0, dur: 0 });
      later(() => { setLabel(TURN_STAGES[0]); setW({ pct: 6, dur: 0.18 }); }, 60);
      later(() => { setLabel(TURN_STAGES[1]); setW({ pct: 12, dur: 0.18 }); }, 300);
      later(() => { setLabel(TURN_STAGES[2]); setW({ pct: 18, dur: 0.18 }); }, 540);
      // the long leg - crawl slowly toward 80% until the backend returns
      later(() => { setLabel(TURN_STAGES[3]); setW({ pct: 80, dur: 11, ease: 'linear' }); }, 800);
    };
    const onDone = () => {
      clear();
      setLabel(TURN_STAGES[4]); setW({ pct: 93, dur: 0.3 });
      later(() => { setLabel(TURN_STAGES[5]); setW({ pct: 100, dur: 0.3 }); }, 320);
      later(() => setFade(true), 750);
      later(() => setVis(false), 1150);
    };
    window.addEventListener('co-turn-start', onStart);
    window.addEventListener('co-turn-done', onDone);
    return () => { clear(); window.removeEventListener('co-turn-start', onStart); window.removeEventListener('co-turn-done', onDone); };
  }, []);
  if (!vis) return null;
  return (
    <div aria-hidden="true" style={{
      position: 'absolute', left: 42, right: 42, top: -30, zIndex: 5,
      opacity: fade ? 0 : 1, transition: 'opacity 0.4s ease', pointerEvents: 'none',
    }}>
      <div style={{
        fontFamily: "'IBM Plex Mono', ui-monospace, Menlo, monospace",
        fontSize: 8.5, letterSpacing: '0.1em', textTransform: 'uppercase',
        color: 'rgba(237,235,230,0.42)', textAlign: 'center', marginBottom: 5,
      }}>{label}</div>
      <div style={{ position: 'relative', height: 8 }}>
        {/* track */}
        <div style={{ position: 'absolute', left: 0, right: 0, top: 3, height: 2, borderRadius: 1, background: 'rgba(232,232,236,0.14)' }}></div>
        {/* stage ticks */}
        {TURN_TICKS.map((t) => (
          <div key={t} style={{ position: 'absolute', left: `calc(${t}% - 0.75px)`, top: 0, width: 1.5, height: 8, borderRadius: 1, background: 'rgba(232,232,236,0.3)' }}></div>
        ))}
        {/* fill - red through the rapid stages, yellow across the long wait, green home */}
        <div style={{
          position: 'absolute', left: 0, right: 0, top: 3, height: 2, borderRadius: 1,
          background: 'linear-gradient(90deg, #e05252 0%, #e0a052 16%, #e0c052 24%, #e0c052 78%, #9fe06a 90%, #52e080 100%)',
          boxShadow: '0 0 8px rgba(224,192,82,0.45)',
          clipPath: `inset(0 ${100 - w.pct}% 0 0)`,
          transition: `clip-path ${w.dur}s ${w.ease || 'ease'}`,
        }}></div>
      </div>
    </div>
  );
}

// every alert shares one box growing out of the composer's top: maintenance, 451, 500,
// the storage notice, and the two celebrations
function ComposerAlert({ kind, label, body, left, stale, onFade, children }) {
  const mono = "'IBM Plex Mono', ui-monospace, Menlo, monospace";
  const maint = kind === 'maint';
  const grant = kind === 'grant' || kind === 'xmas';
  // celebration palette - gold for a new chapter, evergreen for the seasonal variant
  const pal = kind === 'xmas'
    ? { border: '#4fbf72', bg: 'linear-gradient(180deg, rgb(7, 24, 13), rgb(4, 17, 8))', glow: 'rgba(126,224,150,0.12)', label: '#8fe6a3', body: 'rgba(214,240,222,0.82)', mote: '#a3f0b8', moteGlow: 'rgba(150,240,175,0.9)' }
    : { border: '#d9b24f', bg: 'linear-gradient(180deg, #1a1305, rgb(18, 13, 4))', glow: 'rgba(230,207,134,0.12)', label: '#e6cf86', body: 'rgba(240,230,200,0.82)', mote: '#f0d894', moteGlow: 'rgba(240,216,148,0.9)' };
  // a celebration fades on its own once it has been savoured
  React.useEffect(() => {
    if (!grant || !onFade) return undefined;
    const timer = setTimeout(onFade, 20000);
    return () => clearTimeout(timer);
  }, [grant, onFade]);
  // a confirmation fades after a short read
  React.useEffect(() => {
    if (kind !== 'done' || !onFade) return undefined;
    const timer = setTimeout(onFade, TOAST_MS);
    return () => clearTimeout(timer);
  }, [kind, onFade]);
  if (kind === 'done') {
    return (
      <div role="status" data-testid="composer-toast" data-screen-label="Composer toast" style={{
        width: '64%', margin: '0 auto -2px', padding: '8px 16px 10px',
        borderRadius: '16px 16px 0 0',
        border: '2px solid #e8e8ec', borderBottom: 'none',
        background: '#0c1414',
        position: 'relative', zIndex: 1,
        boxShadow: '0 -10px 24px rgba(0,0,0,0.4)',
        display: 'flex', flexDirection: 'column', gap: 2,
      }}>
        <span style={{ fontFamily: mono, fontSize: 9, letterSpacing: '0.14em', textTransform: 'uppercase', color: '#40E0D0' }}>{label}</span>
        <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 13, color: 'rgba(237,235,230,0.65)' }}>{body}</span>
      </div>
    );
  }
  if (grant) {
    return (
      <div role="status" data-testid="composer-celebration" data-screen-label="Composer grant" style={{
        width: '64%', margin: '0 auto -2px', padding: '9px 16px 11px',
        borderRadius: '16px 16px 0 0',
        border: '2px solid ' + pal.border, borderBottom: 'none',
        background: pal.bg,
        position: 'relative', zIndex: 1, overflow: 'hidden',
        boxShadow: '0 -10px 26px rgba(0,0,0,0.4), 0 0 30px ' + pal.glow + ' inset',
        display: 'flex', flexDirection: 'column', gap: 3,
      }}>
        {/* drifting motes */}
        {[0, 1, 2, 3, 4].map((i) => (
          <span key={i} aria-hidden="true" style={{
            position: 'absolute', bottom: 6, left: (12 + i * 19) + '%',
            width: 3, height: 3, borderRadius: 3, background: pal.mote,
            boxShadow: '0 0 6px ' + pal.moteGlow + ', 0 0 2px ' + pal.moteGlow + ', 0 0 1px ' + pal.moteGlow, pointerEvents: 'none',
            animation: 'co-grant-mote ' + (2.2 + i * 0.4) + 's ease-in-out ' + (i * 0.5) + 's infinite',
          }}></span>
        ))}
        <span style={{ fontFamily: mono, fontSize: 9, letterSpacing: '0.14em', textTransform: 'uppercase', color: pal.label }}><Icon name={kind === 'grant' ? 'chapter' : 'season'} label={kind === 'grant' ? 'alert.grant.label' : 'alert.xmas.label'} size={9} />{' ' + label}</span>
        <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 13, color: pal.body }}>{body}</span>
      </div>
    );
  }
  return (
    <div role="alert" data-testid="composer-alert" data-kind={kind} data-screen-label="Composer alert" style={{
      /* same box as the composer - a narrower extension growing out of its top,
         red outline continuous with the pill's; bg overlaps the pill's top
         border so the two read as one shape */
      width: '64%', margin: '0 auto -2px', padding: '8px 16px 10px',
      borderRadius: '16px 16px 0 0',
      border: '2px solid #cc4b47', borderBottom: 'none',
      background: 'rgb(20, 11, 11)',
      position: 'relative', zIndex: 1,
      boxShadow: '0 -10px 24px rgba(0,0,0,0.4)',
      display: 'flex', flexDirection: 'column', gap: 2,
    }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
        <span style={{ fontFamily: mono, fontSize: 9, letterSpacing: '0.14em', textTransform: 'uppercase', color: '#e05252' }}>{label}</span>
        {maint && (
          <span data-testid="maint-countdown" style={{
            fontFamily: mono, fontSize: 11, fontWeight: 600, color: '#e05252', fontVariantNumeric: 'tabular-nums',
          }}>{left > 0 ? <bdi>{'0:' + String(left).padStart(2, '0')}</bdi> : t('alert.maint.now')}</span>
        )}
      </div>
      <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 13, color: 'rgba(237,235,230,0.65)' }}>{body}</span>
      {maint && stale && (
        <React.Fragment>
          <span style={{ fontFamily: "'Newsreader', Georgia, serif", fontSize: 12, color: 'rgba(237,235,230,0.5)' }}>{t('alert.maint.refresh_hint')}</span>
          {/* a true cache-bypassing refresh cannot be scripted; a reload is the closest the page can do */}
          <button data-testid="hard-refresh" onClick={() => window.location.reload()} style={{
            alignSelf: 'center', marginTop: 6,
            fontFamily: mono, fontSize: 10, letterSpacing: '0.1em', textTransform: 'uppercase',
            color: '#e05252', background: 'rgba(224,82,82,0.08)',
            border: '1px solid rgba(224,82,82,0.55)', borderRadius: 10,
            padding: '6px 14px', cursor: 'pointer',
          }}>{t('alert.maint.refresh')}</button>
        </React.Fragment>
      )}
      {children}
    </div>
  );
}

const randomDie = (sides) => {
  const buf = crypto.getRandomValues(new Uint32Array(1));
  return 1 + (buf[0] % sides);
};

// milliseconds a confirmation stays above the composer
const TOAST_MS = 4000;

const LEVEL_DOT = { ok: '#40E0D0', attention: '#e6cf86', error: '#e05252' };

// the top-right status dot; a tap shows the same text as its tooltip, and an unchecked
// key store offers one more read of it
function StatusIcon({ status, onCheck }) {
  const [show, setShow] = React.useState(false);
  if (!status) return null;
  const text = statusText(status);
  const housing = {
    border: '1px solid transparent',
    background: 'linear-gradient(rgb(20, 27, 27), #0c1414) padding-box, linear-gradient(180deg, #e8e8ec, #a8adb5 50%, rgb(98, 103, 110)) border-box',
    boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.08)',
  };
  return (
    <div style={{ position: 'relative', pointerEvents: 'auto' }}>
      <button data-testid="link-status" data-level={status.level} aria-label={text} title={text} onClick={() => setShow((s) => !s)} style={{
        ...housing, width: 26, height: 26, borderRadius: 13, padding: 0, cursor: 'pointer',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}>
        <span aria-hidden="true" style={{
          width: 8, height: 8, borderRadius: 4, background: LEVEL_DOT[status.level],
          animation: status.level === 'ok' ? undefined : 'co-breathe 2.4s ease-in-out infinite',
        }}></span>
      </button>
      {show && (
        <span style={{ ...housing, position: 'absolute', top: 32, right: 0, width: 220, borderRadius: 8, padding: '8px 10px', boxSizing: 'border-box', display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span role="status" data-testid="link-status-text" style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 12.5, lineHeight: 1.5, color: 'rgba(237,235,230,0.55)' }}>{text}</span>
          {status.key === 'status.unchecked' && (
            <button className="co-btn quiet" data-testid="link-status-check" onClick={onCheck} style={{ alignSelf: 'flex-start' }}>{t('status.check_again')}</button>
          )}
        </span>
      )}
    </div>
  );
}

const safeStore = () => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

// the id of the page build this server holds, or null when it cannot be read
async function fetchBuild() {
  try {
    const r = await fetch('/build.json', { cache: 'no-store' });
    const j = await r.json();
    return typeof j.build === 'string' && j.build ? j.build : null;
  } catch {
    return null;
  }
}

function App() {
  const client = React.useMemo(() => createClient(), []);
  const [cfg, setCfg] = React.useState(null);
  const [packs, setPacks] = React.useState([]);
  const [provider, setProvider] = React.useState('anthropic');
  const [hostedKeys, setHostedKeys] = React.useState({}); // provider -> key; memory only: lost on reload
  // the key in force is the selected provider's own, so no key goes out with another provider
  const hostedKey = hostedKeys[provider] || '';
  const setHostedKey = React.useCallback((k) => setHostedKeys((m) => ({ ...m, [provider]: k })), [provider]);
  const [keysEpoch, setKeysEpoch] = React.useState(0);
  const [lockEpoch, setLockEpoch] = React.useState(0);
  const [store, setStore] = React.useState(null); // the last finished read of the self-host key store
  const [toast, setToast] = React.useState(null);
  const [doc, setDoc] = React.useState(null);
  const [gate, setGate] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [creating, setCreating] = React.useState(null); // pack name while the engine seeds a new save
  const [errObj, setErrObj] = React.useState(null);
  const [newErr, setNewErr] = React.useState(null); // {error, draft} of a failed new game
  const [engineDown, setEngineDown] = React.useState(false);
  const [stale, setStale] = React.useState(false); // the server holds a newer page than this tab
  const [left, setLeft] = React.useState(DOWN_SECONDS);
  const [shake, setShake] = React.useState(false);
  const [celebrate, setCelebrate] = React.useState(null); // {chapter} after the chapter rises
  const [streamAt, setStreamAt] = React.useState(null); // history index being typed out
  const [asides, setAsides] = React.useState([]); // session-only replies, keyed by history length
  const [activeTurn, setActiveTurn] = React.useState(null);
  const [compose, setCompose] = React.useState(null); // {kind, who} while giving feedback on a turn
  const [marks, setMarks] = React.useState({}); // local ratings, never sent anywhere
  const [prefs, setPrefs] = React.useState(() => loadPrefs(safeStore()));
  const [moreOpen, setMoreOpen] = React.useState(false);
  const [menuOpen, setMenuOpen] = React.useState(false);
  const [settingsOpen, setSettingsOpen] = React.useState(false);
  const storyRef = React.useRef(null);
  const composerWrapRef = React.useRef(null);
  const build0 = React.useRef(null);
  const [composerH, setComposerH] = React.useState(150);
  const interval = (cfg && cfg.message_interval_seconds) || DEFAULT_INTERVAL;
  const clearKeyErrors = React.useCallback(() => {
    setErrObj((e) => (opensSettings(e) ? null : e));
    setNewErr((n) => (n && opensSettings(n.error) ? null : n));
  }, []);
  React.useEffect(() => { clearKeyErrors(); }, [provider, hostedKey, clearKeyErrors]);
  React.useEffect(() => { if (!menuOpen) setNewErr(null); }, [menuOpen]);

  const loadDoc = (d) => {
    setDoc(d); setGate(null); setErrObj(null); setStreamAt(null);
    setAsides([]); setActiveTurn(null); setCompose(null); setMarks({});
  };
  const saves = useSaves(doc, loadDoc, client.checkSave);

  // config decides the mode; until it arrives the engine counts as down and no game request goes out
  React.useEffect(() => {
    if (cfg || engineDown) return undefined;
    let live = true;
    client.config().then((r) => {
      if (!live) return;
      // a body without a mode is not a config the page can use
      if (!r.ok || !r.data || typeof r.data !== 'object' || typeof r.data.mode !== 'string') { setEngineDown(true); return; }
      setCfg(r.data);
      if (r.data.tuned) setProvider(r.data.tuned);
    });
    return () => { live = false; };
  }, [client, cfg, engineDown]);
  React.useEffect(() => {
    client.packs().then((r) => { if (r.ok) setPacks(r.data.packs || []); });
    fetchBuild().then((b) => { build0.current = b; });
  }, [client]);
  React.useEffect(() => {
    client.configure({ mode: cfg ? cfg.mode : 'selfhost', provider, key: hostedKey || null });
  }, [client, cfg, provider, hostedKey]);

  // one health check: every 5 s while the engine is out of reach, every 30 s while it answers;
  // none while the tab is hidden. While down it also notes whether this tab is running an
  // older page than the server now holds. It never reads the key store.
  React.useEffect(() => {
    if (!cfg && !engineDown) return undefined;
    let live = true;
    let busy = false;
    let timer = null;
    let n = period(engineDown);
    const checkBuild = async () => {
      const b = await fetchBuild();
      if (live && b && build0.current && b !== build0.current) setStale(true);
    };
    if (engineDown) { setLeft(n); checkBuild(); }
    const check = async () => {
      if (busy) return;
      busy = true;
      const r = await client.health();
      busy = false;
      if (!live) return;
      const down = downAfter(engineDown, r);
      if (down === engineDown) {
        if (down) checkBuild();
        return;
      }
      setEngineDown(down);
      if (!down) {
        setStale(false);
        if (cfg && cfg.mode !== 'hosted') setKeysEpoch((e) => e + 1);
      }
    };
    const tick = () => {
      if (busy) return;
      const s = nextLeft(n, engineDown);
      n = s.left;
      if (engineDown) setLeft(s.check ? 0 : n);
      if (s.check) check();
    };
    const start = () => { if (!timer) timer = setInterval(tick, 1000); };
    const stop = () => { clearInterval(timer); timer = null; };
    const onVis = () => {
      if (document.visibilityState === 'hidden') { stop(); return; }
      n = period(engineDown);
      check();
      start();
    };
    document.addEventListener('visibilitychange', onVis);
    if (document.visibilityState !== 'hidden') start();
    return () => { live = false; stop(); document.removeEventListener('visibilitychange', onVis); };
  }, [engineDown, client, cfg]);

  // leaving the page asks first only while the open game has changes not written anywhere
  const warnLeave = shouldWarnOnLeave({ doc, saved: saves.saved, savedToFile: saves.savedToFile });
  React.useEffect(() => {
    if (!warnLeave) return undefined;
    const onLeave = (e) => { e.preventDefault(); e.returnValue = ''; };
    window.addEventListener('beforeunload', onLeave);
    return () => window.removeEventListener('beforeunload', onLeave);
  }, [warnLeave]);

  // keep the story's bottom padding equal to the composer's live height so the
  // newest message always clears the hovering composer
  React.useEffect(() => {
    const el = composerWrapRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => setComposerH(el.offsetHeight));
    ro.observe(el);
    setComposerH(el.offsetHeight);
    return () => ro.disconnect();
  }, []);
  const scrollBottom = () => {
    const el = storyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };
  const history = ((doc && doc.history) || []).map((h) => ({
    kind: h.role === 'user' ? 'player' : 'dm',
    body: h.content_en || h.content || '',
  }));
  React.useEffect(() => { scrollBottom(); }, [doc, asides]);

  // where: what the failed call asked for ('turn', 'regenerate', 'new' or 'other')
  const fail = (err, where) => {
    if (cfg && cfg.mode !== 'hosted' && opensSettings(err)) setKeysEpoch((e) => e + 1);
    if (err.kind === 'engine_down') { setEngineDown(true); setErrObj(null); } else setErrObj({ ...err, where });
  };
  const fade = React.useCallback(() => setCelebrate(null), []);

  // one call to the engine; true means the turn was played
  const exec = async (call, input, strict, where, answers) => {
    if (!cfg || saves.switching) return false;
    setBusy(true);
    setErrObj(null);
    setCelebrate(null);
    window.dispatchEvent(new CustomEvent('co-turn-start'));
    const from = saves.slotNow();
    const r = await call();
    window.dispatchEvent(new CustomEvent('co-turn-done'));
    setBusy(false);
    // another game was opened meanwhile: the result goes to the game it started from
    if (saves.slotNow() !== from) {
      if (r.ok && r.data && r.data.save) saves.land(from, r.data.save);
      return false;
    }
    if (!r.ok) { fail(r.error, where); return false; }
    if (strict && r.data.ok === false) { setErrObj({ kind: 'unknown' }); return false; }
    if (r.data.kind === 'system_reply') {
      // a free answer from the GameMaster: shown in the story for this session only
      setAsides((a) => [...a, { at: history.length, ask: input, body: r.data.narrative || '' }]);
      setGate(null);
      return true;
    }
    if (r.data.save) {
      if (milestone(doc, r.data.save)) setCelebrate({ chapter: r.data.save.state.chapter });
      saves.track(r.data.save);
      setDoc(r.data.save);
    }
    const g = gateOf(r.data);
    if (g) { setGate({ ...g, input, answers }); return false; }
    setGate(null);
    if (r.data.save) setStreamAt(r.data.save.history.length - 1);
    return true;
  };
  const run = (text, extra) => exec(() => client.turn(doc, text, extra), text, false, 'turn', extra);
  const onSend = (text) => { if (!doc) { setErrObj({ kind: 'no_game' }); return Promise.resolve(false); } return run(text, {}); };
  const onResolve = (opt) => run(gate.input, mergeAnswers(gate.answers, opt.extra || (gate.roll ? { [gate.roll]: randomDie(gate.die) } : {})));
  const onCompose = async (text) => {
    if (!doc || !compose) return false;
    const feedback = compose.kind === 'flag' ? t('composer.flag_prefix') + text : text;
    const ok = await exec(() => client.regenerate(doc, feedback), text, true, 'regenerate');
    if (ok) setCompose(null);
    return ok;
  };
  const onMaintSend = () => {
    setShake(true);
    setTimeout(() => setShake(false), 450);
  };

  const create = async (form) => {
    if (!cfg) return;
    const sw = saves.leave();
    if (!sw) return;
    setMenuOpen(false); setMoreOpen(false); setSettingsOpen(false);
    setCreating(form.pack.name); setDoc(null); setGate(null); setErrObj(null); setNewErr(null);
    setAsides([]); setActiveTurn(null); setCompose(null); setMarks({});
    try {
      const r = await client.newGame({
        name: form.name,
        profile: { name: form.character, notes: form.notes },
        config: { pack_id: form.pack.id },
      });
      setCreating(null);
      if (!r.ok) {
        if (r.error.kind === 'engine_down') { fail(r.error, 'new'); return; }
        if (cfg.mode !== 'hosted' && opensSettings(r.error)) setKeysEpoch((e) => e + 1);
        setNewErr({ error: r.error, draft: form.draft });
        setMenuOpen(true);
        return;
      }
      // a stored game was opened while this one was being made: keep the new one beside it
      if (saves.slotNow() !== null) { saves.land(null, r.data.save); return; }
      saves.adopt(r.data.save);
      if (saves.ask()) setMenuOpen(true);
      setStreamAt(0);
    } finally {
      saves.settle(sw);
    }
  };

  const undo = async () => {
    if (!cfg || saves.switching) return;
    const from = saves.slotNow();
    const r = await client.undo(doc);
    if (saves.slotNow() !== from) { if (r.ok && r.data.save) saves.land(from, r.data.save); return; }
    if (!r.ok) { fail(r.error, 'other'); return; }
    if (r.data.save) { saves.track(r.data.save); loadDoc(r.data.save); }
  };

  const onTurnAction = async (i, id, label) => {
    setActiveTurn(null);
    if (id === 'feedback' || id === 'flag') {
      setGate(null);
      setCompose({ kind: id, who: label });
    } else if (id === 'undo') {
      if (!cfg || saves.switching) return;
      const from = saves.slotNow();
      const r = await client.undoTo(doc, turnOf(doc, i));
      if (saves.slotNow() !== from) { if (r.ok && r.data.ok !== false && r.data.save) saves.land(from, r.data.save); return; }
      if (!r.ok) fail(r.error, 'other');
      else if (r.data.ok === false || !r.data.save) setErrObj({ kind: 'unknown' });
      else { saves.track(r.data.save); loadDoc(r.data.save); }
    } else if (id === 'fork') {
      const f = forkDoc(doc, i);
      if (!f) return;
      // with no passphrase, ask for one; any other failure is shown in the open menu
      const mode = await saves.saveFile(saves.plain, f, 'coherence-fork.json');
      if (mode !== 'plain' && mode !== 'encrypted') {
        if (mode === 'need_passphrase') saves.ask();
        else saves.reveal();
        setMenuOpen(true);
      }
    } else {
      setMarks((m) => ({ ...m, [i]: m[i] === id ? undefined : id }));
    }
  };

  const setPref = (patch) => setPrefs((p) => {
    const next = { ...p, ...patch };
    savePrefs(safeStore(), next);
    return next;
  });
  const hosted = !!cfg && cfg.mode === 'hosted';
  const notify = React.useCallback((text) => setToast((p) => ({ text, n: p ? p.n + 1 : 1 })), []);
  const unToast = React.useCallback(() => setToast(null), []);
  const forget = async () => {
    if (hosted) { setHostedKeys({}); notify(t('toast.keys.forgotten')); return; }
    const r = await client.keys.lock();
    setKeysEpoch((e) => e + 1);
    if (r.ok) {
      setStore((s) => ({ ...(s || {}), unlocked: false, keys: {}, error: null }));
      setLockEpoch((e) => e + 1);
      notify(t('toast.keys.locked'));
    }
  };
  const openKeys = () => { setMoreOpen(false); setMenuOpen(true); setSettingsOpen(true); setLockEpoch((e) => e + 1); };
  const link = linkStatus({ cfg, engineDown, hostedKey, hostedKeys, store, provider });
  const beginGate = link && link.level === 'attention' && link.key !== 'status.unchecked' ? link : null;

  const [voices, setVoices] = React.useState(() => {
    try {
      const s = JSON.parse(localStorage.getItem(VOICES_KEY));
      if (s && s.dm && s.dm.h !== undefined) return s;
    } catch {}
    return VOICE_DEFAULTS;
  });
  const setVoice = (k, patch) => setVoices((v) => {
    const next = { ...v, [k]: { ...v[k], ...patch } };
    try { localStorage.setItem(VOICES_KEY, JSON.stringify(next)); } catch {}
    return next;
  });
  const colors = {
    dm: voiceColor(voices.dm),
    player: voiceColor(voices.player),
    npc: voiceColor(voices.npc),
  };
  const state = (doc && doc.state) || {};
  const suggestions = (state.suggested_moves || []).filter((s) => typeof s === 'string');
  const npcNames = Object.values(state.npcs || {}).map((n) => n && n.name).filter(Boolean);
  const addressable = ['GameMaster', ...npcNames];
  const empty = history.length === 0 && !creating;

  // which alert the composer wears: maintenance, 451, 500, or a celebration
  const picked = pickAlert({ error: errObj, engineDown, milestone: !!celebrate, now: new Date() });
  // the saved-story text and the reload control only while this game is stored in the browser
  const maint = maintCopy(saves.saved && !saves.full);
  const kind = picked ? picked.kind : null;
  const alertView = !kind ? null : {
    maint: { label: t('alert.maint.label'), body: t(maint.body) },
    451: { label: t('alert.451.label'), body: t(blockedKey(errObj)) },
    500: { label: shudders(errObj) ? t('alert.500.label') : t('error.label'), body: errObj ? errorText(errObj, interval) : '' },
    grant: { label: t('alert.grant.label'), body: t('alert.grant.body', { chapter: celebrate ? celebrate.chapter : '' }) },
    xmas: { label: t('alert.xmas.label'), body: t('alert.xmas.body') },
  }[kind];
  return (
    <div className="stage">
      <div data-screen-label="Game screen" style={{
        height: '100%',
        background: '#0c0e0e',
        boxSizing: 'border-box',
        position: 'relative',
      }}>
        <div data-screen-label="Story" data-testid="story" ref={storyRef} className="no-scrollbar" onClick={() => setActiveTurn(null)} style={{
          position: 'absolute', inset: 0, overflow: 'auto',
          display: 'flex', flexDirection: 'column',
          gap: 30, padding: '124px 26px 0',
          paddingBottom: composerH + 46,
        }}>
          <div style={{ marginTop: 'auto' }}></div>
          {history.map((b, i) => (
            <React.Fragment key={i}>
              <TurnWrap open={activeTurn === i} onLongPress={() => setActiveTurn(i)}
                onAction={(id) => onTurnAction(i, id, b.kind === 'dm' ? 'GameMaster' : 'You')}
                enabled={saves.switching ? null : actionsFor(i, doc)} mark={marks[i]}>
                {b.kind === 'dm'
                  ? <DmBlock label="GameMaster" body={b.body} colors={colors} stream={streamAt === i} onGrow={scrollBottom} />
                  : <PlayerBlock label="You" body={b.body} playerColor={colors.player} revealed={activeTurn === i} />}
              </TurnWrap>
              {asides.filter((a) => a.at === i + 1).map((a, k) => (
                <React.Fragment key={'aside' + k}>
                  <AskBlock label={t('aside.ask')} body={a.ask} />
                  <GmAsideBlock label={t('aside.reply')} body={a.body} />
                </React.Fragment>
              ))}
            </React.Fragment>
          ))}
          {empty && (
            <div style={{ margin: 'auto 0', textAlign: 'center', padding: '0 24px', fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 17, lineHeight: 1.5, color: 'rgba(237,235,230,0.52)' }}>
              Open the menu to start a new story, or load a save.
            </div>
          )}
          {history.length === 0 && creating && (
            <div style={{ margin: 'auto 0', textAlign: 'center', padding: '0 24px' }}>
              <div style={{ ...blockLabel, fontSize: 9.5, color: 'rgba(64,224,208,0.75)', marginBottom: 14, animation: 'co-breathe 2.4s ease-in-out infinite' }}>The GameMaster is setting the scene</div>
              <div style={{ fontFamily: "'Newsreader', Georgia, serif", fontStyle: 'italic', fontSize: 17, lineHeight: 1.5, color: 'rgba(237,235,230,0.52)' }}>{creating}</div>
              <div style={{ display: 'flex', gap: 6, justifyContent: 'center', marginTop: 18 }}>
                {[0, 1, 2].map((i) => <span key={i} style={{ width: 5, height: 5, borderRadius: 3, background: '#40E0D0', animation: 'co-breathe 1.4s ease-in-out infinite', animationDelay: (i * 0.18) + 's' }}></span>)}
              </div>
            </div>
          )}
        </div>
        {/* chrome overlay - header spheres + panels float above the text */}
        <div style={{ position: 'absolute', top: 62, left: 0, right: 0, bottom: 34, zIndex: 5, pointerEvents: 'none' }}>
          {/* the row lets presses through to the story; only its buttons take them */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0 20px', flexShrink: 0, pointerEvents: 'none' }}>
            <DuoButton
              onMore={() => { setMoreOpen(o => !o); setMenuOpen(false); }} moreOpen={moreOpen}
              onMenu={() => { setMenuOpen(o => !o); setMoreOpen(false); setSettingsOpen(false); }} menuOpen={menuOpen} />
            <StatusIcon status={link} onCheck={() => setKeysEpoch((e) => e + 1)} />
          </div>
          <MorePanel open={moreOpen} onClose={() => setMoreOpen(false)} voices={voices} colors={colors} onVoice={setVoice} saveName={doc ? doc.name : 'No game'} doc={doc} />
          <MenuPanel open={menuOpen} onClose={() => { setMenuOpen(false); setSettingsOpen(false); }}
            beginReason={beginGate ? statusText(beginGate) : ''} newFailed={newErr} onOpenKeys={openKeys} interval={interval}
            onCreate={create} packs={packs} creating={!!creating || saves.switching} doc={doc} ask={saves.asking}
            rows={saves.rows} onOpenSlot={saves.openSlot} onRemoveSlot={saves.removeSlot} onVetSlot={saves.vetSlot} summaryShown={saves.summaryShown} switching={saves.switching}
            saves={<SavesSection s={saves} doc={doc} onUndo={undo} onDone={notify} />} />
          <SettingsPanel open={settingsOpen} prefs={prefs} onPrefs={setPref}
            forgetLabel={hosted ? t('keys.hosted.forget') : t('settings.lock_store')} onForget={forget}
            reveal={lockEpoch} forgetDisabled={!hosted && !!store && !store.unlocked}
            apiKeys={<KeysSection key={keysEpoch} client={client} cfg={cfg} provider={provider} onProvider={setProvider} hostedKey={hostedKey} onHostedKey={setHostedKey} onForget={forget} onDone={(text) => { notify(text); clearKeyErrors(); }} onStore={setStore} />} />
          <TravelSphere open={menuOpen} settingsOpen={settingsOpen} onClick={() => setSettingsOpen(s => !s)} />
        </div>
        {/* composer hovers over the story - the text wall scrolls beneath it */}
        <div ref={composerWrapRef} style={{ position: 'absolute', left: 0, right: 0, bottom: 34, zIndex: 4 }}>
          <TurnProgress />
          {/* a confirmation sits on top of any alert, in the same column, and fades on its own */}
          {toast && <ComposerAlert key={toast.n} kind="done" label={t('toast.label')} body={toast.text} onFade={unToast} />}
          {alertView && <ComposerAlert kind={kind} label={alertView.label} body={alertView.body} left={left} stale={stale && maint.refresh} onFade={fade}>
            {kind === '500' && opensSettings(errObj) && <button className="co-btn" data-testid="alert-settings" onClick={openKeys} style={{ alignSelf: 'center', marginTop: 6 }}>{t('error.open_settings')}</button>}
          </ComposerAlert>}
          {!alertView && saves.full && (
            <ComposerAlert kind="notice" label={t('storage.full.label')} body={t('storage.full')}>
              <button className="co-btn" data-testid="storage-full-save" onClick={() => saves.saveFile(false)} style={{ alignSelf: 'center', marginTop: 6 }}>{t('saves.save_file')}</button>
            </ComposerAlert>
          )}
          <Composer onSend={onSend} gate={gate} onResolve={onResolve} onRephrase={() => setGate(null)}
            suggestions={suggestions} addressable={addressable} busy={busy || !!creating || saves.switching} switching={saves.switching}
            alert={kind || (saves.full ? 'notice' : null)}
            compose={compose} onCompose={onCompose} onCancelCompose={() => setCompose(null)}
            maint={kind === 'maint'} shake={shake} onMaintSend={onMaintSend} submitKey={prefs.submitKey} />
        </div>
      </div>
    </div>
  );
}

export default App;
