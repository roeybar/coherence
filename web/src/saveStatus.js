// Whether the page may call the open game saved after a write finished: the result must
// be a success and the document written must still be the one the page shows.
export function savedAfterWrite(writtenDoc, currentDoc, result) {
  return writtenDoc === currentDoc && !!(result && result.ok);
}

// Whether leaving the page would lose the open game: a game is open and its current
// document is neither confirmed written in this browser nor the one last saved to or
// loaded from a file.
export function shouldWarnOnLeave({ doc, saved, savedToFile }) {
  return !!doc && !saved && !savedToFile;
}

// Which note the Saves section shows. Without a passphrase: stored games found, or off.
// With one and no open game: autosave is armed for the next game. With one and a game:
// pending while its write runs, saved once confirmed, else off.
export function noteKey({ saving, saved, full, stored, key, hasDoc }) {
  if (!key) return stored ? 'saves.found' : 'saves.off';
  if (!hasDoc) return 'saves.armed';
  if (saving) return 'saves.saving';
  return saved && !full ? 'saves.on' : 'saves.off';
}
