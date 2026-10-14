import { test, expect, Page, Locator } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { KEY, TEXT, story, dmBlocks, openMenu, closeMenu, openSettings, addSelfHostKey, ensureSelfHostKey, newGame, send, longPress, idbRecords, slotCount, slotRows, setSavePass, enterSlot, clickUnlock, closeSettings } from './helpers';

const DEMO_SAVE = process.env.DEMO_SAVE || '/examples/demo-save.json';

test.describe.configure({ mode: 'serial' });
test.setTimeout(240_000);

test('unlock the store, add a key, and the key is never shown again', async ({ page }) => {
  await page.goto('/');
  // a page with no key store session reads as locked (the fresh-volume check is in scripts/test.sh)
  await expect(page.getByTestId('link-status')).toHaveAttribute('data-level', 'attention');
  await expect(page.getByTestId('link-status')).toHaveAttribute('aria-label', 'Key store locked: unlock it in Settings.');
  await addSelfHostKey(page);
  await page.reload();
  await openSettings(page);
  await expect(page.getByTestId('key-status')).toContainText('Key set');
  await expect(page.getByTestId('apikey-input')).toHaveValue('');
  expect(await page.content()).not.toContain(KEY);
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain(KEY);
  expect(JSON.stringify(await idbRecords(page))).not.toContain(KEY);
});

test('new game, two turns, encrypted save, reload and load from file', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  for (const n of [1, 2]) {
    await send(page, `${TEXT} ${n}`);
    await expect(story(page)).toContainText(`${TEXT} ${n}`);
    await expect(dmBlocks(page)).toHaveCount(n + 1, { timeout: 60_000 });
  }

  // an encrypted autosave and an encrypted save file
  await openMenu(page);
  await page.getByTestId('save-passphrase').fill('e2e-save-pass');
  await page.getByTestId('save-unlock').click();
  await expect.poll(() => slotCount(page)).toBe(1);
  const stored = Object.values(await idbRecords(page))[0];
  expect(stored).not.toContain(TEXT);
  expect(JSON.parse(stored as string).ct).toBeTruthy();

  const [encrypted] = await Promise.all([page.waitForEvent('download'), page.getByTestId('save-file').click()]);
  const encryptedText = readFileSync((await encrypted.path()) as string, 'utf8');
  expect(encryptedText).not.toContain(TEXT);

  // the plain-JSON file parses
  await page.getByTestId('save-plain').check();
  const [plain] = await Promise.all([page.waitForEvent('download'), page.getByTestId('save-file').click()]);
  const parsed = JSON.parse(readFileSync((await plain.path()) as string, 'utf8'));
  expect(parsed.format).toBe('coherence-save');
  expect(JSON.stringify(parsed.save.history)).toContain(`${TEXT} 2`);

  // after a reload the game is gone until a file is loaded
  await page.reload();
  await expect(story(page)).not.toContainText(TEXT);
  await openMenu(page);
  await page.getByTestId('save-passphrase').fill('e2e-save-pass');
  await page.getByTestId('load-file').setInputFiles({ name: 'save.json', mimeType: 'application/json', buffer: Buffer.from(encryptedText) });
  await expect(story(page)).toContainText(`${TEXT} 2`);
});

test('the sample save loads from a file and shows its opening', async ({ page }) => {
  const demo = JSON.parse(readFileSync(DEMO_SAVE, 'utf8'));
  const opening: string = demo.save.history[0].content_en || demo.save.history[0].content;
  await page.goto('/');
  await openMenu(page);
  await page.getByTestId('load-file').setInputFiles(DEMO_SAVE);
  await expect(story(page)).toContainText(opening.slice(0, 40));
});

test('with no config the page shows maintenance, then the key controls once the engine answers', async ({ page }) => {
  await page.route('**/api/config', (route) => route.fulfill({
    status: 502, contentType: 'application/json', body: '{"detail":{"error":"engine_unavailable"}}',
  }));
  await page.goto('/');
  await expect(page.getByTestId('composer-alert')).toHaveAttribute('data-kind', 'maint');
  await openSettings(page);
  const keyUi = page.getByTestId('vault-passphrase').or(page.getByTestId('apikey-input'));
  await expect(keyUi).toHaveCount(0);
  await page.unroute('**/api/config');
  await expect(page.getByTestId('composer-alert')).toHaveCount(0, { timeout: 30_000 });
  await expect(keyUi.first()).toBeVisible();
});

const turns = (page: Page) => story(page).getByTestId('turn');

async function playOneTurn(page: Page) {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await send(page, `${TEXT} 1`);
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });
}

test('Fork with a save passphrase downloads an encrypted save file', async ({ page }) => {
  await playOneTurn(page);
  await openMenu(page);
  await page.getByTestId('save-passphrase').fill('e2e-save-pass');
  await page.getByTestId('save-unlock').click();
  await expect.poll(() => slotCount(page)).toBe(1);
  await closeMenu(page);
  await longPress(page, turns(page).nth(2));
  const [file] = await Promise.all([
    page.waitForEvent('download'),
    turns(page).nth(2).getByTestId('turn-fork').click(),
  ]);
  const text = readFileSync((await file.path()) as string, 'utf8');
  const parsed = JSON.parse(text);
  expect(parsed.format).toBe('coherence-save');
  expect(parsed.ct).toBeTruthy();
  expect(parsed.save).toBeUndefined();
  expect(text).not.toContain(TEXT);
});

test('Fork with no passphrase and plain not ticked asks for a passphrase and downloads nothing', async ({ page }) => {
  await playOneTurn(page);
  let downloads = 0;
  page.on('download', () => { downloads += 1; });
  // clear the new-game prompt with a plain save file, then untick plain
  await openMenu(page);
  await page.getByTestId('save-plain').check();
  await Promise.all([page.waitForEvent('download'), page.getByTestId('save-file').click()]);
  downloads = 0;
  await page.getByTestId('save-plain').uncheck();
  await expect(page.getByTestId('saves-error')).toHaveCount(0);
  await closeMenu(page);
  await longPress(page, turns(page).nth(2));
  await turns(page).nth(2).getByTestId('turn-fork').click();
  await expect(page.getByTestId('saves-error')).toBeVisible();
  await expect(page.getByTestId('saves-error')).toHaveText('Set a save passphrase first, or tick plain JSON.');
  await page.waitForTimeout(1_000);
  expect(downloads).toBe(0);
});

