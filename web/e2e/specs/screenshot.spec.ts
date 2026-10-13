import { test, expect } from '@playwright/test';
import { story, dmBlocks, addSelfHostKey, newGame } from './helpers';

const OUT = process.env.SCREENSHOT_PATH || '/out/screenshot.png';

test('write the README screenshot', async ({ page }) => {
  await page.goto('/');
  await addSelfHostKey(page);
  await newGame(page);
  // the opening is the sample world's own text and needs no model call
  await expect(dmBlocks(page)).toHaveCount(1);
  await expect(story(page)).toContainText('They said you still take the ones nobody else will.');
  // the key toast fades after 4 s and the menu sphere lands back; shoot a settled screen
  await expect(page.getByTestId('composer-toast')).toHaveCount(0, { timeout: 15_000 });
  await page.waitForTimeout(1_000);
  await page.locator('.stage').screenshot({ path: OUT });
});
