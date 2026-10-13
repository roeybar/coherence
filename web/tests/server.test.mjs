import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import path from 'node:path';
import { mkdtempSync, mkdirSync, writeFileSync } from 'node:fs';
import { createServer, inOwnSubnet, resolveStatic } from '../server.mjs';

function listen(server, host = '127.0.0.1') {
  return new Promise((resolve) => server.listen(0, host, () => resolve(server.address().port)));
}

// a stub engine that records what reaches it
async function stubEngine(reply = (res) => res.end('{"ok":true}')) {
  const seen = [];
  const opened = { count: 0 };
  const server = http.createServer((req, res) => {
    opened.count += 1;
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      seen.push({ method: req.method, url: req.url, headers: req.headers, bytes: Buffer.concat(chunks).length });
      res.setHeader('Content-Type', 'application/json');
      reply(res);
    });
  });
  const port = await listen(server);
  return { seen, opened, port, close: () => server.close() };
}

function request(port, { method = 'GET', path: p = '/', headers = {}, body, localAddress, chunked = false }) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method, path: p, headers, localAddress }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, text: Buffer.concat(chunks).toString() }));
    });
    req.on('error', reject);
    if (body !== undefined && chunked) {
      req.write(body.subarray(0, 1));
      req.write(body.subarray(1));
    } else if (body !== undefined) req.write(body);
    req.end();
  });
}

async function start(opts = {}) {
  const engine = await stubEngine(opts.reply);
  const server = createServer({
    engineUrl: `http://127.0.0.1:${engine.port}`,
    dist: opts.dist,
    bodyCap: opts.bodyCap,
    tunnelHost: opts.tunnelHost,
    resolve: opts.resolve,
    tunnelTtlMs: opts.tunnelTtlMs,
    allowAddr: opts.allowAddr,
  });
  const port = await listen(server, '0.0.0.0');
  return { engine, port, close: () => { server.close(); engine.close(); } };
}

const STRIPPED = ['forwarded', 'x-real-ip', 'cf-connecting-ip'];
const TUNNEL = { tunnelHost: 'tunnel', resolve: async () => ['127.0.0.5'], allowAddr: (a) => a.startsWith('127.') };
const xffOf = (s) => s.engine.seen.map((r) => r.headers['x-forwarded-for']);

test('a direct request is counted under its socket peer, whatever forwarding headers it sends', async () => {
  const s = await start();
  await request(s.port, { path: '/api/config', localAddress: '127.0.0.1' });
  await request(s.port, { path: '/api/config', localAddress: '127.0.0.2' });
  await request(s.port, { path: '/api/config', localAddress: '127.0.0.1', headers: { 'X-Forwarded-For': '192.0.2.11' } });
  await request(s.port, {
    path: '/api/config',
    localAddress: '127.0.0.3',
    headers: { 'X-Forwarded-For': '192.0.2.11, 192.0.2.22', Forwarded: 'for=192.0.2.11', 'X-Real-IP': '192.0.2.11', 'CF-Connecting-IP': '192.0.2.11' },
  });
  s.close();
  assert.deepEqual(xffOf(s), ['127.0.0.1, 127.0.0.1', '127.0.0.2, 127.0.0.2', '127.0.0.1, 127.0.0.1', '127.0.0.3, 127.0.0.3']);
  for (const r of s.engine.seen) for (const h of STRIPPED) assert.equal(r.headers[h], undefined);
});

