import { test, expect, Page, Locator } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { KEY, TEXT, story, dmBlocks, openMenu, closeMenu, openSettings, closeSettings, newGame, send, idbRecords } from './helpers';

// Hosted mode allows one move per 15 seconds per session and per IP.
const INTERVAL_MS = 16_000;

test.describe.configure({ mode: 'serial' });
test.setTimeout(240_000);

async function enterKey(page: Page) {
  await openSettings(page);
  await page.getByTestId('apikey-ack').check();
  await page.getByTestId('apikey-input').fill(KEY);
  await expect(page.getByTestId('apikey-input')).toHaveValue(KEY.slice(0, 4) + '*'.repeat(KEY.length - 4));
  await closeSettings(page);
}

const toastBody = (page: Page) => page.getByTestId('composer-toast').locator('span').nth(1);
const icon = (page: Page) => page.getByTestId('link-status');

async function expectKeyFieldAttrs(field: Locator) {
  for (const [k, v] of [['type', 'text'], ['autocomplete', 'off'], ['spellcheck', 'false'], ['autocapitalize', 'off'],
    ['autocorrect', 'off'], ['data-form-type', 'other'], ['data-1p-ignore', ''], ['data-lpignore', 'true'], ['data-bwignore', '']]) {
    await expect(field).toHaveAttribute(k, v);
  }
  expect(await field.getAttribute('id')).not.toMatch(/pass/i);
  expect(await field.getAttribute('name')).not.toMatch(/pass/i);
  expect(await field.evaluate((el) => el.closest('form'))).toBeNull();
}

async function setPassphrase(page: Page, pass = 'e2e-save-pass') {
  await openMenu(page);
  await page.getByTestId('save-passphrase').fill(pass);
  await page.getByTestId('save-unlock').click();
}

test('the disclosure comes before the key field, and the key is never stored', async ({ page }) => {
  const logs: string[] = [];
  page.on('console', (m) => logs.push(m.text()));
  await page.goto('/');
  await openSettings(page);
  const disclosure = page.getByTestId('hosted-disclosure');
  const field = page.getByTestId('apikey-input');
  await expect(disclosure).toBeVisible();
  await expect(disclosure).not.toBeEmpty();
  const [d, f] = [await disclosure.boundingBox(), await field.boundingBox()];
  expect(d!.y).toBeLessThan(f!.y);
  // password managers are told to leave the key field alone
  await expectKeyFieldAttrs(field);
  await closeSettings(page);

  await enterKey(page);
  // a new game with no passphrase opens the menu and asks for one; the whole key rides on it
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/game/new')),
    newGame(page, 'Mira', true),
  ]);
  expect(req.headers()['x-provider-key'] === KEY).toBe(true);
  await expect(page.getByTestId('saves-error')).toHaveText('Set a save passphrase first, or tick plain JSON.');
  await expect(page.getByTestId('save-passphrase')).toBeVisible();
  await closeMenu(page);
  await page.waitForTimeout(INTERVAL_MS);
  await send(page, TEXT);
  await expect(story(page)).toContainText(TEXT);
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });

  // a second move inside the interval is refused inline and the draft stays
  await send(page, `${TEXT} again`);
  await expect(page.getByTestId('composer-alert')).toContainText('one move every');
  await expect(page.getByPlaceholder('What do you do?')).toHaveValue(`${TEXT} again`);

  // the browser save is an envelope with no canary text, and the key is nowhere
  await setPassphrase(page);
  await expect.poll(async () => Object.values(await idbRecords(page)).some((v) => !!JSON.parse(v).ct)).toBe(true);
  const records = await idbRecords(page);
  const dump = await page.evaluate(async () => JSON.stringify({
    local: { ...localStorage }, session: { ...sessionStorage }, idb: (await indexedDB.databases()).map((d) => d.name),
  })) + JSON.stringify(records);
  expect(dump).not.toContain(KEY);
  expect(dump).not.toContain(TEXT);
  expect(await page.evaluate(async () => (await indexedDB.databases()).map((d) => d.name))).toEqual(['coherence']);
  expect(await page.evaluate(() => new Promise((resolve) => {
    const req = indexedDB.open('coherence');
    req.onsuccess = () => { const names = [...req.result.objectStoreNames]; req.result.close(); resolve(names); };
  }))).toEqual(['saves']);
  for (const [id, text] of Object.entries(records)) {
    expect(id).toMatch(/^(?:[0-9a-f]{16}|legacy|legacy-local)$/);
    expect(Object.keys(JSON.parse(text)).sort()).toEqual(['cipher', 'ct', 'format', 'kdf', 'v']);
  }
  const envelope = JSON.parse(Object.values(records)[0]);
  expect(envelope.format).toBe('coherence-save');
  expect(envelope.ct).toBeTruthy();

  // no cookie, and the key is in no download and no console message
  const [file] = await Promise.all([page.waitForEvent('download'), page.getByTestId('save-file').click()]);
  const saved = readFileSync((await file.path()) as string, 'utf8');
  expect(JSON.parse(saved).ct).toBeTruthy();
  expect(saved).not.toContain(KEY);
  expect(saved).not.toContain(TEXT);
  expect(await page.evaluate(() => document.cookie)).toBe('');
  expect(logs.filter((l) => l.includes(KEY))).toEqual([]);

  // Forget key confirms itself while the rate-limit alert is still up: the confirmation sits
  // directly above the alert, then fades on its own and the alert stays
  const alert = page.getByTestId('composer-alert');
  await expect(alert).toContainText('one move every');
  await openSettings(page);
  await page.getByTestId('settings-forget').click();
  await expect(toastBody(page)).toHaveText('Key forgotten. This tab no longer holds a key.');
  await expect(page.getByTestId('apikey-input')).toHaveValue('');
  await expect(alert).toBeVisible();
  const [tb, ab] = [await page.getByTestId('composer-toast').boundingBox(), await alert.boundingBox()];
  expect(tb!.y + tb!.height).toBeLessThanOrEqual(ab!.y + 2);
  expect(ab!.y + ab!.height).toBeLessThanOrEqual((await page.getByPlaceholder('What do you do?').boundingBox())!.y);
  await expect(page.getByTestId('composer-toast')).toBeHidden({ timeout: 6_000 });
  await expect(alert).toBeVisible();

  // reloading forgets the key
  await page.reload();
  await openSettings(page);
  await expect(page.getByTestId('apikey-input')).toHaveValue('');
});

