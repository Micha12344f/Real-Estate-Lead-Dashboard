// Exercise the actual same-origin dashboard proxy with a local fake backend.
const assert = require('node:assert/strict');
const { test, before, beforeEach, after } = require('node:test');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const path = require('node:path');

let backend;
let frontend;
let backendPort;
let dashboardUrl;
let leadReply;
const calls = [];
const REQUEST_ID = 'b91d6f31-48ad-4ff9-b6be-ae680fed7281';
const UPSTREAM_REQUEST_ID = 'f30fc7d6-5980-4aef-8455-6f74997453d7';
const operationHeaders = {
  'X-TVE-Delete-Operation': 'bd2b6957-231a-4f09-9674-27d08ee8fd78',
  'X-TVE-Delete-Contact-UID': '00000000-0000-0000-0000-000000000007',
  'X-TVE-Delete-Initiated-At': '2026-10-09T11:00:00.000Z',
};
const deleteHeaderNames = Object.keys(operationHeaders);

before(async () => {
  backend = http.createServer((req, res) => {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      calls.push({ method: req.method, url: req.url, auth: req.headers['x-tve-auth'],
        headers: req.headers, rawBody: body, body: body ? JSON.parse(body) : null });
      if (req.url === '/dashboard/leads/7' && leadReply) {
        if (leadReply.disconnect) return req.socket.destroy();
        if (leadReply.getOnly && req.method !== 'GET') {
          res.writeHead(405, { 'Content-Type': 'application/json', Allow: 'GET' });
          return res.end(JSON.stringify({ error: { code: 'method_not_allowed', message: 'Method Not Allowed' } }));
        }
        res.writeHead(leadReply.status, { 'Content-Type': 'application/json', ...leadReply.headers });
        return res.end(leadReply.rawBody ?? JSON.stringify(leadReply.body));
      }
      if (req.url !== '/dashboard/settings/mode' || !['GET', 'PUT'].includes(req.method)) {
        res.writeHead(404, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: { message: 'Unexpected test upstream route' } }));
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ sandbox: req.method === 'PUT' ? JSON.parse(body).sandbox : false,
        mode: req.method === 'PUT' && JSON.parse(body).sandbox ? 'sandbox' : 'production', source: 'environment' }));
    });
  });
  await new Promise(resolve => backend.listen(0, '127.0.0.1', resolve));
  backendPort = backend.address().port;
  const bootstrap = `
    const http = require('node:http');
    const listen = http.Server.prototype.listen;
    http.Server.prototype.listen = function(port, ...args) {
      if (port === 3002) this.once('listening', () => console.log('TEST_DASHBOARD_PORT=' + this.address().port));
      return listen.call(this, 0, ...args);
    };
    require(process.argv[1]);
  `;
  frontend = spawn(process.execPath, ['-e', bootstrap, path.join(__dirname, '../frontend/serve-local.js')], {
    env: { ...process.env, APP_FUNCTION_LOGIC_URL: `http://127.0.0.1:${backendPort}`,
      TVE_WEBHOOK_SECRET: 'proxy-mode-test-secret', APP_LISTEN_HOST: '127.0.0.1',
      APP_DASHBOARD_ONLY: '1', TVE_DEMO_MODE: '0', TVE_PROXY_TEST_NO_LISTEN: '0' },
    stdio: ['ignore', 'pipe', 'ignore'],
  });
  const port = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(new Error('Test dashboard did not bind')), 5000);
    frontend.stdout.on('data', chunk => {
      output += chunk;
      const match = output.match(/TEST_DASHBOARD_PORT=(\d+)/);
      if (match) { clearTimeout(timer); resolve(Number(match[1])); }
    });
    frontend.once('error', error => { clearTimeout(timer); reject(error); });
    frontend.once('exit', () => { clearTimeout(timer); reject(new Error('Test dashboard exited')); });
  });
  dashboardUrl = `http://127.0.0.1:${port}`;
  for (let attempt = 0; attempt < 50; attempt++) {
    try { if ((await fetch(`${dashboardUrl}/healthz`)).ok) return; } catch { /* starting */ }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('Isolated dashboard proxy did not start');
});

