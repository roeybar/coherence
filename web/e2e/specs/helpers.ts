import { expect, Page } from '@playwright/test';

// A canary, deliberately not key-shaped.
export const KEY = 'test-canary-key-7f3a';
export const TEXT = 'canary-player-text-91c2';

export const story = (page: Page) => page.getByTestId('story');
export const dmBlocks = (page: Page) => story(page).getByText('GameMaster', { exact: true });

// Panels toggle, so read the sphere's state before clicking it.
async function setOpen(page: Page, name: string, want: boolean) {
  const sphere = page.getByRole('button', { name, exact: true });
  if (((await sphere.getAttribute('aria-expanded')) === 'true') !== want) await sphere.click();
}

// the save controls sit inside the save row, so open it too
export async function openMenu(page: Page) {
  await setOpen(page, 'Menu', true);
  const row = page.getByTestId('save-row');
  if ((await row.getAttribute('aria-expanded')) !== 'true') await row.click();
}

export async function openSettings(page: Page) {
  await openMenu(page);
  await setOpen(page, 'Settings', true);
  const apikey = page.getByTestId('settings-apikey');
  if ((await apikey.getAttribute('aria-expanded')) !== 'true') await apikey.click();
}

export const closeSettings = (page: Page) => setOpen(page, 'Settings', false);

// the open panel covers the Menu sphere, so close it from inside
export async function closeMenu(page: Page) {
  const sphere = page.getByRole('button', { name: 'Menu', exact: true });
  if ((await sphere.getAttribute('aria-expanded')) !== 'true') return;
  await page.getByRole('button', { name: 'Close menu', exact: true }).click();
  await expect(sphere).toHaveAttribute('aria-expanded', 'false');
}

// A new game with no save passphrase opens the menu to ask for one; close it unless keepMenu.
export async function newGame(page: Page, character = 'Mira', keepMenu = false) {
  const packs = (await (await page.request.get('/api/packs')).json()).packs;
  await openMenu(page);
  await page.getByRole('button', { name: 'New save' }).click();
  await page.getByRole('button', { name: packs[0].name }).and(page.locator(':not([aria-label^="Enter "])')).first().click();
  await page.getByPlaceholder('a name for your character').fill(character);
  await page.getByRole('button', { name: 'Begin story' }).click();
  await expect(dmBlocks(page).first()).toBeVisible({ timeout: 60_000 });
  if (!keepMenu) await closeMenu(page);
}

export async function send(page: Page, text: string) {
  const box = page.getByPlaceholder('What do you do?');
  await box.fill(text);
  await box.press('Enter');
}

// The engine allows 5 unlocks a minute per address, and every page is a new session,
// so wait here instead of tripping the limit.
const unlocks: number[] = [];
export async function clickUnlock(page: Page) {
  const recent = () => unlocks.filter((at) => Date.now() - at < 61_000);
  while (recent().length >= 5) await page.waitForTimeout(1_000);
  unlocks.splice(0, unlocks.length, ...recent(), Date.now());
  await page.getByTestId('vault-unlock').click();
}

// Unlock the server key store and store a key for the default provider.
export async function addSelfHostKey(page: Page, vaultPass = 'e2e-vault-pass') {
  await openSettings(page);
  await page.getByTestId('vault-passphrase').fill(vaultPass);
  await clickUnlock(page);
  await page.getByTestId('apikey-input').fill(KEY);
  await page.getByTestId('key-save').click();
  await expect(page.getByTestId('key-status')).toContainText('Key set');
  await expect(page.getByTestId('apikey-input')).toHaveValue('');
  await closeSettings(page);
}

// Like addSelfHostKey, but also fine when the store is already unlocked or the key is already set.
export async function ensureSelfHostKey(page: Page, vaultPass = 'e2e-vault-pass') {
  await openSettings(page);
  const pass = page.getByTestId('vault-passphrase');
  const input = page.getByTestId('apikey-input');
  await expect(pass.or(input)).toBeVisible();
  if (await pass.isVisible()) {
    await pass.fill(vaultPass);
    await clickUnlock(page);
    // only the engine's per-minute unlock limit is waited out and tried once more;
    // any other refusal fails here
    const refused = page.getByTestId('keys-error');
    await expect(input.or(refused)).toBeVisible({ timeout: 15_000 });
    if (!(await input.isVisible())) {
      await expect(refused).toContainText('You can make one move every');
      await page.waitForTimeout(61_000);
      await expect(pass).toBeVisible();
      await pass.fill(vaultPass);
      await clickUnlock(page);
    }
  }
  await expect(input).toBeVisible();
  if (!(await page.getByTestId('key-status').innerText()).includes('Key set')) {
    await input.fill(KEY);
    await page.getByTestId('key-save').click();
    await expect(page.getByTestId('key-status')).toContainText('Key set');
  }
  await closeSettings(page);
}

// The saved-game records in the browser store, by id; {} while the database does not exist,
// so reading never creates it.
export async function idbRecords(page: Page): Promise<Record<string, string>> {
  return page.evaluate(async () => {
    if (!(await indexedDB.databases()).some((d) => d.name === 'coherence')) return {};
    return new Promise<Record<string, string>>((resolve) => {
      const req = indexedDB.open('coherence');
      req.onerror = () => resolve({});
      req.onsuccess = () => {
        const db = req.result;
        const out: Record<string, string> = {};
        if (!db.objectStoreNames.contains('saves')) { db.close(); resolve(out); return; }
        const tx = db.transaction('saves', 'readonly');
        const cur = tx.objectStore('saves').openCursor();
        cur.onsuccess = () => { const c = cur.result; if (c) { out[String(c.key)] = c.value; c.continue(); } };
        tx.oncomplete = () => { db.close(); resolve(out); };
        tx.onerror = () => { db.close(); resolve(out); };
      };
    });
  });
}

export const slotCount = async (page: Page) => Object.keys(await idbRecords(page)).length;
export const slotRows = (page: Page) => page.getByTestId('save-slot');

export async function setSavePass(page: Page, pass = 'e2e-save-pass') {
  await openMenu(page);
  await page.getByTestId('save-passphrase').fill(pass);
  await page.getByTestId('save-unlock').click();
}

// enter the stored game whose row shows this name (or the first row)
export async function enterSlot(page: Page, n = 0) {
  await openMenu(page);
  await page.getByTestId('save-slot-enter').nth(n).click();
}
