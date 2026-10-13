import { test, expect, Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import {
  dmBlocks, openMenu, openSettings, closeSettings, ensureSelfHostKey, newGame, send, story, setSavePass, slotCount, slotRows,
} from './helpers';

// an entry of the sample world's main word list, not of its always-on list
const LISTED = 'I study the nude statue';

test.describe.configure({ mode: 'serial' });
test.setTimeout(240_000);

// sends a move and returns the request body and the response
async function sendWatched(page: Page, text: string) {
  const [req, res] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/game/turn') && r.method() === 'POST'),
    page.waitForResponse((r) => r.url().endsWith('/api/game/turn')),
    send(page, text),
  ]);
  return { body: JSON.parse(req.postData() || '{}'), res };
}

test('Settings lists API key, Play mode and Submit key, then the forget row', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await openSettings(page);
  const panel = page.locator('[data-screen-label="Settings panel"]');
  const ids = await panel.locator('[data-testid^="settings-"]').evaluateAll(
    (els) => els.map((el) => el.getAttribute('data-testid')));
  expect(ids).toEqual(['settings-apikey', 'settings-playmode', 'settings-submitkey', 'settings-forget']);
  const heads = await panel.locator('button[aria-expanded]').allTextContents();
  expect(heads.map((s) => s.trim())).toEqual(['API key', 'Play mode', 'Submit key']);
  await closeSettings(page);
});

test('a move with a word from the pack list is refused before any reply', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  const { res } = await sendWatched(page, LISTED);
  expect(res.status()).toBe(451);
  expect((await res.json()).detail.category).toBe('pack');
  await expect(page.getByTestId('composer-alert')).toHaveAttribute('data-kind', '451');
  await expect(dmBlocks(page)).toHaveCount(1);
});

test('a new game whose note carries a listed word is refused', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  const packs = (await (await page.request.get('/api/packs')).json()).packs;
  await openMenu(page);
  await page.getByRole('button', { name: 'New save' }).click();
  await page.getByRole('button', { name: packs[0].name }).and(page.locator(':not([aria-label^="Enter "])')).first().click();
  await page.getByPlaceholder('a name for your character').fill('Mira');
  await page.getByRole('button', { name: /Shape your character/ }).click();
  await page.getByPlaceholder('a scar, a tic, a bond - anything at all').fill(LISTED);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/new')),
    page.getByRole('button', { name: 'Begin story' }).click(),
  ]);
  expect(res.status()).toBe(451);
  expect((await res.json()).detail.category).toBe('pack');
  await expect(page.getByTestId('new-error')).toContainText('Story not started');
  await expect(page.getByTestId('new-error')).toContainText('Something you wrote for the new story was blocked by the content filter.');
  await expect(page.getByTestId('composer-alert')).toHaveCount(0);
});

// a doxx-shaped canary: the engine's hard rule blocks it wherever it appears
const CANARY = 'Ledger 123-45-6789';
const DIGITS = '123-45-6789';
const DEMO_SAVE = process.env.DEMO_SAVE || '/examples/demo-save.json';
const demoWith = (edit: (save: any) => void) => {
  const demo = JSON.parse(readFileSync(DEMO_SAVE, 'utf8'));
  edit(demo.save);
  return { name: 'save.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(demo)) };
};
const turnsSent = (page: Page) => {
  const seen: string[] = [];
  page.on('request', (r) => { if (r.url().endsWith('/api/game/turn')) seen.push(r.url()); });
  return seen;
};

test('a model reply the filter blocks is never shown', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  const { res } = await sendWatched(page, 'I ask the clerk about zzreplycanary');
  expect(res.status()).toBe(451);
  const detail = (await res.json()).detail;
  expect(detail.error).toBe('output_blocked');
  expect(detail.category).toBe('doxx');
  const alert = page.getByTestId('composer-alert');
  await expect(alert).toHaveAttribute('data-kind', '451');
  await expect(alert).toContainText('The reply to this move was blocked by the content filter, so it was not shown or saved.');
  await expect(dmBlocks(page)).toHaveCount(1);
  await expect(page.locator('body')).not.toContainText(DIGITS);
});

