import assert from 'node:assert/strict';
import { once } from 'node:events';
import { test, before, after } from 'node:test';
import { createApp } from '../src/server.ts';

let server, base;
before(async () => {
  server = createApp(); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  base = `http://127.0.0.1:${server.address().port}`;
});
after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));

test('health endpoint identifies the new foundation', async () => {
  const result = await fetch(`${base}/health`);
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), { project: 'qmg-mercury', version: '0.1.0', status: 'foundation-ready' });
});
test('startup page is served with Chinese text and a content policy', async () => {
  const result = await fetch(base);
  assert.equal(result.status, 200);
  assert.match(result.headers.get('content-type'), /text\/html/);
  assert.match(result.headers.get('content-security-policy'), /default-src 'none'/);
  assert.match(await result.text(), /游戏功能尚未接入/);
});
test('private archive, source files and encoded traversal are never served', async () => {
  for (const route of ['/legacy/2026-11-19/package.json', '/resources/upstream/forge/LICENSE', '/src/server.ts', '/.git/config', '/%2e%2e%2flegacy/README.md']) {
    const result = await fetch(base + route);
    assert.equal(result.status, 404, route);
  }
});
test('the local server does not accept writes', async () => {
  const result = await fetch(`${base}/health`, { method: 'POST', body: 'test' });
  assert.equal(result.status, 405);
  assert.equal(result.headers.get('allow'), 'GET');
});