test('Fork with no passphrase and plain ticked downloads plain JSON', async ({ page }) => {
  await playOneTurn(page);
  await openMenu(page);
  await page.getByTestId('save-plain').check();
  await closeMenu(page);
  await longPress(page, turns(page).nth(2));
  const [file] = await Promise.all([
    page.waitForEvent('download'),
    turns(page).nth(2).getByTestId('turn-fork').click(),
  ]);
  const parsed = JSON.parse(readFileSync((await file.path()) as string, 'utf8'));
  expect(parsed.format).toBe('coherence-save');
  expect(parsed.ct).toBeUndefined();
  expect(JSON.stringify(parsed.save.history)).toContain(`${TEXT} 1`);
});

const P1 = 'e2e-save-pass';
const WRONG = 'Wrong passphrase, or the data is damaged.';
const tagged = (tag: string) => `${TEXT} ${tag}`;
const CONTEXT_TOO_LARGE = "This story's world state has grown too large for the game to send to the model in one move, so your move was not played. Load an earlier save or start a new game.";
const PROVIDER_UNAVAILABLE = 'The provider is not responding right now. Your move was not played. Try again in a moment.';

// a new game with one played turn, told apart by its tag
async function game(page: Page, tag: string) {
  await newGame(page);
  await send(page, tagged(tag));
  await expect(story(page)).toContainText(tagged(tag));
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });
}

async function storedGame(page: Page, tag: string) {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await game(page, tag);
  await setSavePass(page);
  await expect.poll(() => slotCount(page)).toBe(1);
  await closeMenu(page);
}

const delay = (page: Page, url: string, ms = 8_000) => page.route(url, async (route) => {
  await new Promise((r) => setTimeout(r, ms));
  await route.continue();
});

test('New game keeps the stored game, and the list opens it', async ({ page }) => {
  await storedGame(page, 'A');
  const before = await idbRecords(page);
  await game(page, 'B');
  await expect.poll(() => slotCount(page)).toBe(2);
  const after = await idbRecords(page);
  for (const [k, v] of Object.entries(before)) expect(after[k]).toBe(v);
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('A'));
  await expect(story(page)).not.toContainText(tagged('B'));
});

test('New game over an unsaved game keeps it in memory, and a passphrase stores both', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await game(page, 'A');
  await game(page, 'B');
  await openMenu(page);
  await expect(slotRows(page)).toHaveCount(1);
  await expect(page.getByTestId('slot-kept')).toHaveText('Not saved');
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('A'));
  await setSavePass(page);
  await expect.poll(() => slotCount(page)).toBe(2);
});

test('Load from file over an unsaved game keeps it', async ({ page }) => {
  const demo = JSON.parse(readFileSync(DEMO_SAVE, 'utf8'));
  const opening: string = demo.save.history[0].content_en || demo.save.history[0].content;
  await page.goto('/');
  await ensureSelfHostKey(page);
  await game(page, 'A');
  await openMenu(page);
  await page.getByTestId('load-file').setInputFiles(DEMO_SAVE);
  await expect(story(page)).toContainText(opening.slice(0, 40));
  await expect(slotRows(page)).toHaveCount(1);
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('A'));
});

test('Open a stored game over an unsaved one keeps both', async ({ page }) => {
  await storedGame(page, 'A');
  const before = await idbRecords(page);
  await page.reload();
  await ensureSelfHostKey(page);
  await game(page, 'B');
  await openMenu(page);
  await expect(page.getByTestId('saves-note')).toHaveText('This browser has saved games. Enter the save passphrase to open them.');
  await page.getByTestId('save-passphrase').fill(P1);
  await page.getByTestId('save-open').click();
  await expect(story(page)).toContainText(tagged('A'));
  await expect(story(page)).not.toContainText(tagged('B'));
  await expect.poll(() => slotCount(page)).toBe(2);
  const after = await idbRecords(page);
  for (const [k, v] of Object.entries(before)) expect(after[k]).toBe(v);
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('B'));
});

test('A different passphrase changes no stored game; the new-passphrase button stores beside them', async ({ page }) => {
  await storedGame(page, 'A');
  await page.evaluate(() => localStorage.setItem('coherence.save', '{"format":"coherence-save","v":1}'));
  await page.reload();
  await expect.poll(async () => (await idbRecords(page)).legacy).toBe('{"format":"coherence-save","v":1}');
  const before = await idbRecords(page);
  await ensureSelfHostKey(page);
  await game(page, 'B');
  await setSavePass(page, 'e2e-other-pass');
  await expect(page.getByTestId('saves-error')).toHaveText(WRONG);
  await expect(page.getByTestId('save-new-passphrase')).toHaveText('Start saving with this new passphrase');
  expect(await idbRecords(page)).toEqual(before);
  await page.getByTestId('save-new-passphrase').click();
  await expect.poll(() => slotCount(page)).toBe(3);
  const after = await idbRecords(page);
  for (const [k, v] of Object.entries(before)) expect(after[k]).toBe(v);
  await expect(page.getByTestId('saves-locked')).toBeVisible();
  // the first passphrase opens none of this page's games now, so it is refused
  await page.getByTestId('save-passphrase').fill(P1);
  await page.getByTestId('save-unlock').click();
  await expect(page.getByTestId('saves-error')).toHaveText(WRONG);
  await expect(page.getByTestId('save-new-passphrase')).toHaveCount(0);
  await page.reload();
  await setSavePass(page, P1);
  await expect(story(page)).toContainText(tagged('A'));
});

test('Fork leaves every stored game unchanged', async ({ page }) => {
  await storedGame(page, 'A');
  const before = await idbRecords(page);
  await longPress(page, turns(page).nth(2));
  await Promise.all([page.waitForEvent('download'), turns(page).nth(2).getByTestId('turn-fork').click()]);
  await page.waitForTimeout(1_000);
  expect(await idbRecords(page)).toEqual(before);
  await expect(story(page)).toContainText(tagged('A'));
});

