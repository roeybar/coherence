import { test, expect, Page, Locator } from '@playwright/test';
import { TEXT, story, dmBlocks, openSettings, closeSettings, ensureSelfHostKey, newGame, send } from './helpers';

test.describe.configure({ mode: 'serial' });
test.setTimeout(240_000);

const turns = (page: Page) => story(page).getByTestId('turn');

// the last engine save seen on the wire, to compare the page with what the engine sent
function trackSaves(page: Page) {
  const seen: { save: any } = { save: null };
  page.on('response', async (res) => {
    if (!/\/api\/game\/(new|turn|undo-to-turn|regenerate)$/.test(res.url()) || !res.ok()) return;
    try {
      const body = await res.json();
      if (body && body.save) seen.save = body.save;
    } catch {
      // a body that is not JSON is not a save
    }
  });
  return seen;
}

async function longPress(page: Page, target: Locator) {
  await target.scrollIntoViewIfNeeded();
  const b = (await target.boundingBox())!;
  await page.mouse.move(b.x + b.width / 2, b.y + 12);
  await page.mouse.down();
  await page.waitForTimeout(600);
  await page.mouse.up();
}

async function play(page: Page, count: number) {
  await page.goto('/');
  await ensureSelfHostKey(page);
  await newGame(page);
  for (let n = 1; n <= count; n += 1) {
    await send(page, `${TEXT} ${n}`);
    await expect(dmBlocks(page)).toHaveCount(n + 1, { timeout: 60_000 });
  }
}

test('the map shows the place the engine put the player in', async ({ page }) => {
  const seen = trackSaves(page);
  await play(page, 0);
  await page.getByRole('button', { name: 'More', exact: true }).click();
  await page.getByRole('button', { name: 'Map', exact: true }).click();
  const here = page.getByTestId('place-here');
  await expect(here).toBeVisible();
  const location: string = seen.save.state.location;
  expect(location.length).toBeGreaterThan(0);
  await expect(here).toHaveText(location.trim());
});

test('long-press on a player turn, then Undo, rewinds through the engine', async ({ page }) => {
  await play(page, 1);
  await expect(turns(page)).toHaveCount(3);
  await longPress(page, turns(page).nth(1));
  const undo = turns(page).nth(1).getByTestId('turn-undo');
  await expect(undo).toBeEnabled();
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/game/undo-to-turn')),
    undo.click(),
  ]);
  expect(JSON.parse(req.postData() as string).turn_index).toBe(1);
  await expect(turns(page)).toHaveCount(1);
});

test('a comment on the last GameMaster turn is sent to regenerate', async ({ page }) => {
  await play(page, 1);
  await longPress(page, turns(page).nth(2));
  await turns(page).nth(2).getByTestId('turn-feedback').click();
  await expect(page.getByTestId('compose-mode')).toBeVisible();
  const box = page.getByPlaceholder('What did you think of this turn?');
  await box.fill('more rain please');
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/game/regenerate')),
    box.press('Enter'),
  ]);
  expect(JSON.parse(req.postData() as string).feedback).toBe('more rain please');
  await expect(page.getByTestId('compose-mode')).toHaveCount(0);
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });
});

test('with Submit key set to Shift + Enter, Enter adds a line and Shift + Enter sends', async ({ page }) => {
  await play(page, 0);
  await openSettings(page);
  await page.getByTestId('settings-submitkey').click();
  await page.getByRole('button', { name: 'Shift + Enter' }).click();
  await closeSettings(page);
  const box = page.getByPlaceholder('What do you do?');
  await box.fill('first line');
  await box.press('Enter');
  await expect(box).toHaveValue('first line\n');
  await box.pressSequentially('second line');
  const [req] = await Promise.all([
    page.waitForRequest((r) => r.url().endsWith('/api/game/turn')),
    box.press('Shift+Enter'),
  ]);
  expect(JSON.parse(req.postData() as string).player_input).toBe('first line\nsecond line');
  await expect(dmBlocks(page)).toHaveCount(2, { timeout: 60_000 });
});

test('an unreachable engine shows the maintenance notice, then clears when it is back', async ({ page }) => {
  await play(page, 0);
  await page.route('**/api/game/turn', (route) => route.fulfill({
    status: 502, contentType: 'application/json', body: '{"detail":{"error":"engine_unavailable"}}',
  }));
  const box = page.getByPlaceholder('What do you do?');
  await box.fill(TEXT);
  await box.press('Enter');
  const alert = page.getByTestId('composer-alert');
  await expect(alert).toHaveAttribute('data-kind', 'maint');
  await expect(alert).toContainText('Maintenance');
  const parked = page.getByPlaceholder('Back in a moment...');
  await expect(parked).toBeDisabled();
  await expect(parked).toHaveValue(TEXT);
  await page.unroute('**/api/game/turn');
  await expect(page.getByTestId('composer-alert')).toHaveCount(0, { timeout: 30_000 });
  await expect(page.getByPlaceholder('What do you do?')).toBeEnabled();
});

test('a dice gate shows the drawn die', async ({ page }) => {
  await play(page, 0);
  await page.route('**/api/game/turn', (route) => route.fulfill({
    status: 200, contentType: 'application/json',
    body: JSON.stringify({ kind: 'dice_roll_needed', message: 'Chance decides this one.', dice_sides: 20 }),
  }));
  await send(page, TEXT);
  const die = page.getByAltText('d20');
  await expect(die).toBeVisible();
  await expect.poll(() => die.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBeGreaterThan(0);
});

test('the fonts load from this origin and the page raises no policy errors', async ({ page }) => {
  const urls: string[] = [];
  const problems: string[] = [];
  page.on('request', (r) => urls.push(r.url()));
  page.on('console', (m) => { if (/content security policy|refused to/i.test(m.text())) problems.push(m.text()); });
  await play(page, 0);
  const loaded = await page.evaluate(async () => {
    await document.fonts.load('italic 16px Newsreader');
    await document.fonts.load("11px 'IBM Plex Mono'");
    await document.fonts.ready;
    return {
      newsreader: document.fonts.check('16px Newsreader'),
      mono: document.fonts.check("11px 'IBM Plex Mono'"),
      faces: [...document.fonts].filter((f) => f.status === 'loaded').map((f) => f.family.replace(/['"]/g, '')),
    };
  });
  expect(loaded.newsreader).toBe(true);
  expect(loaded.mono).toBe(true);
  expect(loaded.faces).toContain('Newsreader');
  expect(loaded.faces).toContain('IBM Plex Mono');
  const origin = new URL(page.url()).origin;
  expect(urls.filter((u) => !u.startsWith(origin) && !u.startsWith('data:') && !u.startsWith('blob:'))).toEqual([]);
  expect(problems).toEqual([]);
});

test('on a wide window the stage is a 402 by 874 column in the middle', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/');
  const box = (await page.locator('.stage').boundingBox())!;
  expect(box.width).toBe(402);
  expect(box.height).toBe(874);
  expect(Math.abs(box.x - 439)).toBeLessThanOrEqual(1);
});

test('stress: 30 long-presses in a row leave at most one action row open', async ({ page }) => {
  await play(page, 1);
  for (let i = 0; i < 30; i += 1) {
    await longPress(page, turns(page).nth(i % 3));
    await expect(story(page).locator('[data-testid="turn-actions"][data-open="true"]')).toHaveCount(1);
  }
  await story(page).click({ position: { x: 5, y: 5 } });
  await expect(story(page).locator('[data-testid="turn-actions"][data-open="true"]')).toHaveCount(0);
});