beforeEach(() => { calls.length = 0; leadReply = null; });

after(async () => {
  if (frontend && frontend.exitCode === null) { frontend.kill(); await once(frontend, 'exit'); }
  if (backend) await new Promise(resolve => backend.close(resolve));
});

test('dashboard sandbox/production mode GET and PUT proxy to the authenticated backend', async () => {
  const page = await fetch(`${dashboardUrl}/sandbox/`);
  const html = await page.text();
  assert.match(page.headers.get('cache-control') || '', /no-store/);
  assert.match(html, /app\.js\?v=[A-Za-z0-9._-]+/);
  assert.match(html, /delete-recovery\.js\?v=[A-Za-z0-9._-]+/);
  assert.match(html, /styles\.css\?v=[A-Za-z0-9._-]+/);
  const recovery = await fetch(`${dashboardUrl}/sandbox/delete-recovery.js`);
  assert.equal(recovery.status, 200);
  assert.match(recovery.headers.get('cache-control') || '', /no-store/);
  const get = await fetch(`${dashboardUrl}/sandbox/api/settings/mode`);
  assert.equal(get.status, 200);
  assert.equal((await get.json()).mode, 'production');
  const put = await fetch(`${dashboardUrl}/sandbox/api/settings/mode`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sandbox: true }),
  });
  assert.equal(put.status, 200);
  assert.equal((await put.json()).mode, 'sandbox');
  assert.deepEqual(calls.map(({ method, url, auth, body }) => ({ method, url, auth, body })), [
    { method: 'GET', url: '/dashboard/settings/mode', auth: 'proxy-mode-test-secret', body: null },
    { method: 'PUT', url: '/dashboard/settings/mode', auth: 'proxy-mode-test-secret', body: { sandbox: true } },
  ]);
});

function dashboardRequest(resource, options = {}) {
  return fetch(`${dashboardUrl}/sandbox/api/${resource}`, {
    ...options, redirect: 'error', signal: AbortSignal.timeout(5000),
    headers: { Accept: 'application/json', Origin: dashboardUrl, ...options.headers },
  });
}

function assertLeadRequest(method, headers) {
  assert.equal(calls.length, 1, 'Exactly one upstream request; no method fallback or automatic retry');
  const call = calls[0];
  assert.equal(call.method, method);
  assert.equal(call.url, '/dashboard/leads/7');
  assert.equal(call.auth, 'proxy-mode-test-secret');
  assert.equal(call.headers.accept, 'application/json');
  assert.equal(call.rawBody, '');
  assert.equal(call.headers['content-type'], undefined);
  assert.equal(call.headers['x-tve-mode'], ['sandbox', 'production'].includes(headers['X-TVE-Mode'])
    ? headers['X-TVE-Mode'] : undefined);
  for (const name of deleteHeaderNames)
    assert.equal(call.headers[name.toLowerCase()], method === 'DELETE' ? headers[name] : undefined, name);
  assert.match(call.headers['x-request-id'], /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/);
  return call;
}