test('A late turn lands in its own game, not the one opened meanwhile', async ({ page }) => {
  await storedGame(page, 'A');
  await game(page, 'B');
  await expect.poll(() => slotCount(page)).toBe(2);
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('A'));
  await delay(page, '**/api/game/turn');
  const done = page.waitForResponse('**/api/game/turn');
  await send(page, tagged('late'));
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('B'));
  await done;
  await page.waitForTimeout(1_000);
  await expect(story(page)).not.toContainText(tagged('late'));
  await openMenu(page);
  await expect(slotRows(page)).toHaveCount(1);
  await expect(story(page)).not.toContainText(tagged('late'));
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('late'));
});

// The turn may return before, during or after the switch; in every case it stays with A.
test('A turn that returns while another game opens stays with its own game', async ({ page }) => {
  await storedGame(page, 'A');
  await game(page, 'B');
  await expect.poll(() => slotCount(page)).toBe(2);
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('A'));
  await delay(page, '**/api/game/turn', 300);
  const done = page.waitForResponse('**/api/game/turn');
  await send(page, tagged('quick'));
  await enterSlot(page);
  await done;
  await expect(story(page)).toContainText(tagged('B'));
  await expect(story(page)).not.toContainText(tagged('quick'));
  await enterSlot(page);
  await expect(story(page)).toContainText(tagged('quick'));
});

test('While a new game is being made no stored game can be entered; the new game is kept beside them', async ({ page }) => {
  const packs = (await (await page.request.get('/api/packs')).json()).packs;
  await storedGame(page, 'A');
  await delay(page, '**/api/game/new');
  const done = page.waitForResponse('**/api/game/new');
  await openMenu(page);
  await page.getByRole('button', { name: 'New save' }).click();
  await page.getByRole('button', { name: packs[0].name }).and(page.locator(':not([aria-label^="Enter "])')).first().click();
  await page.getByPlaceholder('a name for your character').fill('Tobin');
  await page.getByRole('button', { name: 'Begin story' }).click();
  await openMenu(page);
  const enter = page.getByTestId('save-slot-enter').first();
  const sendButton = page.getByRole('button', { name: 'Send', exact: true });
  await expect(enter).toHaveAttribute('aria-disabled', 'true');
  await expect(sendButton).toBeDisabled();
  await done;
  await expect(enter).not.toHaveAttribute('aria-disabled', 'true');
  await expect(sendButton).toBeEnabled();
  await expect(slotRows(page)).toHaveCount(1);
  await expect.poll(() => slotCount(page)).toBe(2);
});

test('While a stored game opens, the other rows and the composer send are disabled until it settles', async ({ page }) => {
  await storedGame(page, 'A');
  await game(page, 'B');
  await game(page, 'C');
  await expect.poll(() => slotCount(page)).toBe(3);
  await openMenu(page);
  await expect(page.getByTestId('save-slot-enter')).toHaveCount(2);
  // record the controls from the click until the open has settled
  const seen = await page.evaluate(() => new Promise<string[]>((resolve) => {
    const enters = () => [...document.querySelectorAll('[data-testid="save-slot-enter"]')];
    const sendButton = () => document.querySelector('button[aria-label="Send"]') as HTMLButtonElement;
    const log: string[] = [];
    const note = () => log.push(enters().map((e) => e.getAttribute('aria-disabled') === 'true').join(',') + '|' + sendButton().disabled);
    const watch = new MutationObserver(note);
    watch.observe(document.body, { subtree: true, childList: true, attributes: true });
    (enters()[0] as HTMLElement).click();
    setTimeout(() => { watch.disconnect(); note(); resolve(log); }, 3_000);
  }));
  expect(seen.some((s) => s.endsWith('|true') && !s.split('|')[0].split(',').includes('false'))).toBe(true);
  expect(seen[seen.length - 1]).toBe('false,false|false');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await openMenu(page);
  await expect(page.getByTestId('save-slot-enter').first()).not.toHaveAttribute('aria-disabled', 'true');
});

test('While a stored game opens, a shown gate cannot be answered; the opened game is shown and no record changes', async ({ page }) => {
  await storedGame(page, 'A');
  await game(page, 'B');
  await expect.poll(() => slotCount(page)).toBe(2);
  const gates = [
    { kind: 'addressee_clarification_needed', message: 'gate-e2e', options: ['gate-north', 'gate-south'] },
    { kind: 'dice_roll_needed', message: 'gate-e2e', dice_sides: 20 },
  ];
  for (const [i, body] of gates.entries()) {
    await page.route('**/api/game/turn', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }));
    await send(page, tagged('gate'));
    await expect(page.getByText('gate-e2e')).toBeVisible();
    await page.unroute('**/api/game/turn');
    const before = await idbRecords(page);
    await openMenu(page);
    // record the gate controls from the click until the open has settled
    const seen = await page.evaluate(() => new Promise<string[]>((resolve) => {
      const controls = () => [
        ...[...document.querySelectorAll('[role="button"]')].filter((e) => (e.textContent || '').startsWith('gate-')),
        ...document.querySelectorAll('button[aria-label^="Roll d"]'),
      ];
      const off = (e: Element) => e.getAttribute('aria-disabled') === 'true' || (e as HTMLButtonElement).disabled === true;
      const log: string[] = [];
      const note = () => { const c = controls(); if (c.length) log.push(c.map(off).join(',')); };
      const watch = new MutationObserver(note);
      watch.observe(document.body, { subtree: true, childList: true, attributes: true });
      (document.querySelector('[data-testid="save-slot-enter"]') as HTMLElement).click();
      setTimeout(() => { watch.disconnect(); resolve(log); }, 3_000);
    }));
    expect(seen.some((s) => !s.split(',').includes('false'))).toBe(true);
    await expect(story(page)).toContainText(tagged(i === 0 ? 'A' : 'B'));
    const after = await idbRecords(page);
    expect(after).toEqual(before);
  }
});

test('Fork on a page without encryption shows why in the open menu', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(Crypto.prototype, 'subtle', { get: () => undefined }));
  await playOneTurn(page);
  await longPress(page, turns(page).nth(2));
  await turns(page).nth(2).getByTestId('turn-fork').click();
  await expect(page.getByTestId('saves-error')).toHaveText('Encryption needs a secure page (https or localhost). Use plain JSON, or open the game over https.');
  await expect(page.getByRole('button', { name: 'Menu', exact: true })).toHaveAttribute('aria-expanded', 'true');
});