test('the status icon follows the key in this tab and the game server', async ({ page }) => {
  await page.goto('/');
  await expect(icon(page)).toHaveAttribute('data-level', 'attention');
  await expect(icon(page)).toHaveAttribute('aria-label', 'No key in this tab: enter one in Settings.');
  await enterKey(page);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  await expect(icon(page)).toHaveAttribute('aria-label', 'Game server reachable, and this tab holds a key.');
  await openSettings(page);
  await page.getByTestId('settings-forget').click();
  await expect(toastBody(page)).toHaveText('Key forgotten. This tab no longer holds a key.');
  await expect(page.getByTestId('apikey-input')).toHaveValue('');
  await expect(icon(page)).toHaveAttribute('data-level', 'attention');
});

test('with no config at load the icon shows the game server out of reach until it answers', async ({ page }) => {
  await page.route('**/api/config', (r) => r.abort());
  await page.goto('/');
  await expect(icon(page)).toHaveAttribute('data-level', 'error');
  await expect(icon(page)).toHaveAttribute('aria-label', 'Game server not reachable: the game checks again on its own.');
  await page.unroute('**/api/config');
  await expect(icon(page)).toHaveAttribute('data-level', 'attention', { timeout: 20_000 });
});

test('the 30 s health check finds the game server gone, then back', async ({ page }) => {
  await page.clock.install();
  await page.goto('/');
  await enterKey(page);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  await page.route('**/api/health', (r) => r.abort());
  await page.clock.runFor(31_000);
  await expect(icon(page)).toHaveAttribute('data-level', 'error');
  await expect(page.getByTestId('composer-alert')).toHaveAttribute('data-kind', 'maint');
  await page.unroute('**/api/health');
  await page.clock.runFor(6_000);
  await expect(icon(page)).toHaveAttribute('data-level', 'ok');
  await expect(page.getByTestId('composer-alert')).toHaveCount(0);
});

test('a full browser store offers the save file and play continues', async ({ page }) => {
  await page.addInitScript(() => {
    const original = IDBObjectStore.prototype.put;
    let writes = 0;
    IDBObjectStore.prototype.put = function (...a: any[]) {
      if (this.name === 'saves') {
        writes += 1;
        if (writes > 1) throw new DOMException('full', 'QuotaExceededError');
      }
      return original.apply(this, a as any);
    };
  });
  await page.goto('/');
  await enterKey(page);
  await page.waitForTimeout(INTERVAL_MS);
  await newGame(page);
  await setPassphrase(page);
  await page.waitForTimeout(INTERVAL_MS);
  await send(page, `${TEXT} one`);
  await expect(page.getByTestId('storage-full-save')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('composer-alert')).toContainText('Save to file');
  await expect(page.getByTestId('composer-alert')).toContainText('Not saved');

  await page.waitForTimeout(INTERVAL_MS);
  await send(page, `${TEXT} two`);
  await expect(story(page)).toContainText(`${TEXT} two`);
  await expect(dmBlocks(page)).toHaveCount(3, { timeout: 60_000 });
});

