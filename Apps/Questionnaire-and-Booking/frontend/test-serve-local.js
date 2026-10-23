const test = require('node:test');
const assert = require('node:assert/strict');
const { Readable } = require('node:stream');
process.env.TVE_PROXY_TEST_NO_LISTEN = '1';
const { api } = require('./serve-local');

function request(method, headers, body) {
  const stream = Readable.from(body === undefined ? [] : [JSON.stringify(body)]);
  stream.method = method;
  stream.headers = headers;
  return stream;
}

function response() {
  return {
    status: null, payload: null, headers: {},
    setHeader(key, value) { this.headers[key] = value; },
    writeHead(status, headers) { this.status = status; Object.assign(this.headers, headers); },
    end(data) { this.payload = data && JSON.parse(data); },
  };
}

test('proxy keeps standard auth and replay references on dashboard lead DELETE only', async () => {
  const seen = [];
  const original = global.fetch;
  global.fetch = async (target, options) => {
    seen.push({ target: target.pathname, ...options });
    return { status: 200, headers: { get: () => null }, text: async () => '{"deleted":true}' };
  };
  try {
    const app = { name: 'dashboard' };
    const url = new URL('http://localhost/sandbox/api/leads/7');
    const headers = { 'x-tve-mode': 'production',
      'x-tve-delete-operation': 'bd2b6957-231a-4f09-9674-27d08ee8fd78',
      'x-tve-delete-contact-uid': '00000000-0000-0000-0000-000000000007',
      'x-tve-delete-initiated-at': '2026-10-09T11:00:00.000Z' };
    const result = response();
    await api(app, 'leads/7', request('DELETE', headers), result, url);
    assert.equal(result.status, 200);
    assert.equal(seen[0].target, '/dashboard/leads/7');
    assert.equal(seen[0].headers['X-TVE-Mode'], 'production');
    assert.equal(seen[0].headers['X-TVE-Delete-Operation'], headers['x-tve-delete-operation']);
    assert.equal(seen[0].headers['X-TVE-Delete-Contact-UID'], headers['x-tve-delete-contact-uid']);
    assert.equal(seen[0].headers['X-TVE-Delete-Initiated-At'], headers['x-tve-delete-initiated-at']);
    assert.ok(seen[0].headers['X-TVE-Auth']);

    await api(app, 'leads/7', request('GET', headers), response(), url);
    assert.equal(seen[1].headers['X-TVE-Delete-Operation'], undefined);
    await api(app, 'leads/7', request('DELETE', { 'x-tve-mode': 'production' }), response(), url);
    assert.equal(seen[2].headers['X-TVE-Delete-Operation'], undefined);
    assert.ok(seen.every(item => item.headers['X-TVE-Auth']));
  } finally {
    global.fetch = original;
  }
});

test('proxy forwards resend and viewing actions with production mode and auth', async () => {
  const original = global.fetch;
  const seen = [];
  global.fetch = async (target, options) => {
    seen.push({ target: target.pathname, ...options });
    return { status: 200, headers: { get: () => null }, text: async () => '{"sent":true}' };
  };
  try {
    for (const action of ['resend-invitation', 'send-booking', 'viewing-decision']) {
      const result = response();
      const body = { contact_id: 7, ...(action === 'viewing-decision' ? { decision: 'approve' } : {}) };
      await api({ name: 'dashboard' }, `actions/${action}`, request('POST', { 'x-tve-mode': 'production' }, body),
        result, new URL(`http://localhost/sandbox/api/actions/${action}`));
      assert.equal(result.status, 200);
      const call = seen.at(-1);
      assert.equal(call.target, `/dashboard/actions/${action}`);
      assert.equal(call.method, 'POST');
      assert.equal(call.headers['X-TVE-Mode'], 'production');
      assert.ok(call.headers['X-TVE-Auth']);
      assert.deepEqual(JSON.parse(call.body), body);
    }
  } finally { global.fetch = original; }
});