test('a config body that is not a config shows maintenance', async ({ page }) => {
  await page.route('**/api/config', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: 'null' }));
  await page.goto('/');
  await expect(page.getByTestId('composer-alert')).toHaveAttribute('data-kind', 'maint');
});

test('Two tabs: a game stored in one is listed in the other, and both keep their turns', async ({ page, context }) => {
  const page2 = await context.newPage();
  await page2.goto('/');
  await setSavePass(page2);
  await storedGame(page, 'A');
  await openMenu(page2);
  await expect(slotRows(page2)).toHaveCount(1, { timeout: 5_000 });
  await ensureSelfHostKey(page2);
  await enterSlot(page2);
  await expect(story(page2)).toContainText(tagged('A'));
  await send(page2, tagged('two'));
  await expect(story(page2)).toContainText(tagged('two'));
  await expect(dmBlocks(page2)).toHaveCount(3, { timeout: 60_000 });
  await send(page, tagged('one'));
  await expect(dmBlocks(page)).toHaveCount(3, { timeout: 60_000 });
  await expect.poll(() => slotCount(page)).toBe(2);
});

// a save picker whose files are kept in the page
const PICKER = () => {
  const w = window as any;
  w.__files = [];
  w.showSaveFilePicker = async () => ({
    createWritable: async () => {
      let text = '';
      return { write: async (t: string) => { text += t; }, close: async () => { w.__files.push(text); } };
    },
  });
};

async function removeFirst(page: Page) {
  await openMenu(page);
  await slotRows(page).first().click();
  await page.getByTestId('save-slot-remove').first().click();
}

test('Save to file and remove: two games written through the picker, then gone; files load back', async ({ page }) => {
  await page.addInitScript(PICKER);
  await storedGame(page, 'A');
  await game(page, 'B');
  await game(page, 'C');
  await expect.poll(() => slotCount(page)).toBe(3);
  await openMenu(page);
  await expect(page.getByTestId('save-row').locator('..').getByTestId('save-slot-remove')).toHaveCount(0);
  await removeFirst(page);
  await expect.poll(() => slotCount(page)).toBe(2);
  await page.getByTestId('save-plain').check();
  await removeFirst(page);
  await expect.poll(() => slotCount(page)).toBe(1);
  await expect(story(page)).toContainText(tagged('C'));
  const files: string[] = await page.evaluate(() => (window as any).__files);
  expect(files.length).toBe(2);
  expect(JSON.parse(files[0]).ct).toBeTruthy();
  expect(JSON.parse(files[1]).save).toBeTruthy();
  await page.getByTestId('save-plain').uncheck();
  const seen = new Set<string>();
  for (const [i, text] of files.entries()) {
    await openMenu(page);
    const before = await story(page).innerText();
    const checked = page.waitForResponse('**/api/game/check');
    await page.getByTestId('load-file').setInputFiles({ name: `f${i}.json`, mimeType: 'application/json', buffer: Buffer.from(text) });
    expect((await checked).status()).toBe(200);
    await expect.poll(() => story(page).innerText()).not.toBe(before);
    await expect(story(page)).toContainText(/canary-player-text-91c2 [AB]/);
    seen.add((await story(page).innerText()).includes(tagged('A')) ? 'A' : 'B');
  }
  expect([...seen].sort()).toEqual(['A', 'B']);
});

test('Save to file and remove: a cancelled picker leaves the record', async ({ page }) => {
  await page.addInitScript(() => {
    (window as any).showSaveFilePicker = async () => { throw new DOMException('cancelled', 'AbortError'); };
  });
  await storedGame(page, 'A');
  await game(page, 'B');
  await expect.poll(() => slotCount(page)).toBe(2);
  const before = await idbRecords(page);
  await removeFirst(page);
  await page.waitForTimeout(1_500);
  expect(await idbRecords(page)).toEqual(before);
});

test('Save to file and remove without a picker downloads the file, then removes the record', async ({ page }) => {
  await page.addInitScript(() => { delete (window as any).showSaveFilePicker; });
  await storedGame(page, 'A');
  await game(page, 'B');
  await expect.poll(() => slotCount(page)).toBe(2);
  const [file] = await Promise.all([page.waitForEvent('download'), removeFirst(page)]);
  expect(JSON.parse(readFileSync((await file.path()) as string, 'utf8')).ct).toBeTruthy();
  await expect.poll(() => slotCount(page)).toBe(1);
});

test('The old single browser save moves into the store byte for byte and opens', async ({ page, browser }, testInfo) => {
  await storedGame(page, 'A');
  await openMenu(page);
  const [file] = await Promise.all([page.waitForEvent('download'), page.getByTestId('save-file').click()]);
  const text = readFileSync((await file.path()) as string, 'utf8');
  const ctx = await browser.newContext({ baseURL: testInfo.project.use.baseURL });
  const p = await ctx.newPage();
  await p.goto('/');
  await p.evaluate((t) => localStorage.setItem('coherence.save', t), text);
  await p.reload();
  await expect.poll(async () => (await idbRecords(p)).legacy).toBe(text);
  expect(await p.evaluate(() => localStorage.getItem('coherence.save'))).toBeNull();
  await setSavePass(p);
  await expect(story(p)).toContainText(tagged('A'));
  await ctx.close();
});

test('A write the browser aborts after the put shows Not saved and keeps the last record', async ({ page }) => {
  await page.addInitScript(() => {
    const put = IDBObjectStore.prototype.put;
    let n = 0;
    IDBObjectStore.prototype.put = function (...a: any[]) {
      const r = put.apply(this, a as any);
      if (this.name === 'saves' && ++n > 1) {
        const tx = this.transaction;
        queueMicrotask(() => { try { tx.abort(); } catch { /* already finished */ } });
      }
      return r;
    };
  });
  await storedGame(page, 'A');
  const before = await idbRecords(page);
  await send(page, tagged('two'));
  await expect(page.getByTestId('storage-full-save')).toBeVisible({ timeout: 60_000 });
  expect(await idbRecords(page)).toEqual(before);
});