test('an old single browser save moves into the store, and a passphrase stores a new game beside it', async ({ page }) => {
  const OLD = '{"format":"coherence-save","v":1}';
  await page.goto('/');
  await page.evaluate((t) => localStorage.setItem('coherence.save', t), OLD);
  await page.reload();
  await expect.poll(async () => (await idbRecords(page)).legacy).toBe(OLD);
  expect(await page.evaluate(() => localStorage.getItem('coherence.save'))).toBeNull();
  await enterKey(page);
  await page.waitForTimeout(INTERVAL_MS);
  await newGame(page);
  await openMenu(page);
  await expect(page.getByTestId('saves-note')).toHaveText('This browser has saved games. Enter the save passphrase to open them.');
  await setPassphrase(page);
  await expect(page.getByTestId('save-replace-confirm')).toHaveCount(0);
  await expect.poll(async () => Object.entries(await idbRecords(page)).filter(([k, v]) => /^[0-9a-f]{16}$/.test(k) && !!JSON.parse(v).ct).length).toBe(1);
  expect((await idbRecords(page)).legacy).toBe(OLD);
  await expect(page.getByTestId('saves-note')).toHaveText('Saved in this browser.');
});

test('Begin story needs a tab key and Open Settings reveals its field', async ({ page }) => {
  await page.goto('/');
  const packs = (await (await page.request.get('/api/packs')).json()).packs;
  await openMenu(page);
  await page.getByRole('button', { name: 'New save', exact: true }).click();
  await page.getByRole('button', { name: packs[0].name }).and(page.locator(':not([aria-label^="Enter "])')).first().click();
  await page.getByPlaceholder('a name for your character').fill('Mira');
  const begin = page.getByRole('button', { name: 'Begin story', exact: true });
  await expect(begin).toBeDisabled();
  await expect(page.getByTestId('begin-reason')).toHaveText('No key in this tab: enter one in Settings.');
  await page.getByTestId('begin-settings').click();
  await expect(page.getByTestId('apikey-input')).toBeVisible();
  await page.getByTestId('apikey-ack').check();
  await page.getByTestId('apikey-input').fill(KEY);
  await closeSettings(page);
  await page.waitForTimeout(INTERVAL_MS);
  await expect(begin).toBeEnabled();
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/new')),
    begin.click(),
  ]);
  expect(res.status()).toBe(200);
  await expect(dmBlocks(page).first()).toBeVisible();
});

test('a key entered for one provider is never sent with another', async ({ page }) => {
  const OTHER = 'test-other-key-2b6d';
  await page.goto('/');
  await enterKey(page);
  const sent: { p: string; k: string }[] = [];
  let switched = false;
  page.on('request', (r) => { if (switched) sent.push({ p: r.headers()['x-provider'] || '', k: r.headers()['x-provider-key'] || '' }); });
  await openSettings(page);
  await expect(page.getByTestId('provider-anthropic')).toHaveAttribute('aria-pressed', 'true');
  switched = true;
  await page.getByTestId('provider-openai').click();
  const field = page.getByTestId('apikey-input');
  await expect(field).toHaveValue('');
  await expect(page.getByText('Enter a key to start playing.', { exact: true })).toBeVisible();
  await expect(icon(page)).toHaveAttribute('aria-label', 'No key for OpenAI: add one in Settings.');

  // a request right after the switch carries the new provider and no key
  const [h] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/health')),
    page.evaluate(() => document.dispatchEvent(new Event('visibilitychange'))),
  ]);
  expect(h.headers()['x-provider']).toBe('openai');
  expect(h.headers()['x-provider-key']).toBeUndefined();

  // a move under the second provider carries only the key entered for it
  await field.fill(OTHER);
  await closeSettings(page);
  await page.waitForTimeout(INTERVAL_MS);
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/game/new')),
    newGame(page),
  ]);
  expect(req.headers()['x-provider']).toBe('openai');
  expect(req.headers()['x-provider-key'] === OTHER).toBe(true);
  expect(sent.filter((s) => s.k === KEY)).toEqual([]);
  expect(sent.some((s) => s.p === 'openai' && s.k === '')).toBe(true);

  // switching back shows the first key again, masked
  await openSettings(page);
  await page.getByTestId('provider-anthropic').click();
  await expect(field).toHaveValue(KEY.slice(0, 4) + '*'.repeat(KEY.length - 4));
});

