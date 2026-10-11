// Each turn adds a player entry, then a GM entry; the newest entry is the GM entry of turn doc.turn.
// Counting back from it gives each entry's turn, also after older entries were folded into one
// summary entry. The first GM entry is the opening or that summary.
function place(doc, index) {
  const history = (doc && Array.isArray(doc.history)) ? doc.history : [];
  const entry = history[index];
  if (!Number.isInteger(index) || !entry) return null;
  if (history[history.length - 1].role !== 'assistant') return null;
  const total = Number.isInteger(doc.turn) ? doc.turn : 0;
  const back = history.length - 1 - index;
  const own = (index === 0 && entry.role === 'assistant') ? 0 : Math.max(0, total - Math.floor(back / 2));
  return { total, own, done: Math.max(0, total - Math.ceil(back / 2)) };
}

export function turnOf(doc, index) {
  const p = place(doc, index);
  return p ? p.own : 0;
}

// A copy of the save cut after the last complete turn at or before this entry, or null when
// the world state of that moment is no longer held (the undo stack is capped).
export function forkDoc(doc, index) {
  const p = place(doc, index);
  if (!p) return null;
  const turns = p.done;
  const total = p.total;
  const name = String(doc.name || '') + ' (fork)';
  if (turns >= total) return { ...doc, name };
  const stack = Array.isArray(doc.undo_stack) ? doc.undo_stack : [];
  let at = -1;
  for (let i = stack.length - 1; i >= 0; i -= 1) {
    if (stack[i] && stack[i].turn === turns) { at = i; break; }
  }
  if (at < 0) return null;
  const snap = stack[at];
  const history = Array.isArray(snap.history) ? snap.history : doc.history.slice(0, snap.history_len);
  const fork = { ...doc, name, state: snap.state, history, turn: snap.turn, undo_stack: stack.slice(0, at) };
  if (Array.isArray(snap.items)) fork.items = structuredClone(snap.items);
  if (Array.isArray(doc.memory)) fork.memory = doc.memory.filter((e) => !(e && Number.isInteger(e.turn) && e.turn > snap.turn));
  if (doc.checkpoint_len != null) {
    fork.checkpoint_history = doc.history.slice(0, doc.checkpoint_len);
    delete fork.checkpoint_len;
  }
  return fork;
}

export function actionsFor(index, doc) {
  const history = (doc && Array.isArray(doc.history)) ? doc.history : [];
  const entry = history[index];
  const last = history.length - 1;
  const turn = turnOf(doc, index);
  const depth = (doc && Array.isArray(doc.undo_stack)) ? doc.undo_stack.length : 0;
  const total = (doc && Number.isInteger(doc.turn)) ? doc.turn : 0;
  const lastGm = !!entry && index === last && entry.role === 'assistant' && index > 0;
  return {
    undo: !!entry && turn >= 1 && total - turn + 1 >= 1 && total - turn + 1 <= depth,
    fork: !!entry && forkDoc(doc, index) !== null,
    comment: lastGm && depth > 0,
    flag: lastGm && depth > 0,
    rate: !!entry,
  };
}