for (const v of [1, 2]) {
  test(`a database without the saves store (version ${v}) is reopened and works`, async ({ page }) => {
    await page.goto('/');
    await page.evaluate((ver) => new Promise<void>((resolve) => {
      const del = indexedDB.deleteDatabase('coherence');
      del.onsuccess = del.onerror = del.onblocked = () => {
        const req = indexedDB.open('coherence', ver);
        req.onupgradeneeded = () => { req.result.createObjectStore('other'); };
        req.onsuccess = () => { req.result.close(); resolve(); };
      };
    }), v);
    await page.reload();
    await ensureSelfHostKey(page);
    await game(page, 'A');
    await setSavePass(page);
    await expect.poll(() => slotCount(page)).toBe(1);
    await expect(page.getByTestId('saves-note')).toHaveText('Saved in this browser.');
  });
}

test('a database that never opens: after 3 s the game plays on with Not saved', async ({ page }) => {
  await page.addInitScript(() => { IDBFactory.prototype.open = function () { return {} as any; }; });
  await page.goto('/');
  await ensureSelfHostKey(page);
  await game(page, 'A');
  await setSavePass(page);
  await expect(page.getByTestId('storage-full-save')).toBeVisible({ timeout: 30_000 });
  await closeMenu(page);
  await send(page, tagged('two'));
  await expect(dmBlocks(page)).toHaveCount(3, { timeout: 60_000 });
});


async function engineDownOnTurn(page: Page) {
  await page.route('**/build.json', (route) => route.fulfill({
    status: 200, contentType: 'application/json', body: '{"build":"e2e-newer-page"}',
  }));
  await page.route('**/api/game/turn', (route) => route.fulfill({
    status: 502, contentType: 'application/json', body: '{"detail":{"error":"engine_unavailable"}}',
  }));
  const built = page.waitForResponse('**/build.json');
  await send(page, TEXT);
  const alert = page.getByTestId('composer-alert');
  await expect(alert).toHaveAttribute('data-kind', 'maint');
  await built;
  await expect(page.getByTestId('maint-countdown')).toHaveText('0:04');
  return alert;
}

test('maintenance over a game that is not stored asks for a save file and offers no reload', async ({ page }) => {
  await playOneTurn(page);
  const alert = await engineDownOnTurn(page);
  await expect(alert).toContainText('Your story is still open in this tab. Use "Save to file" before you reload.');
  await expect(alert).not.toContainText('your story is saved');
  await expect(page.getByTestId('hard-refresh')).toHaveCount(0);
});

test('maintenance over a stored game keeps the saved text', async ({ page }) => {
  await playOneTurn(page);
  await openMenu(page);
  await page.getByTestId('save-passphrase').fill('e2e-save-pass');
  await page.getByTestId('save-unlock').click();
  await expect.poll(() => slotCount(page)).toBe(1);
  await closeMenu(page);
  const alert = await engineDownOnTurn(page);
  await expect(alert).toContainText('The realm is being reshaped - your story is saved.');
  await expect(alert).not.toContainText('Save to file');
  await expect(page.getByTestId('hard-refresh')).toHaveCount(1);
});

test('With the context window on, the story keeps every turn, and Undo and Fork act on the pressed turn', async ({ page }) => {
  test.setTimeout(600_000);
  const centred = async (t: Locator) => {
    await t.evaluate((e) => e.scrollIntoView({ block: 'center' }));
    await (await t.elementHandle())!.waitForElementState('stable');
  };
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  for (let n = 1; n <= 23; n += 1) {
    const tag = `${TEXT} w${String(n).padStart(2, '0')}`;
    await send(page, tag);
    await expect(turns(page)).toHaveCount(2 * n + 1, { timeout: 60_000 });
    await expect(story(page)).toContainText(tag);
  }
  await expect(turns(page).nth(1)).toContainText(`${TEXT} w01`);

  await openMenu(page);
  await page.getByTestId('save-plain').check();
  await closeMenu(page);
  await centred(turns(page).nth(16));
  await longPress(page, turns(page).nth(16));
  const [file] = await Promise.all([
    page.waitForEvent('download'),
    turns(page).nth(16).getByTestId('turn-fork').click(),
  ]);
  const parsed = JSON.parse(readFileSync((await file.path()) as string, 'utf8'));
  expect(parsed.save.turn).toBe(8);
  expect(parsed.save.history).toHaveLength(17);
  const forked = JSON.stringify(parsed.save.history);
  expect(forked).toContain(`${TEXT} w08`);
  expect(forked).not.toContain(`${TEXT} w09`);

  await centred(turns(page).nth(39));
  await longPress(page, turns(page).nth(39));
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/game/undo-to-turn')),
    turns(page).nth(39).getByTestId('turn-undo').click(),
  ]);
  expect(req.postDataJSON().turn_index).toBe(20);
  await expect(story(page)).toContainText(`${TEXT} w19`);
  for (const gone of ['w20', 'w21', 'w22', 'w23']) await expect(story(page)).not.toContainText(`${TEXT} ${gone}`);
  await expect(turns(page)).toHaveCount(39);
});

test('the saved text appears only after the write of the current game finishes', async ({ page }) => {
  await page.addInitScript(() => {
    const put = IDBObjectStore.prototype.put;
    // while held, the transaction is kept open with reads, and the put runs once released
    IDBObjectStore.prototype.put = function (...a: any[]) {
      if (this.name !== 'saves' || !(window as any).holdPut) return put.apply(this, a as any);
      const os = this;
      const spin = () => {
        if ((window as any).holdPut) os.get('hold').onsuccess = spin;
        else put.apply(os, a as any);
      };
      const first = os.get('hold');
      first.onsuccess = spin;
      return first as any;
    };
  });
  await storedGame(page, 'A');
  await page.evaluate(() => { (window as any).holdPut = true; });
  await send(page, tagged('B'));
  await expect(story(page)).toContainText(tagged('B'));
  await expect(dmBlocks(page)).toHaveCount(3, { timeout: 60_000 });
  await openMenu(page);
  const note = page.getByTestId('saves-note');
  await expect(note).toHaveText('Saving in this browser...');
  await page.waitForTimeout(1000);
  await expect(note).toHaveText('Saving in this browser...');
  await expect(note).not.toHaveText('Saved in this browser.');
  await page.evaluate(() => { (window as any).holdPut = false; });
  await expect(note).toHaveText('Saved in this browser.', { timeout: 30_000 });
});

