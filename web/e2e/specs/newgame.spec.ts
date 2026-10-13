import { test, expect, Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import {
  KEY, TEXT, story, dmBlocks, openMenu, closeMenu, openSettings, closeSettings,
  ensureSelfHostKey, clickUnlock, newGame, send,
} from './helpers';

test.describe.configure({ mode: 'serial' });
test.setTimeout(300_000);

const begin = (page: Page) => page.getByRole('button', { name: 'Begin story', exact: true });
const icon = (page: Page) => page.getByTestId('link-status');

async function prepareNew(page: Page, character = 'Mira') {
  const packs = (await (await page.request.get('/api/packs')).json()).packs;
  await openMenu(page);
  await page.getByRole('button', { name: 'New save', exact: true }).click();
  await page.getByRole('button', { name: packs[0].name }).and(page.locator(':not([aria-label^="Enter "])')).first().click();
  await page.getByPlaceholder('a name for your character').fill(character);
}

async function unlock(page: Page) {
  await page.getByTestId('vault-passphrase').fill('e2e-vault-pass');
  await clickUnlock(page);
  await expect(page.getByTestId('apikey-input')).toBeVisible();
}

test('a locked store gates Begin story, and unlocking keeps the draft and opening', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openSettings(page);
  await page.getByTestId('settings-forget').click();
  await expect(page.getByTestId('vault-passphrase')).toBeVisible();
  await closeSettings(page);
  await prepareNew(page);
  await expect(begin(page)).toBeDisabled();
  const reason = 'Key store locked: unlock it in Settings.';
  await expect(page.getByTestId('begin-reason')).toHaveText(reason);
  await expect(icon(page)).toHaveAttribute('aria-label', reason);
  await page.getByTestId('begin-settings').click();
  await expect(page.getByTestId('vault-passphrase')).toBeVisible();
  await unlock(page);
  await closeSettings(page);
  await expect(page.getByPlaceholder('a name for your character')).toHaveValue('Mira');
  await expect(begin(page)).toBeEnabled();
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/new')),
    begin(page).click(),
  ]);
  expect(res.status()).toBe(200);
  const first = (await res.json()).save.history[0];
  const opening = readFileSync('/pack/opening.md', 'utf8').trim();
  expect(first.content_en || first.content).toBe(opening);
  await expect(story(page)).toContainText(opening);
  await expect(dmBlocks(page)).toHaveCount(1);
});

test('an empty unlocked store gates Begin story with the active provider', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openSettings(page);
  for (const provider of ['anthropic', 'openai', 'gemini', 'xai']) {
    await page.getByTestId('provider-' + provider).click();
    await page.getByTestId('key-remove').click();
    await expect(page.getByTestId('key-status')).toContainText('No key');
  }
  await page.getByTestId('provider-anthropic').click();
  await closeSettings(page);
  await prepareNew(page);
  await expect(begin(page)).toBeDisabled();
  await expect(page.getByTestId('begin-reason')).toHaveText('No key for Claude (tuned): add one in Settings.');
  await expect(page.getByTestId('begin-reason')).not.toContainText('stored keys');
  await closeMenu(page);
  await ensureSelfHostKey(page);
});

test('a key for another provider gates Begin story until that provider is chosen', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openSettings(page);
  await page.getByTestId('provider-xai').click();
  if ((await page.getByTestId('key-status').innerText()).includes('Key set')) {
    await page.getByTestId('key-remove').click();
    await expect(page.getByTestId('key-status')).toContainText('No key');
  }
  await closeSettings(page);
  await prepareNew(page);
  await expect(begin(page)).toBeDisabled();
  await expect(page.getByTestId('begin-reason')).toContainText('No key for xAI Grok');
  await expect(page.getByTestId('begin-reason')).toContainText('stored keys:');
  await page.getByTestId('begin-settings').click();
  await page.getByTestId('provider-anthropic').click();
  await closeSettings(page);
  await expect(begin(page)).toBeEnabled();
});

test('a move with no game loaded says to start or load a story', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await closeMenu(page);
  let turns = 0;
  page.on('request', (r) => { if (r.url().endsWith('/api/game/turn')) turns += 1; });
  await send(page, TEXT);
  await expect(page.getByTestId('composer-alert')).toContainText('Start or load a story first.');
  expect(turns).toBe(0);
  await newGame(page);
  await expect(page.getByTestId('composer-alert')).toHaveCount(0);
});

test('a locked move offers Settings and unlocking clears its error', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await openSettings(page);
  await page.getByTestId('vault-lock').click();
  await expect(page.getByTestId('vault-passphrase')).toBeVisible();
  await closeSettings(page);
  await closeMenu(page);
  const [refused] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/turn')),
    send(page, TEXT),
  ]);
  expect(refused.status()).toBe(428);
  await expect(page.getByTestId('composer-alert')).toContainText('The key store is locked.');
  await page.getByTestId('alert-settings').click();
  await expect(page.getByTestId('vault-passphrase')).toBeVisible();
  await unlock(page);
  await expect(page.getByTestId('composer-alert')).toHaveCount(0);
  await closeSettings(page);
  await closeMenu(page);
  const [played] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/turn')),
    send(page, TEXT),
  ]);
  expect(played.status()).toBe(200);
  await expect(dmBlocks(page)).toHaveCount(2);
});