test('proxy preserves Acquaint validation errors for the dashboard', async () => {
  const original = global.fetch;
  global.fetch = async () => ({
    status: 422,
    headers: { get: () => null },
    text: async () => JSON.stringify({ detail: 'Acquaint rejected the viewing details. Check the applicant contact.' }),
  });
  try {
    const result = response();
    await api({ name: 'dashboard' }, 'actions/viewing-decision',
      request('POST', { 'x-tve-mode': 'production' }, { contact_id: 7, decision: 'approve' }),
      result, new URL('http://localhost/sandbox/api/actions/viewing-decision'));
    assert.equal(result.status, 422);
    assert.equal(result.payload.detail, 'Acquaint rejected the viewing details. Check the applicant contact.');
  } finally { global.fetch = original; }
});

test('demo deletion changes only process-local demo leads, without backend fetch', async () => {
  const key = require.resolve('./serve-local');
  const originalFetch = global.fetch;
  process.env.TVE_DEMO_MODE = '1';
  delete require.cache[key];
  const demo = require('./serve-local');
  global.fetch = () => { throw new Error('demo must not reach backend'); };
  try {
    const app = { name: 'dashboard' };
    const url = new URL('http://localhost/sandbox/api/leads/773038');
    const deleted = response();
    await demo.api(app, 'leads/773038', request('DELETE', {}), deleted, url);
    assert.deepEqual(deleted.payload, { deleted: true, id: 773038 });
    const list = response();
    await demo.api(app, 'leads', request('GET', {}), list, url);
    assert.equal(list.payload.leads.length, 17);
    assert.ok(!list.payload.leads.some(lead => lead.id === 773038));
  } finally {
    global.fetch = originalFetch;
    delete process.env.TVE_DEMO_MODE;
    delete require.cache[key];
  }
});

test('demo viewing decision updates a pending request locally and rejects replay', async () => {
  const key = require.resolve('./serve-local');
  const originalFetch = global.fetch;
  process.env.TVE_DEMO_MODE = '1';
  delete require.cache[key];
  const demo = require('./serve-local');
  global.fetch = () => { throw new Error('demo must not reach backend'); };
  try {
    const app = { name: 'dashboard' };
    const url = new URL('http://localhost/sandbox/api/actions/viewing-decision');
    const approved = response();
    await demo.api(app, 'actions/viewing-decision', request('POST', {}, { contact_id: 773041, decision: 'approve' }), approved, url);
    assert.equal(approved.status, 200);
    assert.equal(approved.payload.status, 'booked');

    const replay = response();
    await demo.api(app, 'actions/viewing-decision', request('POST', {}, { contact_id: 773041, decision: 'deny' }), replay, url);
    assert.equal(replay.status, 422);
  } finally {
    global.fetch = originalFetch;
    delete process.env.TVE_DEMO_MODE;
    delete require.cache[key];
  }
});

test('forms save uses authenticated PUT and applicant configuration is read-only', async () => {
  const original = global.fetch;
  const seen = [];
  global.fetch = async (url, options) => {
    seen.push({ url: String(url), ...options });
    return { status: 200, ok: true, headers: new Headers(), text: async () => JSON.stringify({ logo_url: '', questions: [] }) };
  };
  try {
    const saved = response();
    await api({ name: 'dashboard' }, 'forms', request('PUT', { 'x-tve-mode': 'sandbox' }, { logo_url: '', questions: [] }), saved, new URL('http://localhost/sandbox/api/forms'));
    assert.equal(saved.status, 200);
    assert.equal(seen[0].method, 'PUT');
    assert.equal(seen[0].headers['X-TVE-Mode'], 'sandbox');
    assert.ok(seen[0].headers['X-TVE-Auth']);
    assert.equal(new URL(seen[0].url).pathname, '/dashboard/forms');
    for (const name of ['booking', 'questionnaire']) {
      const read = response();
      await api({ name }, 'forms-config', request('GET', {}), read, new URL('http://localhost/api/forms-config'));
      assert.equal(read.status, 200);
      assert.equal(new URL(seen.at(-1).url).pathname, '/webhook/thames-valley-estates/forms-config');
      const write = response();
      await api({ name }, 'forms-config', request('PUT', {}, {}), write, new URL('http://localhost/api/forms-config'));
      assert.equal(write.status, 405);
    }
  } finally { global.fetch = original; }
});