test('with no game loaded a passphrase arms autosave, and the next game is saved', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await setSavePass(page);
  const note = page.getByTestId('saves-note');
  await expect(note).toHaveText('Autosave is on. Your next new or loaded game is saved in this browser.');
  await closeMenu(page);
  await game(page, 'A');
  await expect.poll(() => slotCount(page)).toBe(1);
  await openMenu(page);
  await expect(note).toHaveText('Saved in this browser.');
});

test('starting autosave with an empty passphrase shows the need-passphrase message', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openMenu(page);
  await page.getByTestId('save-unlock').click();
  await expect(page.getByTestId('saves-error')).toHaveText('Set a save passphrase first, or tick plain JSON.');
  await expect(page.getByTestId('saves-note')).toHaveText('Not saved in this browser yet. Set a passphrase to turn on autosave.');
});

test('starting autosave on a page without encryption shows why', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(Crypto.prototype, 'subtle', { get: () => undefined }));
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openMenu(page);
  await page.getByTestId('save-passphrase').fill('abc');
  await page.getByTestId('save-unlock').click();
  await expect(page.getByTestId('saves-error')).toHaveText('Encryption needs a secure page (https or localhost). Use plain JSON, or open the game over https.');
});

const toastBody = (page: Page) => page.getByTestId('composer-toast').locator('span').nth(1);
const icon = (page: Page) => page.getByTestId('link-status');
const STATUS_OK = 'Game server reachable, and a key is stored for this provider.';
const STATUS_LOCKED = 'Key store locked: unlock it in Settings.';
const STATUS_DOWN = 'Game server not reachable: the game checks again on its own.';
const STATUS_NO_KEY = /^No key for .+: add one in Settings\.$/;

async function expectKeyFieldAttrs(field: Locator) {
  for (const [k, v] of [['type', 'text'], ['autocomplete', 'off'], ['spellcheck', 'false'], ['autocapitalize', 'off'],
    ['autocorrect', 'off'], ['data-form-type', 'other'], ['data-1p-ignore', ''], ['data-lpignore', 'true'], ['data-bwignore', '']]) {
    await expect(field).toHaveAttribute(k, v);
  }
  expect(await field.getAttribute('id')).not.toMatch(/pass/i);
  expect(await field.getAttribute('name')).not.toMatch(/pass/i);
  expect(await field.evaluate((el) => el.closest('form'))).toBeNull();
}

const pasteInto = (field: Locator, text: string) => field.evaluate((el, s) => {
  const dt = new DataTransfer();
  dt.setData('text/plain', s);
  el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
}, text);

test('the key field shows only its first four characters and stores the whole key', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openSettings(page);
  const field = page.getByTestId('apikey-input');
  await expectKeyFieldAttrs(field);
  const masked = (n: number) => KEY.slice(0, 4) + '*'.repeat(n);
  await pasteInto(field, KEY);
  await expect(field).toHaveValue(masked(16));
  await field.press('End');
  await field.press('Backspace');
  await expect(field).toHaveValue(masked(15));
  await field.press(KEY.slice(-1));
  await expect(field).toHaveValue(masked(16));
  await field.press('Control+A');
  await pasteInto(field, KEY);
  await expect(field).toHaveValue(masked(16));
  const html = await field.evaluate((el) => (el as HTMLInputElement).value + el.outerHTML);
  expect(html.includes(KEY.slice(4))).toBe(false);
  expect((await page.content()).includes(KEY.slice(4))).toBe(false);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.method() === 'PUT' && /\/api\/keys\/[a-z]+$/.test(r.url())),
    page.getByTestId('key-save').click(),
  ]);
  expect(req.postDataJSON().key === KEY).toBe(true);
  await expect(toastBody(page)).toHaveText(/^Key saved for .+\.$/);
  await expect(field).toHaveValue('');
  // typing then deleting a long value one key at a time
  await field.click();
  await page.keyboard.type('x'.repeat(60));
  await expect(field).toHaveValue('xxxx' + '*'.repeat(56));
  for (let i = 0; i < 60; i += 1) await page.keyboard.press('Backspace');
  await expect(field).toHaveValue('');
});

test('Lock key store confirms itself and shows the locked store', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openSettings(page);
  await page.getByTestId('settings-apikey').click();
  await expect(page.getByTestId('settings-apikey')).toHaveAttribute('aria-expanded', 'false');
  await page.getByTestId('settings-forget').click();
  await expect(toastBody(page)).toHaveText('Key store locked. Enter the store passphrase to unlock it.');
  await expect(page.getByTestId('settings-apikey')).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByTestId('vault-passphrase')).toBeVisible();
  await expect(page.getByTestId('vault-unlock')).toBeVisible();
  await expect(page.getByTestId('apikey-input')).toHaveCount(0);
  await expect(page.getByTestId('key-status')).toHaveCount(0);
  await expect(page.getByTestId('settings-forget')).toBeDisabled();
  await expect(page.getByTestId('composer-toast')).toBeHidden({ timeout: 10_000 });
  await page.getByTestId('vault-passphrase').fill('e2e-vault-pass');
  await clickUnlock(page);
  await expect(toastBody(page)).toHaveText('Key store unlocked.');
  await expect(page.getByTestId('settings-forget')).toBeEnabled();
  await page.getByTestId('key-remove').click();
  await expect(toastBody(page)).toHaveText(/^Key removed for .+\.$/);
  await page.getByTestId('apikey-input').fill(KEY);
  await page.getByTestId('key-save').click();
  await expect(toastBody(page)).toHaveText(/^Key saved for .+\.$/);
});

test('Start autosave and Save to file confirm themselves', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await openMenu(page);
  await page.getByTestId('save-passphrase').fill('e2e-save-pass');
  await page.getByTestId('save-unlock').click();
  await expect(toastBody(page)).toHaveText('Autosave is on for this browser.');
  await Promise.all([page.waitForEvent('download'), page.getByTestId('save-file').click()]);
  await expect(toastBody(page)).toHaveText('Save file handed to your browser as a download.');
});