test('a request from the tunnel peer is counted under the address the tunnel supplies', async () => {
  const s = await start(TUNNEL);
  const from = (localAddress, headers = {}) => request(s.port, { path: '/api/config', localAddress, headers });
  await from('127.0.0.5', { 'CF-Connecting-IP': '203.0.113.7', 'X-Forwarded-For': '192.0.2.11', Forwarded: 'for=192.0.2.11' });
  await from('127.0.0.5', { 'CF-Connecting-IP': '2001:db8::1' });
  await from('127.0.0.5', { 'CF-Connecting-IP': 'not-an-ip' });
  await from('127.0.0.5', { 'CF-Connecting-IP': ['203.0.113.7', '203.0.113.8'] });
  await from('127.0.0.5');
  await from('127.0.0.6', { 'CF-Connecting-IP': '203.0.113.7' });
  s.close();
  assert.deepEqual(xffOf(s), [
    '203.0.113.7, 127.0.0.5',
    '2001:db8::1, 127.0.0.5',
    '127.0.0.5, 127.0.0.5',
    '127.0.0.5, 127.0.0.5',
    '127.0.0.5, 127.0.0.5',
    '127.0.0.6, 127.0.0.6',
  ]);
  for (const r of s.engine.seen) for (const h of STRIPPED) assert.equal(r.headers[h], undefined);
});

test('a failed or empty lookup treats every request as direct', async () => {
  for (const resolve of [async () => { throw new Error('ENOTFOUND'); }, async () => []]) {
    const s = await start({ ...TUNNEL, resolve });
    await request(s.port, { path: '/api/config', localAddress: '127.0.0.5', headers: { 'CF-Connecting-IP': '203.0.113.7' } });
    s.close();
    assert.deepEqual(xffOf(s), ['127.0.0.5, 127.0.0.5']);
  }
});

test('a lookup that never settles leaves requests direct and starts no second lookup', { timeout: 20_000 }, async () => {
  let calls = 0;
  const s = await start({ ...TUNNEL, tunnelTtlMs: 0, resolve: () => { calls += 1; return new Promise(() => {}); } });
  const headers = { 'CF-Connecting-IP': '203.0.113.7' };
  await request(s.port, { path: '/api/config', localAddress: '127.0.0.5', headers });
  await request(s.port, { path: '/api/config', localAddress: '127.0.0.5', headers });
  s.close();
  assert.deepEqual(xffOf(s), ['127.0.0.5, 127.0.0.5', '127.0.0.5, 127.0.0.5']);
  assert.equal(calls, 1);
});

test('without a tunnel host the lookup is never called', async () => {
  let calls = 0;
  const s = await start({ ...TUNNEL, tunnelHost: '', resolve: async () => { calls += 1; return ['127.0.0.5']; } });
  await request(s.port, { path: '/api/config', localAddress: '127.0.0.5', headers: { 'CF-Connecting-IP': '203.0.113.7' } });
  s.close();
  assert.deepEqual(xffOf(s), ['127.0.0.5, 127.0.0.5']);
  assert.equal(calls, 0);
});

test('the lookup runs once per TTL and never overlaps', async () => {
  let calls = 0;
  let active = 0;
  let most = 0;
  const resolve = async () => {
    calls += 1;
    active += 1;
    most = Math.max(most, active);
    await new Promise((r) => setTimeout(r, 30));
    active -= 1;
    return ['127.0.0.5'];
  };
  const cachedRun = await start({ ...TUNNEL, resolve, tunnelTtlMs: 60000 });
  for (let i = 0; i < 3; i++) await request(cachedRun.port, { path: '/api/config', localAddress: '127.0.0.5' });
  cachedRun.close();
  assert.equal(calls, 1);
  calls = 0;
  const everyRun = await start({ ...TUNNEL, resolve, tunnelTtlMs: 0 });
  await Promise.all(Array.from({ length: 6 }, () => request(everyRun.port, { path: '/api/config', localAddress: '127.0.0.5' })));
  for (let i = 0; i < 3; i++) await request(everyRun.port, { path: '/api/config', localAddress: '127.0.0.5' });
  everyRun.close();
  assert.ok(calls >= 4);
  assert.equal(most, 1);
});

test('a client that aborts during the lookup opens no upstream request', { timeout: 10_000 }, async () => {
  const s = await start({ ...TUNNEL, resolve: () => new Promise((r) => setTimeout(() => r(['127.0.0.5']), 300)) });
  await new Promise((done) => {
    const req = http.request({ host: '127.0.0.1', port: s.port, path: '/api/config', localAddress: '127.0.0.5' });
    req.on('error', () => done());
    req.end();
    setTimeout(() => { req.destroy(); done(); }, 50);
  });
  await new Promise((r) => setTimeout(r, 600));
  s.close();
  assert.equal(s.engine.opened.count, 0);
});

