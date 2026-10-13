import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { createServer } from '../server.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const pub = (...p) => path.join(root, 'public', ...p);

test('fonts.css points at woff2 files that exist and are real fonts', () => {
  const css = readFileSync(pub('fonts', 'fonts.css'), 'utf8');
  const urls = [...css.matchAll(/url\((\/fonts\/[^)]+)\)/g)].map((m) => m[1]);
  assert.equal(urls.length, 8);
  for (const u of urls) {
    const file = pub(u.replace(/^\//, ''));
    assert.ok(existsSync(file), u);
    assert.equal(readFileSync(file).subarray(0, 4).toString('latin1'), 'wOF2', u);
  }
  assert.match(css, /font-family: 'Newsreader'/);
  assert.match(css, /font-family: 'IBM Plex Mono'/);
  assert.ok(!/https?:/.test(css), 'no remote url');
  assert.ok(/font-display: swap/.test(css));
});

test('both font licences ship with the fonts', () => {
  for (const f of ['OFL-Newsreader.txt', 'OFL-IBMPlexMono.txt']) {
    const text = readFileSync(pub('fonts', f), 'utf8');
    assert.match(text, /SIL OPEN FONT LICENSE Version 1\.1/);
  }
});

test('the dice and the favicon are present and plain ASCII', () => {
  for (const n of ['4', '6', '8', '10', '12', '20', '100', 'inf']) {
    const svg = readFileSync(pub('dice', `d${n}.svg`), 'utf8');
    assert.match(svg, /^<svg /);
    assert.ok([...svg].every((c) => c.charCodeAt(0) < 128), `d${n}.svg is ASCII`);
  }
  assert.match(readFileSync(pub('dice', 'dinf.svg'), 'utf8'), /&#x221E;/);
  assert.match(readFileSync(pub('favicon.svg'), 'utf8'), /^<svg /);
});

test('index.html links the favicon and the font stylesheet', () => {
  const html = readFileSync(path.join(root, 'index.html'), 'utf8');
  assert.match(html, /href="\/favicon\.svg"/);
  assert.match(html, /href="\/fonts\/fonts\.css"/);
});

function get(port, url) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path: url }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    }).on('error', reject);
  });
}

test('the server types fonts and notices, and reports the build id', async () => {
  const dist = mkdtempSync(path.join(os.tmpdir(), 'dist-'));
  mkdirSync(path.join(dist, 'fonts'));
  const index = '<!doctype html><title>x</title>';
  writeFileSync(path.join(dist, 'index.html'), index);
  writeFileSync(path.join(dist, 'fonts', 'a.woff2'), 'wOF2');
  writeFileSync(path.join(dist, 'fonts', 'OFL.txt'), 'licence');
  const server = createServer({ engineUrl: 'http://127.0.0.1:9', dist });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const { port } = server.address();
  try {
    assert.equal((await get(port, '/fonts/a.woff2')).headers['content-type'], 'font/woff2');
    assert.equal((await get(port, '/fonts/OFL.txt')).headers['content-type'], 'text/plain; charset=utf-8');
    const b = await get(port, '/build.json');
    assert.equal(b.status, 200);
    assert.deepEqual(JSON.parse(b.body.toString()), { build: createHash('sha256').update(index).digest('hex').slice(0, 12) });
    assert.match(b.headers['content-security-policy'], /^default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'/);
  } finally {
    server.close();
  }
});
