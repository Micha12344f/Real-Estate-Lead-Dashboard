// Production proxy contract checks. All fetches are intercepted; no HTTP is sent.
const assert = require('node:assert/strict');
const { test, beforeEach, afterEach } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const frontend = path.join(__dirname, '../frontend');
const availability = require('../frontend/booking/api/availability.js');
const confirm = require('../frontend/booking/api/confirm.js');
const questionnaire = require('../frontend/questionnaire/api/questionnaire-submit.js');
const properties = require('../frontend/booking/api/available-properties.js');
const guest = require('../frontend/booking/api/booking-guest.js');
const originalFetch = global.fetch;
const originalEnv = { ...process.env };

beforeEach(() => {
  global.fetch = async () => { throw new Error('Unmocked HTTP request blocked'); };
  process.env.BOOKING_BASE_URL = 'https://backend.example.test/webhook/';
  process.env.QUESTIONNAIRE_SUBMIT_BASE_URL = 'https://backend.example.test/webhook/thames-valley-estates/questionnaire-submit';
  process.env.X_TVE_API_KEY = 'proxy-test-key';
});
afterEach(() => {
  global.fetch = originalFetch;
  for (const key of ['BOOKING_BASE_URL', 'QUESTIONNAIRE_SUBMIT_BASE_URL', 'X_TVE_API_KEY']) {
    if (originalEnv[key] === undefined) delete process.env[key];
    else process.env[key] = originalEnv[key];
  }
});

function response() {
  return { code: null, payload: null, setHeader() {},
    status(code) { this.code = code; return this; },
    json(payload) { this.payload = payload; return this; }, end() {} };
}
function intercept(payload = { status: 'ok' }, status = 200) {
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url: String(url), ...options });
    return { status, text: async () => JSON.stringify(payload) };
  };
  return calls;
}

test('questionnaire wrapper preserves contact token, answers and consent', async () => {
  const calls = intercept({ status: 'ok', bookingUrl: 'https://booking.example.test/booking/new-booking-token' });
  const payload = { token: 'opaque.signature', answers: { occupants: 2 }, consent: true };
  const res = response();
  await questionnaire({ method: 'POST', body: { payload } }, res);
  assert.equal(calls[0].url, process.env.QUESTIONNAIRE_SUBMIT_BASE_URL);
  assert.deepEqual(JSON.parse(calls[0].body), payload);
  assert.equal(calls[0].headers['X-TVE-Auth'], 'proxy-test-key');
  assert.equal(res.code, 200);
  assert.match(res.payload.bookingUrl, /new-booking-token$/);
});

test('availability forwards token and property binding to the configured webhook', async () => {
  const calls = intercept();
  await availability({ method: 'GET', query: { token: 'opaque.signature', propertyId: '242', contactId: '7' } }, response());
  const url = new URL(calls[0].url);
  assert.equal(url.pathname, '/webhook/thames-valley-estates/booking-availability');
  assert.equal(url.searchParams.get('token'), 'opaque.signature');
  assert.equal(url.searchParams.get('propertyId'), '242');
  assert.equal(url.searchParams.get('contactId'), '7');
  assert.equal(calls[0].headers['X-TVE-Auth'], 'proxy-test-key');
});

test('confirm places a query-only token into the forwarded POST body', async () => {
  const calls = intercept({ status: 'acknowledged', localMock: false });
  const res = response();
  await confirm({ method: 'POST', query: { token: ' opaque.signature ' }, body: { propertyId: 242, slotStart: '2026-10-10T12:00:00Z' } }, res);
  assert.equal(calls[0].url, 'https://backend.example.test/webhook/thames-valley-estates/booking-confirm');
  assert.equal(JSON.parse(calls[0].body).token, 'opaque.signature');
  assert.equal(res.payload.status, 'acknowledged');
  assert.equal(res.payload.localMock, false);
});

test('proxy preserves token rejection HTTP status and backend error body', async () => {
  const payload = { error: { code: 'invalid_token', message: 'Invalid or expired journey token' } };
  intercept(payload, 401);
  const res = response();
  await confirm({ method: 'POST', query: {}, body: { token: 'expired.signature' } }, res);
  assert.equal(res.code, 401);
  assert.deepEqual(res.payload, payload);
});

test('property proxy translates both applicant request methods to backend GET', async () => {
  const payload = { status: 'ok', properties: [{ id: 242 }] };
  const calls = intercept(payload);
  for (const method of ['GET', 'POST']) {
    const res = response();
    await properties({ method }, res);
    assert.equal(res.code, 200);
    assert.deepEqual(res.payload, payload);
  }
  assert.equal(calls.length, 2);
  for (const call of calls) {
    assert.equal(call.url, 'https://backend.example.test/webhook/thames-valley-estates/booking-available-properties');
    assert.equal(call.method, 'GET');
    assert.equal(call.body, undefined);
    assert.equal(call.headers['X-TVE-Auth'], 'proxy-test-key');
  }
});

test('property proxy rejects unsupported methods without contacting the backend', async () => {
  const calls = intercept();
  const res = response();
  await properties({ method: 'DELETE' }, res);
  assert.equal(res.code, 405);
  assert.equal(calls.length, 0);
});

test('booking proxies require an explicit backend instead of defaulting to n8n', async () => {
  delete process.env.BOOKING_BASE_URL;
  const calls = intercept();
  for (const handler of [availability, confirm, properties, guest]) {
    const res = response();
    await handler({ method: handler === availability ? 'GET' : 'POST', query: { token: 'opaque.signature' }, body: { token: 'opaque.signature' } }, res);
    assert.equal(res.code, 503);
  }
  assert.equal(calls.length, 0);
});

test('Vercel resolves prefixed APIs before tokenized applicant page rewrites', () => {
  for (const app of ['booking', 'questionnaire']) {
    const { rewrites } = JSON.parse(fs.readFileSync(path.join(frontend, app, 'vercel.json'), 'utf8'));
    const api = rewrites.findIndex(rule => rule.source === `/${app}/api/:path*`);
    const page = rewrites.findIndex(rule => rule.source === `/${app}/:path*`);
    assert.ok(api >= 0 && page > api);
    assert.equal(rewrites[api].destination, '/api/:path*');
    assert.equal(rewrites[page].destination, '/index.html');
  }
});

test('booking serves its root-level country-codes asset before the HTML catch-all', () => {
  const { rewrites } = JSON.parse(fs.readFileSync(path.join(frontend, 'booking', 'vercel.json'), 'utf8'));
  const asset = rewrites.findIndex(rule => rule.source === '/booking/country-codes.js');
  const root = rewrites.findIndex(rule => rule.source === '/booking');
  const page = rewrites.findIndex(rule => rule.source === '/booking/:path*');
  assert.ok(asset >= 0, 'country-codes.js rewrite is present');
  assert.equal(rewrites[asset].destination, '/country-codes.js');
  assert.ok(root > asset && page > asset, 'asset rewrite precedes both HTML catch-alls');
});