test('the session and provider headers are forwarded', async () => {
  const s = await start();
  const id = '3f2b8c1e-9d4a-4e7b-8a61-2c5d7e9f0a1b';
  await request(s.port, { method: 'POST', path: '/api/game/turn', headers: { 'X-Coherence-Session': id, 'X-Provider': 'xai', 'X-Provider-Key': 'test-canary-key-7f3a' }, body: Buffer.from('{}') });
  s.close();
  const h = s.engine.seen[0].headers;
  assert.equal(h['x-coherence-session'], id);
  assert.equal(h['x-provider'], 'xai');
  assert.equal(h['x-provider-key'], 'test-canary-key-7f3a');
});

test('the default cap refuses 134217729 bytes before opening an upstream request', async () => {
  const s = await start();
  try {
    const over = await request(s.port, { method: 'POST', path: '/api/game/turn', headers: { 'Content-Length': '134217729' } });
    assert.equal(over.status, 413);
    assert.deepEqual(JSON.parse(over.text), { detail: { error: 'body_too_large' } });
    assert.equal(s.engine.opened.count, 0);
  } finally {
    s.close();
  }
});

test('the default cap allows a header-only probe at exactly 134217728 bytes', { timeout: 10_000 }, async () => {
  const server = createServer({ engineUrl: 'http://127.0.0.1:9' });
  const port = await listen(server);
  try {
    const at = await request(port, { method: 'POST', path: '/api/game/turn', headers: { 'Content-Length': '134217728' } });
    assert.notEqual(at.status, 413);
    assert.equal(at.status, 502);
  } finally {
    server.close();
  }
});

test('the default cap forwards a body larger than 16777216 bytes', async () => {
  const s = await start();
  try {
    const r = await request(s.port, { method: 'POST', path: '/api/game/turn', body: Buffer.alloc(16777217) });
    assert.equal(r.status, 200);
    assert.equal(s.engine.seen[0].bytes, 16777217);
  } finally {
    s.close();
  }
});

test('a body over an explicit 16777216 byte cap gets 413 with the error envelope', async () => {
  const s = await start({ bodyCap: 16777216 });
  const over = await request(s.port, { method: 'POST', path: '/api/game/turn', body: Buffer.alloc(16777217) });
  s.close();
  assert.equal(over.status, 413);
  assert.deepEqual(JSON.parse(over.text), { detail: { error: 'body_too_large' } });
  assert.equal(s.engine.seen.length, 0);
});

test('the cap is exact, and chunked bodies are counted too', async () => {
  const s = await start({ bodyCap: 10 });
  const at = await request(s.port, { method: 'POST', path: '/api/x', body: Buffer.alloc(10) });
  const over = await request(s.port, { method: 'POST', path: '/api/x', body: Buffer.alloc(11), chunked: true });
  s.close();
  assert.equal(at.status, 200);
  assert.equal(over.status, 413);
});

test('a client that keeps uploading past the cap still gets the 413 envelope', { timeout: 10_000 }, async () => {
  const s = await start({ bodyCap: 1024 });
  const r = await new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port: s.port, method: 'POST', path: '/api/game/turn' }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, text: Buffer.concat(chunks).toString() }));
      res.on('error', reject);
    });
    // a write may fail once the server has answered; the answer is what counts
    req.on('error', () => {});
    let sent = 0;
    const pump = () => {
      if (sent >= 64 * 1024 || req.destroyed) { req.end(); return; }
      sent += 512;
      req.write(Buffer.alloc(512));
      setTimeout(pump, 2);
    };
    pump();
  });
  s.close();
  assert.equal(r.status, 413);
  assert.deepEqual(JSON.parse(r.text), { detail: { error: 'body_too_large' } });
});