test('same-origin lead DELETE forwards supported headers and propagates failures', async (t) => {
  const cases = [
    { name: 'sandbox confirmed deletion', headers: { 'X-TVE-Mode': 'sandbox', ...operationHeaders }, status: 200 },
    { name: 'production confirmed deletion', headers: { 'X-TVE-Mode': 'production', ...operationHeaders }, status: 200 },
    { name: 'missing mode', headers: { ...operationHeaders }, status: 409 },
    { name: 'invalid mode', headers: { 'X-TVE-Mode': 'invalid', ...operationHeaders }, status: 409 },
    { name: 'stale sandbox mode', headers: { 'X-TVE-Mode': 'sandbox', ...operationHeaders }, status: 409, mode: 'production' },
    { name: 'missing operation references', headers: { 'X-TVE-Mode': 'sandbox' }, status: 422 },
    { name: 'invalid operation references', headers: { 'X-TVE-Mode': 'sandbox',
      'X-TVE-Delete-Operation': 'not-a-uuid', 'X-TVE-Delete-Contact-UID': 'not-a-contact-uuid',
      'X-TVE-Delete-Initiated-At': 'not-a-date' }, status: 422 },
  ];
  for (const item of cases) await t.test(item.name, async () => {
    const body = item.status === 200 ? { deleted: true, id: 7 }
      : { error: { code: 'request_rejected', message: item.name, requestId: UPSTREAM_REQUEST_ID } };
    const mode = item.mode || (item.headers['X-TVE-Mode'] === 'production' ? 'production' : 'sandbox');
    leadReply = { status: item.status, body, headers: { 'X-TVE-Mode': mode, 'X-Request-ID': UPSTREAM_REQUEST_ID } };
    const response = await dashboardRequest('leads/7', { method: 'DELETE', headers: {
      ...item.headers, 'X-Request-ID': REQUEST_ID.toUpperCase(), 'X-TVE-Auth': 'untrusted-browser-auth',
    } });
    assert.equal(response.status, item.status);
    assert.deepEqual(await response.json(), body);
    assert.match(response.headers.get('cache-control'), /no-store/);
    assert.equal(response.headers.get('x-tve-mode'), mode);
    assert.equal(response.headers.get('x-request-id'), UPSTREAM_REQUEST_ID);
    assert.equal(assertLeadRequest('DELETE', item.headers).headers['x-request-id'], REQUEST_ID);
  });
});

test('stale GET-only upstream returns an honest 405 with no method fallback', async () => {
  leadReply = { getOnly: true, status: 200, body: { id: 7 } };
  const headers = { 'X-TVE-Mode': 'sandbox', ...operationHeaders, 'X-Request-ID': REQUEST_ID };
  const response = await dashboardRequest('leads/7', { method: 'DELETE', headers });
  assert.equal(response.status, 405);
  assert.equal(response.ok, false);
  assert.deepEqual(await response.json(), { error: { code: 'method_not_allowed', message: 'Method Not Allowed' } });
  assertLeadRequest('DELETE', headers);
});

test('non-JSON 405 and dropped upstream delete stay failures', async (t) => {
  await t.test('non-JSON body', async () => {
    leadReply = { status: 405, rawBody: '<html>Method Not Allowed</html>', headers: { 'Content-Type': 'text/html' } };
    const headers = { 'X-TVE-Mode': 'sandbox', ...operationHeaders, 'X-Request-ID': 'bad-id' };
    const response = await dashboardRequest('leads/7', { method: 'DELETE', headers });
    assert.equal(response.status, 405);
    const call = assertLeadRequest('DELETE', headers);
    assert.deepEqual(await response.json(), { error: { code: 'invalid_backend_response',
      message: 'Backend returned an unreadable response.', requestId: call.headers['x-request-id'] } });
  });
  await t.test('disconnected backend', async () => {
    leadReply = { disconnect: true };
    const headers = { 'X-TVE-Mode': 'sandbox', ...operationHeaders, 'X-Request-ID': REQUEST_ID };
    const response = await dashboardRequest('leads/7', { method: 'DELETE', headers });
    assert.equal(response.status, 502);
    assert.equal((await response.json()).error.code, 'backend_unavailable');
    assertLeadRequest('DELETE', headers);
  });
});

test('lead GET omits replay references and unsupported POST stays local', async (t) => {
  await t.test('GET', async () => {
    leadReply = { status: 200, body: { id: 7 } };
    const headers = { 'X-TVE-Mode': 'production', ...operationHeaders };
    const response = await dashboardRequest('leads/7', { headers });
    assert.equal(response.status, 200);
    assertLeadRequest('GET', headers);
  });
  await t.test('POST', async () => {
    const response = await dashboardRequest('leads/7', { method: 'POST' });
    assert.equal(response.status, 405);
    assert.equal((await response.json()).error.code, 'method_not_allowed');
    assert.equal(calls.length, 0);
  });
});
