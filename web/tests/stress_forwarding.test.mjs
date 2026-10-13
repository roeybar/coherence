import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createServer } from '../server.mjs';

const STRIPPED = ['forwarded', 'x-real-ip', 'cf-connecting-ip'];

function listen(server, host = '127.0.0.1') {
  return new Promise((resolve) => server.listen(0, host, () => resolve(server.address().port)));
}

function get(port, p, localAddress, headers) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: p, localAddress, headers, agent: false }, (res) => {
      res.resume();
      res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', reject);
    req.end();
  });
}

const octet = () => Math.floor(Math.random() * 256);
const randomIp = () => `${octet()}.${octet()}.${octet()}.${octet()}`;

test('300 concurrent mixed requests reach the engine with the exact forwarding header', { timeout: 60_000 }, async () => {
  const seen = [];
  const engine = http.createServer((req, res) => {
    seen.push({ url: req.url, headers: req.headers });
    req.resume();
    req.on('end', () => res.end('{}'));
  });
  const enginePort = await listen(engine);
  let calls = 0;
  const server = createServer({
    engineUrl: `http://127.0.0.1:${enginePort}`,
    tunnelHost: 'tunnel',
    tunnelTtlMs: 60000,
    resolve: async () => { calls += 1; return ['127.0.0.5']; },
    allowAddr: (a) => a.startsWith('127.'),
  });
  const port = await listen(server, '0.0.0.0');
  try {
    // the first answer fills the cache; requests that arrive before it count as direct
    assert.equal(await get(port, '/api/config?warm', '127.0.0.6', {}), 200);
    const expected = [];
    const runs = [];
    for (let i = 0; i < 300; i++) {
      const peer = i % 2 === 0 ? '127.0.0.5' : '127.0.0.6';
      const headers = { 'X-Forwarded-For': `${randomIp()}, ${randomIp()}`, Forwarded: `for=${randomIp()}`, 'X-Real-IP': randomIp() };
      const pick = Math.floor(Math.random() * 3);
      let cf = '';
      if (pick === 0) cf = randomIp();
      if (pick === 1) headers['CF-Connecting-IP'] = 'not-an-ip';
      if (cf) headers['CF-Connecting-IP'] = cf;
      expected.push(`${peer === '127.0.0.5' && cf ? cf : peer}, ${peer}`);
      runs.push(get(port, `/api/config?i=${i}`, peer, headers));
    }
    const statuses = await Promise.all(runs);
    assert.ok(statuses.every((st) => st === 200));
    const byIndex = new Map();
    for (const r of seen) {
      const m = /\?i=(\d+)$/.exec(r.url);
      if (m) byIndex.set(Number(m[1]), r.headers);
    }
    assert.equal(byIndex.size, 300);
    for (let i = 0; i < 300; i++) {
      const h = byIndex.get(i);
      assert.equal(h['x-forwarded-for'], expected[i], `request ${i}`);
      for (const name of STRIPPED) assert.equal(h[name], undefined, `request ${i} ${name}`);
    }
    assert.equal(calls, 1);
  } finally {
    server.close();
    engine.close();
  }
});