test('engine status and body pass through unchanged', async () => {
  const s = await start({ reply: (res) => { res.statusCode = 429; res.end('{"detail":{"error":"rate_limited","retry_after":3}}'); } });
  const r = await request(s.port, { method: 'POST', path: '/api/game/turn', body: Buffer.from('{}') });
  s.close();
  assert.equal(r.status, 429);
  assert.deepEqual(JSON.parse(r.text), { detail: { error: 'rate_limited', retry_after: 3 } });
});

test('an unreachable engine gives 502', async () => {
  const server = createServer({ engineUrl: 'http://127.0.0.1:9' });
  const port = await listen(server);
  const r = await request(port, { path: '/api/config' });
  server.close();
  assert.equal(r.status, 502);
});

test('static files carry the security headers; traversal is refused', async () => {
  const dist = mkdtempSync(path.join(os.tmpdir(), 'dist-'));
  writeFileSync(path.join(dist, 'index.html'), '<!doctype html><title>x</title>');
  const s = await start({ dist });
  const index = await request(s.port, { path: '/' });
  const health = await request(s.port, { path: '/healthz' });
  const trav = await request(s.port, { path: '/%2e%2e/server.mjs' });
  const post = await request(s.port, { method: 'POST', path: '/' });
  s.close();
  assert.equal(index.status, 200);
  assert.match(index.headers['content-security-policy'], /connect-src 'self'/);
  assert.equal(index.headers['x-content-type-options'], 'nosniff');
  assert.equal(health.status, 200);
  assert.equal(trav.status, 404);
  assert.equal(post.status, 405);
});

test('a running server logs its startup line and nothing per request', async () => {
  const engine = await stubEngine();
  const child = spawn(process.execPath, [fileURLToPath(new URL('../server.mjs', import.meta.url))], {
    env: { ...process.env, PORT: '18099', ENGINE_URL: `http://127.0.0.1:${engine.port}` },
  });
  let out = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { out += d; });
  try {
    for (let i = 0; i < 50 && !out.includes('listening'); i++) await new Promise((r) => setTimeout(r, 100));
    assert.match(out, /listening on 18099/);
    out = '';
    for (let i = 0; i < 5; i++) {
      await request(18099, { method: 'POST', path: '/api/game/turn?secret=1', headers: { 'X-Provider-Key': 'test-canary-key-7f3a' }, body: Buffer.from('canary-player-text-91c2') });
    }
    assert.equal(engine.seen.length, 5);
    assert.equal(out, '');
  } finally {
    child.kill();
    engine.close();
  }
});

test('a TUNNEL_HOST that resolves to loopback leaves requests direct', { timeout: 20_000 }, async () => {
  const engine = await stubEngine();
  const child = spawn(process.execPath, [fileURLToPath(new URL('../server.mjs', import.meta.url))], {
    env: { ...process.env, PORT: '18098', ENGINE_URL: `http://127.0.0.1:${engine.port}`, TUNNEL_HOST: 'localhost' },
  });
  let out = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { out += d; });
  try {
    for (let i = 0; i < 50 && !out.includes('listening'); i++) await new Promise((r) => setTimeout(r, 100));
    assert.match(out, /listening on 18098/);
    await request(18098, { path: '/api/config', localAddress: '127.0.0.2', headers: { 'CF-Connecting-IP': '203.0.113.9' } });
    assert.equal(engine.seen[0].headers['x-forwarded-for'], '127.0.0.2, 127.0.0.2');
  } finally {
    child.kill();
    engine.close();
  }
});

test('an engine that drops mid-response ends the browser response', { timeout: 10_000 }, async () => {
  const s = await start({
    reply: (res) => {
      res.writeHead(200);
      res.write('{"narrative":"par');
      setTimeout(() => res.socket.destroy(), 50);
    },
  });
  const outcome = await new Promise((resolve) => {
    const req = http.request({ host: '127.0.0.1', port: s.port, path: '/api/game/turn', method: 'POST' }, (res) => {
      res.on('data', () => {});
      res.on('end', () => resolve('end'));
      res.on('error', () => resolve('aborted'));
      res.on('close', () => resolve(res.complete ? 'end' : 'aborted'));
    });
    req.on('error', () => resolve('aborted'));
    req.end('{}');
  });
  s.close();
  assert.equal(outcome, 'aborted');
});