test('a move names stored providers and changing provider clears its error', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  await openSettings(page);
  await page.getByTestId('provider-xai').click();
  if ((await page.getByTestId('key-status').innerText()).includes('Key set')) {
    await page.getByTestId('key-remove').click();
    await expect(page.getByTestId('key-status')).toContainText('No key');
  }
  await closeSettings(page);
  await closeMenu(page);
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/turn')),
    send(page, TEXT),
  ]);
  expect(res.status()).toBe(428);
  await expect(page.getByTestId('composer-alert')).toContainText('No API key is set for xAI Grok.');
  await expect(page.getByTestId('composer-alert')).toContainText('Stored keys:');
  await expect(page.getByTestId('composer-alert')).toContainText('Claude (tuned)');
  await openSettings(page);
  await page.getByTestId('provider-anthropic').click();
  await expect(page.getByTestId('composer-alert')).toHaveCount(0);
});

test('creation failures reopen New save with its draft and a Settings action', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await prepareNew(page, 'Lena');
  await page.getByRole('button', { name: /Shape your character/ }).click();
  await page.getByPlaceholder('a scar, a tic, a bond - anything at all').fill('a red scarf');
  await page.route('**/api/game/new', (r) => r.fulfill({
    status: 428, contentType: 'application/json',
    body: '{"detail":{"error":"key_store_locked","provider":"anthropic"}}',
  }));
  await begin(page).click();
  await expect(page.getByTestId('new-error')).toContainText('Story not started');
  await expect(page.getByTestId('new-error')).toContainText('The key store is locked.');
  await expect(page.getByPlaceholder('a name for your character')).toHaveValue('Lena');
  await expect(page.getByPlaceholder('a scar, a tic, a bond - anything at all')).toHaveValue('a red scarf');
  await expect(page.getByTestId('composer-alert')).toHaveCount(0);
  await expect(page.getByText('Move not played', { exact: true })).toHaveCount(0);
  await page.getByTestId('new-error-settings').click();
  await expect(page.getByTestId('apikey-input')).toBeVisible();
  await closeSettings(page);
  await page.unroute('**/api/game/new');
  await page.route('**/api/game/new', (r) => r.fulfill({
    status: 503, contentType: 'application/json', body: '{"detail":{"error":"provider_unavailable"}}',
  }));
  await begin(page).click();
  await expect(page.getByTestId('new-error')).toContainText('The new story could not start. Try again in a moment.');
  await expect(page.getByPlaceholder('a name for your character')).toHaveValue('Lena');
});

test('Begin story retries a failed creation without re-entering its draft', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await prepareNew(page, 'Rin');
  await page.route('**/api/game/new', (r) => r.fulfill({
    status: 428, contentType: 'application/json',
    body: '{"detail":{"error":"key_store_locked","provider":"anthropic"}}',
  }));
  await begin(page).click();
  await expect(page.getByTestId('new-error')).toBeVisible();
  await expect(page.getByPlaceholder('a name for your character')).toHaveValue('Rin');
  await page.unroute('**/api/game/new');
  await expect(begin(page)).toBeEnabled();
  const [res] = await Promise.all([
    page.waitForResponse((r) => r.url().endsWith('/api/game/new')),
    begin(page).click(),
  ]);
  expect(res.status()).toBe(200);
  await expect(dmBlocks(page)).toHaveCount(1);
  await expect(page.getByTestId('new-error')).toHaveCount(0);
});

test('every provider works after saving, locking and unlocking its key', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  for (const provider of ['anthropic', 'openai', 'gemini', 'xai']) {
    await openSettings(page);
    await page.getByTestId('provider-' + provider).click();
    await page.getByTestId('apikey-input').fill(KEY);
    const [saved] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/keys/' + provider) && r.request().method() === 'PUT'),
      page.getByTestId('key-save').click(),
    ]);
    expect(saved.status()).toBe(200);
    await expect(page.getByTestId('key-status')).toContainText('Key set');
    await page.getByTestId('vault-lock').click();
    await expect(page.getByTestId('vault-passphrase')).toBeVisible();
    await unlock(page);
    await closeSettings(page);
    await closeMenu(page);
    const [created] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/game/new') && r.status() === 200),
      newGame(page),
    ]);
    expect(created.status()).toBe(200);
    const [played] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith('/api/game/turn')),
      send(page, TEXT),
    ]);
    expect(played.status()).toBe(200);
    await expect(dmBlocks(page)).toHaveCount(2);
  }
  await openSettings(page);
  for (const provider of ['openai', 'gemini', 'xai']) {
    await page.getByTestId('provider-' + provider).click();
    await page.getByTestId('key-remove').click();
    await expect(page.getByTestId('key-status')).toContainText('No key');
  }
});

test('a failed lock keeps the store unlocked and shows its error without a toast', async ({ page }) => {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await openSettings(page);
  await expect(page.getByTestId('composer-toast')).toHaveCount(0, { timeout: 15_000 });
  const label = await icon(page).getAttribute('aria-label');
  await page.route('**/api/keys/lock', (r) => r.fulfill({
    status: 500, contentType: 'application/json', body: '{"detail":{"error":"internal"}}',
  }));
  await page.getByTestId('vault-lock').click();
  await expect(page.getByTestId('keys-error')).toBeVisible();
  await expect(page.getByTestId('keys-error')).toHaveText('Something went wrong on the server. Your move was not played. Try again.');
  await expect(page.getByTestId('key-status')).toBeVisible();
  await expect(page.getByTestId('vault-passphrase')).toHaveCount(0);
  await expect(icon(page)).toHaveAttribute('aria-label', label!);
  await expect(page.getByTestId('composer-toast')).toHaveCount(0);
});