test('save files with blocked text are refused and the open game stays', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  const before = await story(page).innerText();
  const turns = turnsSent(page);
  const files = [
    demoWith((s) => { s.history[0].content_en = s.history[0].content_en + ' ' + CANARY; }),
    demoWith((s) => { s.config.world_premise = CANARY; }),
    demoWith((s) => { s.history[0].content_en = 'Ledger 123-45-' + String.fromCharCode(92) + 'u0036789'; }),
  ];
  for (const file of files) {
    await openMenu(page);
    const [res] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/game/check')),
      page.getByTestId('load-file').setInputFiles(file),
    ]);
    expect(res.status()).toBe(451);
    expect((await res.json()).detail.error).toBe('save_blocked');
    await expect(page.getByTestId('saves-error')).toHaveText('This save holds text the content filter blocks, so it cannot be opened or played.');
    expect(await story(page).innerText()).toBe(before);
    await expect(page.locator('body')).not.toContainText(DIGITS);
  }
  expect(turns).toEqual([]);
});

test('a save file with a number for its text is refused as not a save', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openMenu(page);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/check')),
    page.getByTestId('load-file').setInputFiles(demoWith((s) => { s.history[0].content_en = 12345; })),
  ]);
  expect(res.status()).toBe(400);
  expect((await res.json()).detail.error).toBe('bad_save');
  await expect(page.getByTestId('saves-error')).toHaveText('This file is not a valid Coherence save.');
  await expect(dmBlocks(page)).toHaveCount(0);
});

test('a stored game shows nothing of its own until the engine has checked it', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await setSavePass(page);
  await newGame(page, 'A');
  let first = true;
  await page.route('**/api/game/turn', async (route) => {
    if (!first) return route.continue();
    first = false;
    const res = await route.fetch();
    const body = await res.json();
    body.save.name = CANARY;
    const last = body.save.history[body.save.history.length - 1];
    last.content_en = CANARY;
    if ('content' in last) last.content = CANARY;
    await route.fulfill({ response: res, json: body });
  });
  await sendWatched(page, 'I wait');
  await page.unroute('**/api/game/turn');
  await newGame(page, 'B');
  await expect.poll(() => slotCount(page)).toBe(2);
  await page.reload();
  await setSavePass(page);
  await expect(dmBlocks(page).first()).toBeVisible({ timeout: 30_000 });
  const row = slotRows(page);
  await expect(row).toHaveCount(1);
  await expect(row.first()).toContainText('Saved game');
  await expect(row.first().locator('..')).not.toContainText('Summary');
  await expect(page.locator('body')).not.toContainText(DIGITS);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/check')),
    row.first().click(),
  ]);
  expect(res.status()).toBe(451);
  await expect(row.first()).toContainText('Blocked save');
  await expect(page.getByTestId('save-slot-remove')).toHaveCount(1);
  await expect(page.locator('body')).not.toContainText(DIGITS);
});

test('a summary is shown only once its cut text has been checked', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openMenu(page);
  const row = page.getByTestId('save-row').locator('..');
  const withOpening = (text: string) => demoWith((s) => {
    const first = s.history.find((h: any) => h && h.role === 'assistant');
    first.content_en = text;
    if ('content' in first) first.content = text;
  });
  // a clean opening longer than the cut: its summary shows once checked
  const clean = 'Rain '.repeat(60) + 'and the lamps burn low.';
  await page.getByTestId('load-file').setInputFiles(withOpening(clean));
  await expect(row).toContainText('Summary');
  await expect(row).toContainText('Rain Rain');
  // the digits end exactly at the cut: the whole text holds a longer number,
  // the summary as shown would hold the blocked one
  const cut = 'Rain '.repeat(44) + 'wet a ' + DIGITS + '0 and the lamps burn low over the wet stones. '.repeat(3);
  const blocked = page.waitForResponse((r) => r.url().endsWith('/api/game/check') && r.status() === 451);
  await page.getByTestId('load-file').setInputFiles(withOpening(cut));
  const res = await blocked;
  expect((await res.json()).detail.error).toBe('save_blocked');
  await expect(row).not.toContainText('Summary');
  await expect(row).not.toContainText(DIGITS);
});