test('an upload the browser abandons mid-body closes the engine request at once; a whole one is untouched', { timeout: 10_000 }, async () => {
  const events = [];
  const engine = http.createServer((req, res) => {
    let bytes = 0;
    req.on('data', (c) => { bytes += c.length; });
    req.on('end', () => { events.push(['end', bytes]); res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true}'); });
    req.on('close', () => { if (!req.complete) events.push(['cut', bytes]); });
  });
  const enginePort = await listen(engine);
  const server = createServer({ engineUrl: `http://127.0.0.1:${enginePort}` });
  const port = await listen(server);
  try {
    const whole = await request(port, { method: 'POST', path: '/api/game/turn', headers: { 'Content-Type': 'application/json' }, body: Buffer.alloc(1024 * 1024, 120) });
    assert.equal(whole.status, 200);
    assert.deepEqual(events, [['end', 1024 * 1024]]);
    const started = Date.now();
    await new Promise((done) => {
      const req = http.request({ host: '127.0.0.1', port, method: 'POST', path: '/api/game/turn', headers: { 'Content-Length': String(1024 * 1024) } });
      req.on('error', () => {});
      req.write(Buffer.alloc(4096, 120));
      setTimeout(() => { req.destroy(); done(); }, 200);
    });
    while (events.length < 2 && Date.now() - started < 5000) await new Promise((r) => setTimeout(r, 20));
    assert.equal(events.length, 2);
    assert.equal(events[1][0], 'cut');
    assert.ok(Date.now() - started < 5000);
  } finally {
    server.close();
    engine.close();
  }
});

test('the static resolver refuses a decoded path that leaves dist', async () => {
  const root = mkdtempSync(path.join(os.tmpdir(), 'site-'));
  const dist = path.join(root, 'dist');
  mkdirSync(dist);
  writeFileSync(path.join(dist, 'index.html'), '<!doctype html><title>x</title>');
  writeFileSync(path.join(root, 'sibling.txt'), 'sibling-canary-55e1');
  assert.equal(resolveStatic(dist, '../sibling.txt'), null);
  assert.equal(resolveStatic(dist, 'a/../../sibling.txt'), null);
  assert.equal(resolveStatic(dist, '/index.html'), path.join(dist, 'index.html'));
  const s = await start({ dist });
  const trav = await request(s.port, { path: '/..%2fsibling.txt' });
  const deep = await request(s.port, { path: '/a/..%2f..%2fsibling.txt' });
  s.close();
  for (const r of [trav, deep]) {
    assert.equal(r.status, 404);
    assert.ok(!r.text.includes('sibling-canary-55e1'));
  }
});

test("the tunnel address must sit in one of the container's own subnets", () => {
  const ifaces = {
    lo: [{ address: '127.0.0.1', netmask: '255.0.0.0', family: 'IPv4', internal: true }],
    eth0: [
      { address: '172.18.0.3', netmask: '255.255.0.0', family: 'IPv4', internal: false },
      { address: 'fd00:1:2:3::3', netmask: 'ffff:ffff:ffff:ffff::', family: 'IPv6', internal: false },
    ],
    eth1: [{ address: '192.168.5.3', netmask: '255.255.255.0', family: 'IPv4', internal: true }],
  };
  assert.equal(inOwnSubnet('172.18.0.4', ifaces), true);
  assert.equal(inOwnSubnet('172.18.0.1', ifaces), false);
  assert.equal(inOwnSubnet('10.9.0.4', ifaces), false);
  assert.equal(inOwnSubnet('127.0.0.1', ifaces), false);
  assert.equal(inOwnSubnet('192.168.5.4', ifaces), false);
  assert.equal(inOwnSubnet('fd00:1:2:3::9', ifaces), true);
  assert.equal(inOwnSubnet('fd00:1:2:4::9', ifaces), false);
});