test('Forget key under the key field forgets every key this tab holds', async ({ page }) => {
  const OTHER = 'test-other-key-2b6d';
  await page.goto('/');
  await enterKey(page);
  await openSettings(page);
  await page.getByTestId('provider-openai').click();
  const field = page.getByTestId('apikey-input');
  await field.fill(OTHER);
  await expect(field).toHaveValue(OTHER.slice(0, 4) + '*'.repeat(OTHER.length - 4));

  const sent: { p: string; k: string }[] = [];
  page.on('request', (r) => sent.push({ p: r.headers()['x-provider'] || '', k: r.headers()['x-provider-key'] || '' }));
  await page.getByTestId('apikey-forget').click();
  await expect(toastBody(page)).toHaveText('Key forgotten. This tab no longer holds a key.');
  await expect(field).toHaveValue('');
  const [h1] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/health')),
    page.evaluate(() => document.dispatchEvent(new Event('visibilitychange'))),
  ]);
  expect(h1.headers()['x-provider']).toBe('openai');

  // the first provider's key is gone too
  await page.getByTestId('provider-anthropic').click();
  await expect(field).toHaveValue('');
  await expect(icon(page)).toHaveAttribute('aria-label', 'No key in this tab: enter one in Settings.');
  const [h2] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/health')),
    page.evaluate(() => document.dispatchEvent(new Event('visibilitychange'))),
  ]);
  expect(h2.headers()['x-provider']).toBe('anthropic');

  expect(sent.some((s) => s.p === 'openai')).toBe(true);
  expect(sent.some((s) => s.p === 'anthropic')).toBe(true);
  expect(sent.filter((s) => s.k !== '')).toEqual([]);
});

const DEMO_SAVE = '/examples/demo-save.json';
const HISTORY_CAP = 7;
const TOO_LARGE = 'This save holds more text than this server checks, so it cannot be opened here. Keep it as a file.';
const withEntries = (n: number) => {
  const demo = JSON.parse(readFileSync(DEMO_SAVE, 'utf8'));
  const fill = Array.from({ length: n - 1 }, (_, k) => k + 1).map((i) => (i % 2
    ? { role: 'user', content_en: `I look around the office, pass ${i}.` }
    : { role: 'assistant', content_en: `The lamp buzzes over the desk, pass ${i}.` }));
  demo.save.history = [demo.save.history[0], ...fill];
  demo.save.turn = Math.floor((n - 1) / 2);
  return { name: 'save.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(demo)) };
};
const sealed = async (page: Page) => Object.values(await idbRecords(page)).filter((v) => !!JSON.parse(v).ct).length;
const slotCt = async (page: Page) => {
  const slots = Object.entries(await idbRecords(page)).filter(([k, v]) => /^[0-9a-f]{16}$/.test(k) && !!JSON.parse(v).ct);
  expect(slots).toHaveLength(1);
  return JSON.parse(slots[0][1]).ct;
};

// Kept last: the refused move still uses the 15 s message slot, and check_hosted.sh sleeps 16 s after this spec.
test('a save past the history cap is refused at import, and the move that would pass it is not played', async ({ page }) => {
  await page.goto('/');
  await enterKey(page);
  await setPassphrase(page);
  await closeMenu(page);
  await openMenu(page);
  const stored = await sealed(page);

  const [over] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/check')),
    page.getByTestId('load-file').setInputFiles(withEntries(HISTORY_CAP + 1)),
  ]);
  expect(over.status()).toBe(413);
  expect((await over.json()).detail).toMatchObject({ error: 'text_too_large', scope: 'history' });
  await expect(page.getByTestId('saves-error')).toHaveText(TOO_LARGE);
  await expect(page.getByTestId('saves-error')).not.toContainText('500,000');
  await expect(dmBlocks(page)).toHaveCount(0);
  expect(await sealed(page)).toBe(stored);

  const [at] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/check')),
    page.getByTestId('load-file').setInputFiles(withEntries(HISTORY_CAP)),
  ]);
  expect(at.status()).toBe(200);
  await expect(dmBlocks(page)).toHaveCount(4);
  await expect.poll(() => sealed(page)).toBe(stored + 1);
  const ctBefore = await slotCt(page);
  await closeMenu(page);
  const before = await story(page).innerText();

  await page.waitForTimeout(INTERVAL_MS);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/turn')),
    send(page, `${TEXT} past the cap`),
  ]);
  expect(res.status()).toBe(413);
  expect((await res.json()).detail).toMatchObject({ error: 'text_too_large', scope: 'history' });
  const alert = page.getByTestId('composer-alert');
  await expect(alert).toHaveAttribute('data-kind', '500');
  await expect(alert).toContainText(TOO_LARGE);
  await expect(alert).not.toContainText('500,000');
  expect(await slotCt(page)).toBe(ctBefore);
  expect(await story(page).innerText()).toBe(before);
  await expect(dmBlocks(page)).toHaveCount(4);
  await expect(story(page)).not.toContainText(`${TEXT} past the cap`);
  await expect(page.getByPlaceholder('What do you do?')).toHaveValue(`${TEXT} past the cap`);
  expect(await sealed(page)).toBe(stored + 1);
});
