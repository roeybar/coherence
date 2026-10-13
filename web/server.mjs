// Static server for the built client plus a plain proxy for /api.
// Nothing about a request (path, headers, body) is ever logged.
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { Resolver } from 'node:dns/promises';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
};

const SECURITY = {
  'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
  'Cross-Origin-Opener-Policy': 'same-origin',
};

const HOP = new Set(['host', 'connection', 'keep-alive', 'upgrade', 'proxy-connection']);
// forwarding headers from a client are never passed on
const STRIP = new Set(['x-forwarded-for', 'forwarded', 'x-real-ip', 'cf-connecting-ip']);
const EMPTY = new Set();
const LOOKUP_WAIT_MS = 2000;

const plain = (a) => (a || '').replace(/^::ffff:/, '');

// the address the tunnel names, or '' when the value is not one address
function tunnelClient(v) {
  const a = plain(String(v || '').trim());
  return net.isIP(a) ? a : '';
}

function v4(a) {
  return a.split('.').reduce((n, p) => n * 256 + Number(p), 0);
}

function v6(a) {
  const [head, tail] = a.split('::');
  const part = (s) => (s ? s.split(':') : []);
  const h = part(head);
  const t = tail === undefined ? [] : part(tail);
  const fill = tail === undefined ? [] : Array(8 - h.length - t.length).fill('0');
  return [...h, ...fill, ...t].map((w) => parseInt(w, 16));
}

// true when the address is in a subnet of this container's own interfaces and is not an IPv4 .1 gateway
export function inOwnSubnet(a, interfaces = os.networkInterfaces()) {
  const kind = net.isIP(a);
  for (const list of Object.values(interfaces)) {
    for (const i of list || []) {
      if (i.internal) continue;
      if (kind === 4 && i.family === 'IPv4') {
        const mask = v4(i.netmask);
        const net4 = (v4(i.address) & mask) >>> 0;
        if (((v4(a) & mask) >>> 0) === net4 && v4(a) !== net4 + 1) return true;
      } else if (kind === 6 && i.family === 'IPv6') {
        const x = v6(a);
        const y = v6(i.address);
        if (x.slice(0, 4).every((w, k) => w === y[k])) return true;
      }
    }
  }
  return false;
}

// the A and AAAA addresses of a host name
async function resolveHost(host) {
  const r = new Resolver({ timeout: 2000, tries: 1 });
  const answers = await Promise.allSettled([r.resolve4(host), r.resolve6(host)]);
  return answers.flatMap((x) => (x.status === 'fulfilled' ? x.value : []));
}

function sendJson(res, status, body) {
  if (res.headersSent) return;
  res.writeHead(status, { ...SECURITY, 'Content-Type': 'application/json', Connection: 'close' });
  res.end(JSON.stringify(body));
}

const tooLarge = (res) => sendJson(res, 413, { detail: { error: 'body_too_large' } });

// the file under dist for a decoded path, or null when the path leaves dist
export function resolveStatic(dist, rel) {
  const file = path.join(dist, path.normalize(rel));
  return file.startsWith(dist + path.sep) ? file : null;
}