test('the status icon shows ok, locked and no key, with the same text on a tap', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await closeMenu(page);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  await expect(icon(page)).toHaveAttribute('aria-label', STATUS_OK);
  await expect(icon(page)).toHaveAttribute('title', STATUS_OK);
  await icon(page).click();
  await expect(page.getByTestId('link-status-text')).toHaveText(STATUS_OK);
  await expect(page.getByTestId('link-status-check')).toHaveCount(0);

  await openSettings(page);
  await page.getByTestId('settings-forget').click();
  await expect(icon(page)).toHaveAttribute('data-level', 'attention');
  await expect(icon(page)).toHaveAttribute('aria-label', STATUS_LOCKED);
  await page.getByTestId('vault-passphrase').fill('e2e-vault-pass');
  await clickUnlock(page);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');

  await page.getByTestId('key-remove').click();
  await expect(icon(page)).toHaveAttribute('data-level', 'attention');
  await expect(icon(page)).toHaveAttribute('aria-label', STATUS_NO_KEY);
  await page.getByTestId('apikey-input').fill(KEY);
  await page.getByTestId('key-save').click();
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');

  const original = await page.locator('[data-testid^="provider-"][aria-pressed="true"]').getAttribute('data-testid');
  const other = page.locator('[data-testid^="provider-"][aria-pressed="false"]');
  expect(await other.count()).toBeGreaterThanOrEqual(1);
  await other.first().click();
  await expect(icon(page)).toHaveAttribute('data-level', 'attention');
  await expect(icon(page)).toHaveAttribute('aria-label', STATUS_NO_KEY);
  await page.getByTestId(original!).click();
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
});

test('with no config at load the icon shows the game server out of reach until the health check clears it', async ({ page }) => {
  await page.clock.install();
  await page.route('**/api/config', (r) => r.abort());
  await page.goto('/');
  await expect(icon(page)).toHaveAttribute('data-level', 'error');
  await expect(icon(page)).toHaveAttribute('aria-label', STATUS_DOWN);
  await page.unroute('**/api/config');
  await page.clock.runFor(6_000);
  await expect(icon(page)).not.toHaveAttribute('data-level', 'error');
});

test('the 30 s health check finds the game server gone and back, and never reads the key store', async ({ page }) => {
  await page.clock.install();
  await page.goto('/');
  await ensureSelfHostKey(page);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  let reads = 0;
  page.on('request', (r) => { if (r.url().endsWith('/api/keys/status')) reads += 1; });
  await page.clock.runFor(120_000);
  expect(reads).toBe(0);
  await page.route('**/api/health', (r) => r.abort());
  await page.clock.runFor(31_000);
  await expect(icon(page)).toHaveAttribute('data-level', 'error');
  await expect(page.getByTestId('composer-alert')).toHaveAttribute('data-kind', 'maint');
  await page.unroute('**/api/health');
  await page.clock.runFor(6_000);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  await expect(page.getByTestId('composer-alert')).toHaveCount(0);
  await expect.poll(() => reads).toBe(1);
  await page.waitForTimeout(1_000);
  expect(reads).toBe(1);
});

test('a hidden tab makes no health checks', async ({ page }) => {
  await page.addInitScript(() => {
    (window as any).__vis = 'visible';
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (window as any).__vis });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => (window as any).__vis === 'hidden' });
  });
  await page.clock.install();
  let checks = 0;
  page.on('request', (r) => { if (r.url().endsWith('/api/health')) checks += 1; });
  await page.goto('/');
  await expect(icon(page)).toBeVisible();
  const setVis = (v: string) => page.evaluate((x) => {
    (window as any).__vis = x;
    document.dispatchEvent(new Event('visibilitychange'));
  }, v);
  await setVis('hidden');
  await page.clock.runFor(120_000);
  expect(checks).toBe(0);
  await Promise.all([page.waitForResponse((r) => r.url().endsWith('/api/health')), setVis('visible')]);
  expect(checks).toBe(1);
  await page.waitForTimeout(500);
  await page.clock.runFor(30_000);
  await expect.poll(() => checks).toBe(2);
});

test('a session that ended reads as locked, after a refused save and after the game server returns', async ({ page }) => {
  await page.clock.install();
  await page.goto('/');
  await ensureSelfHostKey(page);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  await page.context().clearCookies();
  await openSettings(page);
  await page.getByTestId('apikey-input').fill(KEY);
  await page.getByTestId('key-save').click();
  await expect(icon(page)).toHaveAttribute('data-level', 'attention');
  await expect(icon(page)).toHaveAttribute('aria-label', STATUS_LOCKED);
  await page.getByTestId('vault-passphrase').fill('e2e-vault-pass');
  await clickUnlock(page);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  await closeSettings(page);
  await page.context().clearCookies();
  await page.route('**/api/health', (r) => r.abort());
  await page.clock.runFor(31_000);
  await expect(icon(page)).toHaveAttribute('data-level', 'error');
  await expect(icon(page)).toHaveAttribute('aria-label', STATUS_DOWN);
  await page.unroute('**/api/health');
  await page.clock.runFor(6_000);
  await expect(icon(page)).toHaveAttribute('aria-label', STATUS_LOCKED);
});

test('an unchecked key store offers Check again, which reads the store once in place', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await page.route('**/api/keys/status', (r) => r.fulfill({
    status: 500, contentType: 'application/json', body: '{"detail":{"error":"internal"}}',
  }), { times: 1 });
  await page.reload();
  await expect(icon(page)).toHaveAttribute('data-level', 'attention');
  await expect(icon(page)).toHaveAttribute('aria-label', 'Key store not checked.');
  await page.unroute('**/api/keys/status');
  let reads = 0;
  let loads = 0;
  page.on('request', (r) => { if (r.url().endsWith('/api/keys/status')) reads += 1; });
  page.on('load', () => { loads += 1; });
  await icon(page).click();
  await expect(page.getByTestId('link-status-text')).toHaveText('Key store not checked.');
  await expect(page.getByTestId('link-status-check')).toHaveText('Check again');
  await page.getByTestId('link-status-check').click();
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  await expect(page.getByTestId('link-status-check')).toHaveCount(0);
  await page.waitForTimeout(1_000);
  expect(reads).toBe(1);
  expect(loads).toBe(0);
});

