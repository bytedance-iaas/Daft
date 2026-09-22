#!/usr/bin/env node
// Serves the built frontend the way the Daemon does (doc 07 §2.3), to check a build by hand:
//   - static files of dist/ under the mount prefix, index.html for every other page path
//     (so refreshing /curation/tasks/1/report does not 404);
//   - the prefix injected into index.html at the <!-- curator:base --> marker, as a static
//     <base href="{base}/"> plus window.__CURATOR_BASE__ (exactly what the Daemon should do);
//   - optionally {base}/api and {base}/events proxied to a running Daemon (SSE streams through).
//
// Usage: node scripts/serve-dist.mjs [--base /curation] [--port 4173] [--dir dist] [--api http://127.0.0.1:8080]
// No dependencies beyond Node itself.
import { createReadStream, existsSync, readFileSync, statSync } from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import { extname, join, normalize, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const base = arg('base', process.env.CURATOR_BASE ?? '/curation').replace(/\/+$/, '');
if (base && !/^\/[A-Za-z0-9._~\-/]*$/.test(base)) {
  console.error(`--base must look like /curation, got ${base}`);
  process.exit(2);
}
const port = Number(arg('port', '4173'));
const dir = resolve(here, '..', arg('dir', 'dist'));
const api = arg('api', process.env.CURATOR_API ?? '');

if (!existsSync(join(dir, 'index.html'))) {
  console.error(`${dir}/index.html not found: run npm run build (or npm run build:demo) first`);
  process.exit(2);
}

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.json': 'application/json',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.map': 'application/json',
};

// What the Daemon does: replace the marker with the prefix (JSON-encoded, so it is always a JS
// string). Read on every request, so a rebuild needs no restart.
function sendIndex(res) {
  const html = readFileSync(join(dir, 'index.html'), 'utf8').replace('<!-- curator:base -->', `<base href="${base}/"><script>window.__CURATOR_BASE__ = ${JSON.stringify(base)};</script>`);
  res.writeHead(200, { 'Content-Type': TYPES['.html'], 'Cache-Control': 'no-cache' });
  res.end(html);
}

function proxy(req, res) {
  const target = new URL(req.url, api);
  const client = target.protocol === 'https:' ? https : http;
  const upstream = client.request(target, { method: req.method, headers: { ...req.headers, host: target.host } }, (up) => {
    res.writeHead(up.statusCode ?? 502, up.headers);
    up.pipe(res);
  });
  upstream.on('error', (e) => {
    res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ error: { code: 'internal', message: `连不上 Daemon（${api}）：${e.message}` } }));
  });
  req.pipe(upstream);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url ?? '/', 'http://localhost');
  const path = decodeURIComponent(url.pathname);
  if (base && (path === '/' || path === '')) {
    res.writeHead(302, { Location: `${base}/` });
    res.end();
    return;
  }
  if (path !== base && !path.startsWith(`${base}/`)) {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end(`not under ${base}/`);
    return;
  }
  const rest = path.slice(base.length) || '/';
  if (rest.startsWith('/api/') || rest.startsWith('/events/')) {
    if (api) return proxy(req, res);
    res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ error: { code: 'internal', message: '没有配置 Daemon 地址（--api），接口不可用；看页面请用 npm run build:demo 的产物' } }));
    return;
  }
  const file = normalize(join(dir, rest));
  if (!file.startsWith(dir + sep) && file !== dir) {
    res.writeHead(400);
    res.end();
    return;
  }
  if (rest !== '/' && rest !== '/index.html' && existsSync(file) && statSync(file).isFile()) {
    const immutable = rest.startsWith('/assets/');
    res.writeHead(200, {
      'Content-Type': TYPES[extname(file)] ?? 'application/octet-stream',
      'Cache-Control': immutable ? 'public, max-age=31536000, immutable' : 'no-cache',
      ...(rest === '/mockServiceWorker.js' ? { 'Service-Worker-Allowed': `${base}/` } : {}),
    });
    createReadStream(file).pipe(res);
    return;
  }
  // A page path (or a missing asset under a page path): the SPA answers it.
  if (extname(rest) && rest.startsWith('/assets/')) {
    res.writeHead(404);
    res.end();
    return;
  }
  sendIndex(res);
});

server.listen(port, () => {
  console.log(`serving ${dir} at http://localhost:${port}${base}/${api ? ` (API → ${api})` : ''}`);
});