// allowAddr exists for tests; the entrypoint below never passes it, so it is always the subnet check there
export function createServer({
  engineUrl,
  dist = path.join(here, 'dist'),
  bodyCap = 134217728,
  tunnelHost = '',
  resolve = resolveHost,
  tunnelTtlMs = 10000,
  allowAddr = inOwnSubnet,
} = {}) {
  const target = new URL(engineUrl);
  let cached = EMPTY;
  let cachedAt = -Infinity;
  let pending = null;

  // the tunnel container's addresses; any lookup failure leaves the set empty, so every request counts as direct
  function tunnelAddrs() {
    if (!tunnelHost) return Promise.resolve(EMPTY);
    if (Date.now() - cachedAt < tunnelTtlMs || pending) return Promise.resolve(cached);
    pending = Promise.resolve()
      .then(() => resolve(tunnelHost))
      .then(
        (list) => new Set((list || []).map(plain).filter((a) => net.isIP(a) && allowAddr(a))),
        () => EMPTY,
      )
      .then((set) => {
        cached = set;
        cachedAt = Date.now();
        pending = null;
        return set;
      });
    let timer;
    const late = new Promise((done) => {
      timer = setTimeout(() => {
        cached = EMPTY;
        done(EMPTY);
      }, LOOKUP_WAIT_MS);
    });
    return Promise.race([pending, late]).finally(() => clearTimeout(timer));
  }

  // identifies the page this server was built with, so an open tab can tell it is stale
  let build = '';
  try {
    build = createHash('sha256').update(readFileSync(path.join(dist, 'index.html'))).digest('hex').slice(0, 12);
  } catch {
    build = '';
  }

  async function proxy(req, res) {
    if (Number(req.headers['content-length'] || 0) > bodyCap) return tooLarge(res);
    const peer = plain(req.socket.remoteAddress);
    const fromTunnel = (await tunnelAddrs()).has(peer);
    if (req.destroyed || res.destroyed) return;
    const client = (fromTunnel && tunnelClient(req.headers['cf-connecting-ip'])) || peer;
    const headers = {};
    for (const [k, v] of Object.entries(req.headers)) if (!HOP.has(k) && !STRIP.has(k)) headers[k] = v;
    // the engine counts the second entry from the right in hosted mode, the last in self-host
    headers['x-forwarded-for'] = `${client}, ${peer}`;
    const up = http.request(
      { hostname: target.hostname, port: target.port || 80, method: req.method, path: req.url, headers },
      (ur) => {
        res.writeHead(ur.statusCode, { ...ur.headers, ...SECURITY });
        ur.on('error', () => res.destroy());
        ur.on('close', () => { if (!ur.complete) res.destroy(); });
        ur.pipe(res);
      },
    );
    // a turn may make several provider calls of up to 120 s each
    up.setTimeout(600000, () => up.destroy());
    up.on('error', () => {
      // once the engine's headers went out, end the response so the browser request does not hang
      // a response that already ended (the 413) keeps its body
      if (res.headersSent) { if (!res.writableEnded) res.destroy(); }
      else sendJson(res, 502, { detail: { error: 'engine_unavailable' } });
    });
    let seen = 0;
    req.on('data', (chunk) => {
      seen += chunk.length;
      if (seen > bodyCap && !up.destroyed) {
        req.unpipe(up);
        up.destroy();
        tooLarge(res);
      }
    });
    req.pipe(up);
    // a browser that leaves mid-upload closes the engine request now, not at the 600 s timeout
    req.on('close', () => { if (!req.complete && !up.destroyed) up.destroy(); });
  }

  async function serveStatic(req, res) {
    if (req.method !== 'GET' && req.method !== 'HEAD') return sendJson(res, 405, { detail: { error: 'method' } });
    let rel;
    try {
      rel = decodeURIComponent(new URL(req.url, 'http://x').pathname);
    } catch {
      return sendJson(res, 400, { detail: { error: 'path' } });
    }
    if (rel === '/healthz') {
      res.writeHead(200, { ...SECURITY, 'Content-Type': 'text/plain' });
      return res.end('ok');
    }
    if (rel === '/build.json') {
      res.writeHead(200, { ...SECURITY, 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
      return res.end(JSON.stringify({ build }));
    }
    if (rel.endsWith('/')) rel += 'index.html';
    const file = resolveStatic(dist, rel);
    if (!file) return sendJson(res, 404, { detail: { error: 'not_found' } });
    try {
      const data = await readFile(file);
      const cache = rel.startsWith('/assets/') ? 'public, max-age=31536000, immutable' : 'no-cache';
      res.writeHead(200, { ...SECURITY, 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream', 'Cache-Control': cache });
      res.end(req.method === 'HEAD' ? undefined : data);
    } catch {
      sendJson(res, 404, { detail: { error: 'not_found' } });
    }
  }

  return http.createServer((req, res) => {
    if (req.url === '/api' || req.url.startsWith('/api/')) return proxy(req, res).catch(() => res.destroy());
    return serveStatic(req, res);
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env.PORT || 8080);
  const server = createServer({
    engineUrl: process.env.ENGINE_URL || 'http://engine:8000',
    bodyCap: Number(process.env.BODY_CAP_BYTES || 134217728),
    tunnelHost: process.env.TUNNEL_HOST || '',
  });
  server.on('error', (err) => console.error('web: server error:', err.code || err.name));
  server.listen(port, '0.0.0.0', () => console.log(`web: listening on ${port}`));
}