test('leaving asks first only while the open game has unsaved changes', async ({ page, context }) => {
  const leave = async (p: Page) => {
    const types: string[] = [];
    p.on('dialog', (d) => { types.push(d.type()); d.dismiss().catch(() => {}); });
    await p.close({ runBeforeUnload: true });
    await expect.poll(() => types.length > 0 || p.isClosed(), { timeout: 10_000 }).toBe(true);
    return types;
  };
  // no game loaded
  const empty = await context.newPage();
  await empty.goto('/');
  await openMenu(empty);
  expect(await leave(empty)).toEqual([]);

  // a game whose save is confirmed
  const stored = await context.newPage();
  await stored.goto('/');
  await ensureSelfHostKey(stored);
  await newGame(stored);
  await setSavePass(stored);
  await expect(stored.getByTestId('saves-note')).toHaveText('Saved in this browser.');
  expect(await leave(stored)).toEqual([]);

  // an unsaved game
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  expect(await leave(page)).toEqual(['beforeunload']);
  await page.close();
});

test('An ability move asks for the stronger model once, then how it happens, then plays', async ({ page }) => {
  const bodies: any[] = [];
  const kinds: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().endsWith('/api/game/turn')) bodies.push(r.postDataJSON());
  });
  page.on('response', async (r) => {
    if (r.request().method() === 'POST' && r.url().endsWith('/api/game/turn')) {
      const body = await r.json();
      kinds.push(body.kind || (typeof body.narrative === 'string' && body.narrative ? 'narrative' : 'unknown'));
    }
  });
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await send(page, tagged('I shape a lantern with zzability'));
  await expect(page.getByText('The usual model could not settle the physics of this move', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await expect(page.getByText('How does this happen?')).toBeVisible();
  await page.getByRole('button', { name: 'Within you (your own energy)', exact: true }).click();
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });
  await expect.poll(() => kinds.length).toBe(3);
  expect(kinds).toEqual(['escalation_consent_needed', 'ability_intent_clarification_needed', 'narrative']);
  expect(bodies[2].consent_token).toBeTruthy();
  expect(bodies[2].consent_token).toBe(bodies[1].consent_token);
  expect(bodies[2].ability_intent).toBe('internal');
});

test('Keep it small asks how it happens, then plays on the usual model', async ({ page }) => {
  const bodies: any[] = [];
  const kinds: string[] = [];
  page.on('request', (r) => {
    if (r.method() === 'POST' && r.url().endsWith('/api/game/turn')) bodies.push(r.postDataJSON());
  });
  page.on('response', async (r) => {
    if (r.request().method() === 'POST' && r.url().endsWith('/api/game/turn')) {
      const body = await r.json();
      kinds.push(body.kind || (typeof body.narrative === 'string' && body.narrative ? 'narrative' : 'unknown'));
    }
  });
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await send(page, tagged('I shape a lantern with zzability'));
  await expect(page.getByText('The usual model could not settle the physics of this move', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: 'Keep it small', exact: true }).click();
  await expect(page.getByText('How does this happen?')).toBeVisible();
  await page.getByRole('button', { name: 'From your surroundings', exact: true }).click();
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });
  await expect.poll(() => kinds.length).toBe(3);
  expect(kinds).toEqual(['escalation_consent_needed', 'ability_intent_clarification_needed', 'narrative']);
  expect(bodies[2].decline_escalation).toBe(true);
  expect(bodies[2].ability_intent).toBe('external');
});

test('A move over the context ceiling is refused with its message and the game stays as it was', async ({ page }) => {
  const heavy = JSON.parse(readFileSync(DEMO_SAVE, 'utf8'));
  heavy.save.state.tutorial_stage = 2;
  heavy.save.state.zz_archive = Array.from({ length: 12_000 }, (_, i) => `lamp${i}`).join(' ');
  const opening: string = heavy.save.history[0].content_en || heavy.save.history[0].content;
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openMenu(page);
  await page.getByTestId('load-file').setInputFiles({
    name: 'heavy.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(heavy)),
  });
  await expect(story(page)).toContainText(opening.slice(0, 40));
  await closeMenu(page);
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/turn')),
    send(page, tagged('too large')),
  ]);
  expect(resp.status()).toBe(413);
  expect((await resp.json()).detail.error).toBe('context_too_large');
  await expect(page.getByTestId('composer-alert')).toContainText(CONTEXT_TOO_LARGE);
  await expect(page.getByTestId('composer-alert')).toContainText('Move not played');
  await expect(dmBlocks(page)).toHaveCount(1);
  await openMenu(page);
  await page.getByTestId('save-plain').check();
  const [file] = await Promise.all([
    page.waitForEvent('download'), page.getByTestId('save-file').click(),
  ]);
  const saved = JSON.parse(readFileSync((await file.path()) as string, 'utf8'));
  expect(saved.save.history).toEqual(heavy.save.history);
  expect(saved.save.state.zz_archive).toBe(heavy.save.state.zz_archive);
});

test('A reply cut short is never shown: the move is refused, the story stays, and the next move plays', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await expect(dmBlocks(page)).toHaveCount(1);
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/turn')),
    send(page, tagged('zzcutreply')),
  ]);
  expect(resp.status()).toBe(503);
  expect((await resp.json()).detail.error).toBe('provider_unavailable');
  await expect(page.getByTestId('composer-alert')).toContainText(PROVIDER_UNAVAILABLE);
  await expect(page.getByTestId('composer-alert')).toHaveAttribute('data-kind', '500');
  await expect(page.getByTestId('composer-alert')).toContainText('500 - The realm shuddered');
  await expect(dmBlocks(page)).toHaveCount(1);
  await expect(story(page)).not.toContainText('frosted-glass');
  await send(page, tagged('after the cut'));
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });
  await expect(story(page)).toContainText(tagged('after the cut'));
});

test('a reply that misses its closing hook gets one hook line after it', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await send(page, 'I wait at the bar zzhookmiss');
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });
  const text = await story(page).innerText();
  expect(text.indexOf('Not yet.')).toBeGreaterThan(-1);
  expect(text.indexOf('So who sent you?')).toBeGreaterThan(text.indexOf('Not yet.'));
  expect(text.split('So who sent you?').length).toBe(2);
});
