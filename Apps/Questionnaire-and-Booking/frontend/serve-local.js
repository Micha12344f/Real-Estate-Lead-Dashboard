// Local-only host frontend for the Sandbox. Uses Node built-ins; no dependencies.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const FRONTEND = __dirname;
const BACKEND = process.env.APP_FUNCTION_LOGIC_URL || process.env.SANDBOX_BACKEND_URL || 'http://127.0.0.1:8001';
const WEBHOOK_AUTH = process.env.TVE_WEBHOOK_SECRET || 'sandbox-webhook-secret';
const apps = process.env.APP_DASHBOARD_ONLY === '1' ? [
  { name: 'dashboard', port: 3002, prefix: '/sandbox' },
] : [
  { name: 'booking', port: 3000, prefix: '/booking' },
  { name: 'questionnaire', port: 3001, prefix: '/questionnaire' },
  { name: 'dashboard', port: 3002, prefix: '/sandbox' },
];
const demoMode = process.env.TVE_DEMO_MODE === '1';
// In-memory demo records are process-local: no backend, file, or browser storage writes.
let demoLeads = Array.from({ length: 18 }, (_, index) => {
  const first = ['Amara', 'Chloe', 'Aisha', 'Alice', 'Ben', 'Niamh', 'Sienna', 'Harriet', 'Poppy', 'Declan', 'Noah', 'Daniel', 'Bethany', 'Craig', 'Katie', 'Liam', 'George', 'Rosa'][index];
  const last = ['Winslow', 'Novak', 'Okafor', 'Brewster', 'Porter', 'Mercer', 'Sinclair', 'Sinclair', 'Sutton', 'Rowntree', 'Mercer', 'Faulkner', 'Bennett', 'Barnes', 'Bishop', 'Vickers', 'Duncan', 'Whitfield'][index];
  const now = new Date().toISOString();
  return { id: 773038 + index, first_name: first, last_name: last, email: `${first.toLowerCase()}.${last.toLowerCase()}@example.invalid`,
    property_address: ['114 Bramble Way, Bracknell, RG1 4BI', '51 Heather Gardens, Thatcham, RG2 3PP', '73 Cedar Rise, Caversham, RG30 4BG'][index % 3],
    status: ['enquiried', 'Questionnaire_sent', 'questionnaire_completed', 'viewing_requested'][index % 4], score: 92 - index * 3,
    flags: index % 4 === 0 ? ['unmatched_property'] : [], enquiry: { platform: ['rightmove', 'onthemarket', 'email'][index % 3] },
    created_at: now, updated_at: now };
});
const routes = {
  'booking/availability': { method: 'GET', target: '/webhook/thames-valley-estates/booking-availability', query: true },
  'booking/available-properties': { method: 'GET', target: '/webhook/thames-valley-estates/booking-available-properties', query: true },
  'booking/booking-guest': { method: 'POST', target: '/webhook/thames-valley-estates/booking-guest' },
  'booking/confirm': { method: 'POST', target: '/webhook/thames-valley-estates/booking-confirm' },
  'questionnaire/questionnaire-submit': { method: 'POST', target: '/webhook/thames-valley-estates/questionnaire-submit', questionnaire: true },
  'dashboard/scenarios': { method: 'GET', target: '/sandbox/scenarios' },
  'dashboard/runs': { method: 'GET', target: '/sandbox/runs' },
  'dashboard/leads': { method: 'GET', target: '/dashboard/leads' },
  'dashboard/funnel': { method: 'GET', target: '/dashboard/funnel' },
  'dashboard/metrics': { method: 'GET', target: '/dashboard/metrics' },
  'dashboard/forms': { method: 'GET', target: '/dashboard/forms' },
  'booking/forms-config': { method: 'GET', target: '/webhook/thames-valley-estates/forms-config' },
  'questionnaire/forms-config': { method: 'GET', target: '/webhook/thames-valley-estates/forms-config' },
  'dashboard/settings': { method: 'GET', target: '/dashboard/settings', settings: true },
  'dashboard/settings/mode': { method: 'GET', target: '/dashboard/settings/mode', settings: true },
  'dashboard/settings/sandbox/seed': { method: 'POST', target: '/dashboard/settings/sandbox/seed', settings: true },
  'dashboard/agent/chat': { method: 'POST', target: '/dashboard/agent/chat' },
  'dashboard/properties': { method: 'GET', target: '/webhook/thames-valley-estates/booking-available-properties' },
  'dashboard/actions/assign-property': { method: 'POST', target: '/dashboard/actions/assign-property' },
  'dashboard/actions/resend-invitation': { method: 'POST', target: '/dashboard/actions/resend-invitation' },
  'dashboard/actions/send-booking': { method: 'POST', target: '/dashboard/actions/send-booking' },
  'dashboard/actions/viewing-decision': { method: 'POST', target: '/dashboard/actions/viewing-decision' },
};
const mime = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp', '.woff2': 'font/woff2' };

function json(res, status, value) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(value));
}
function readBody(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', chunk => { data += chunk; if (data.length > 1_000_000) reject(new Error('Request too large')); });
    req.on('end', () => { try { resolve(data ? JSON.parse(data) : {}); } catch { reject(new Error('Invalid JSON')); } });
    req.on('error', reject);
  });
}
function runtimeConfig(app, res) {
  const urls = app.name === 'booking'
    ? { questionnaireSubmitBaseUrl: 'http://localhost:3001/questionnaire/api/questionnaire-submit', bookingBaseUrl: 'http://localhost:3000/booking/api', bookingPrimaryUrl: 'http://localhost:3000/booking/api', bookingFallbackUrl: 'http://localhost:3000/booking/api' }
    : { bookingAppBaseUrl: 'http://localhost:3000/booking', questionnaireSubmitBaseUrl: 'http://localhost:3001/questionnaire/api/questionnaire-submit' };
  json(res, 200, urls);
}
async function api(app, key, req, res, url) {
  const suppliedId = req.headers['x-request-id'];
  const requestId = typeof suppliedId === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(suppliedId) ? suppliedId.toLowerCase() : randomUUID();
  if (key === 'runtime-config') return runtimeConfig(app, res);
  if (demoMode && app.name === 'dashboard' && key === 'leads' && req.method === 'GET') return json(res, 200, { leads: demoLeads, generated_at: new Date().toISOString() });
  if (demoMode && app.name === 'dashboard' && key === 'actions/viewing-decision' && req.method === 'POST') {
    let body;
    try { body = await readBody(req); } catch { return json(res, 400, { error: { message: 'Invalid request.' } }); }
    const lead = demoLeads.find((item) => Number(item.id) === Number(body.contact_id));
    if (!lead) return json(res, 404, { error: { message: 'Lead not found.' } });
    if (!['approve', 'deny'].includes(body.decision)) return json(res, 422, { error: { message: 'Choose approve or deny.' } });
    if (lead.status !== 'viewing_requested') return json(res, 422, { error: { message: 'This lead no longer has a pending viewing request.' } });
    lead.status = body.decision === 'approve' ? 'booked' : 'questionnaire_completed';
    lead.updated_at = new Date().toISOString();
    return json(res, 200, lead);
  }
  if (demoMode && app.name === 'dashboard' && /^leads\/[0-9]+$/.test(key)) {
    const id = Number(key.split('/')[1]);
    if (req.method === 'DELETE') {
      const existing = demoLeads.length;
      demoLeads = demoLeads.filter((lead) => Number(lead.id) !== id);
      return existing === demoLeads.length ? json(res, 404, { error: { message: 'Lead not found.' } }) : json(res, 200, { deleted: true, id });
    }
    if (req.method === 'GET') {
      const lead = demoLeads.find((item) => Number(item.id) === id);
      return lead ? json(res, 200, lead) : json(res, 404, { error: { message: 'Lead not found.' } });
    }
  }
   let route = routes[`${app.name}/${key}`];
   if (app.name === 'dashboard' && key === 'forms' && req.method === 'PUT') route = { method: 'PUT', target: '/dashboard/forms' };
   if (app.name === 'dashboard' && key === 'runs' && req.method === 'POST') route = { method: 'POST', target: '/sandbox/runs' };
   if (app.name === 'dashboard' && /^runs\/[0-9a-f-]{36}\/(replay|remind|retry)$/.test(key)) route = { method: 'POST', target: `/sandbox/${key}` };
  if (app.name === 'dashboard' && /^leads\/[0-9]+$/.test(key)) route = { method: req.method === 'DELETE' ? 'DELETE' : 'GET', target: `/dashboard/${key}` };
  if (app.name === 'dashboard' && /^properties\/[0-9]+\/contacts$/.test(key)) route = { method: 'GET', target: `/dashboard/${key}` };
   if (app.name === 'dashboard' && /^settings\/[A-Z][A-Z0-9_]*$/.test(key)) route = { method: req.method === 'DELETE' ? 'DELETE' : 'PUT', methods: ['PUT', 'DELETE'], target: `/dashboard/${key}`, settings: true };
   if (app.name === 'dashboard' && key === 'settings/mode' && req.method === 'PUT') route = { method: 'PUT', target: '/dashboard/settings/mode', settings: true };
  if (app.name === 'dashboard' && /^settings\/test\/(acquaint|imap|smtp)$/.test(key)) route = { method: 'POST', target: `/dashboard/${key}`, settings: true };
  if (!route) { res.setHeader('X-Request-ID', requestId); return json(res, 404, { error: { code: 'not_found', message: 'Resource not found.', requestId } }); }
  const allowedMethods = route.methods || [route.method];
  if (req.method === 'OPTIONS') { res.writeHead(204, { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Methods': `${allowedMethods.join(', ')}, OPTIONS`, 'Access-Control-Allow-Headers': 'Content-Type' }); return res.end(); }
  if (!allowedMethods.includes(req.method)) { res.setHeader('X-Request-ID', requestId); return json(res, 405, { error: { code: 'method_not_allowed', message: 'Method not allowed.', requestId } }); }
  let body = {};
  try { if (req.method !== 'GET') body = await readBody(req); }
  catch (err) { res.setHeader('X-Request-ID', requestId); return json(res, err.message === 'Invalid JSON' ? 400 : 413, { error: { code: err.message === 'Invalid JSON' ? 'invalid_json' : 'request_too_large', message: 'Request could not be read.', requestId } }); }
  if (route.questionnaire) body = body.payload || {};
  const target = new URL(route.target, BACKEND);
  if (target.origin !== BACKEND) return json(res, 500, { status: 'error', message: 'Sandbox proxy target rejected.' });
  if (route.query) {
     for (const [k, v] of url.searchParams) if (['token', 'propertyId', 'contactId', 'statusId', 'status'].includes(k)) target.searchParams.set(k, v);
  }
  try {
    const hasBody = route.method === 'POST' || route.method === 'PUT';
    const expectedMode = req.headers['x-tve-mode'];
    const modeHeader = ['sandbox', 'production'].includes(expectedMode) ? { 'X-TVE-Mode': expectedMode } : {};
    const operationHeader = app.name === 'dashboard' && /^leads\/[0-9]+$/.test(key) && req.method === 'DELETE'
      ? { ...(typeof req.headers['x-tve-delete-operation'] === 'string' ? { 'X-TVE-Delete-Operation': req.headers['x-tve-delete-operation'] } : {}),
          ...(typeof req.headers['x-tve-delete-contact-uid'] === 'string' ? { 'X-TVE-Delete-Contact-UID': req.headers['x-tve-delete-contact-uid'] } : {}),
          ...(typeof req.headers['x-tve-delete-initiated-at'] === 'string' ? { 'X-TVE-Delete-Initiated-At': req.headers['x-tve-delete-initiated-at'] } : {}) } : {};
    const response = await fetch(target, { method: route.method, headers: { Accept: 'application/json', 'X-TVE-Auth': WEBHOOK_AUTH, 'X-Request-ID': requestId, ...modeHeader, ...operationHeader, ...(hasBody ? { 'Content-Type': 'application/json' } : {}) }, ...(hasBody ? { body: JSON.stringify(body) } : {}) });
    const text = await response.text();
    let safeBody;
    try { safeBody = JSON.parse(text); } catch { safeBody = { error: { code: 'invalid_backend_response', message: 'Backend returned an unreadable response.', requestId } }; }
    // Validation detail can include submitted credentials. Never relay input-bearing errors.
    if (route.settings && !response.ok) {
      const messages = { 401: 'Settings access was denied.', 403: 'Settings access was denied.', 404: 'Settings resource not found.', 409: 'This operation is unavailable in the current mode. Shared production settings are read-only in Sandbox.', 422: 'Setting validation failed. Check the field format and connection details.', 503: 'Settings or sandbox storage is unavailable. Check the backend vault and sandbox database configuration.' };
      safeBody = { error: { code: 'settings_request_failed', message: messages[response.status] || 'Settings request failed. Check the backend connection and configuration.', requestId } };
    }
    res.writeHead(response.status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*', 'X-Request-ID': response.headers.get('x-request-id') || requestId,
      ...(response.headers.get('x-tve-mode') ? { 'X-TVE-Mode': response.headers.get('x-tve-mode') } : {}) });
    res.end(JSON.stringify(safeBody));
  } catch { res.setHeader('X-Request-ID', requestId); json(res, 502, { error: { code: 'backend_unavailable', message: 'Sandbox backend is unavailable.', requestId } }); }
}
function handler(app) {
  return async (req, res) => {
    const url = new URL(req.url, `http://127.0.0.1:${app.port}`);
    if (url.pathname === '/healthz') return json(res, 200, { status: 'ok' });
    const prefix = app.prefix;
    const localPath = url.pathname.startsWith(prefix + '/') ? url.pathname.slice(prefix.length) : url.pathname;
    const apiMatch = localPath.match(/^\/api\/(.+)$/);
    if (apiMatch) return api(app, apiMatch[1], req, res, url);
    const tokenizedPath = ['questionnaire', 'booking'].includes(app.name) && /^\/(?:sbx_[a-f0-9]{64}|[A-Za-z0-9_-]{43}|[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{43})\/?$/.test(localPath);
    let relative = localPath === '/' || localPath === app.prefix || tokenizedPath ? 'index.html' : localPath.replace(/^\/+/, '');
    // Each copied app retains its original absolute asset prefix in HTML/CSS.
    if (relative.startsWith(`${app.name}/`)) relative = relative.slice(app.name.length + 1);
    if (app.name === 'dashboard' && ['views.html', 'issues.html', 'inbox.html', 'home.html', 'my-issues.html', 'index.html'].includes(relative)) relative = 'index.html';
    const root = path.resolve(FRONTEND, app.name);
    if (relative.split('/').some(part => part.startsWith('.')) || relative.startsWith('api/') || !['index.html', 'country-codes.js', 'dashboard.js', 'dashboard.css', 'app.js', 'forms.js', 'forms-defaults.js', 'forms.css', 'delete-recovery.js', 'styles.css'].includes(relative) && !relative.startsWith('assets/')) return json(res, 404, {status:'error',message:'Not found'});
    const file = path.resolve(root, relative);
    if (file !== root && !file.startsWith(root + path.sep)) return json(res, 403, { status: 'error', message: 'Forbidden' });
    fs.stat(file, (err, stat) => {
      const target = !err && stat.isDirectory() ? path.join(file, 'index.html') : file;
      fs.readFile(target, (readErr, data) => {
        if (readErr) return json(res, 404, { status: 'error', message: 'Not found' });
        const liveDashboardAsset = app.name === 'dashboard' && ['index.html', 'app.js', 'forms.js', 'forms-defaults.js', 'forms.css', 'delete-recovery.js', 'styles.css'].includes(relative);
        res.writeHead(200, {
          'Content-Type': mime[path.extname(target).toLowerCase()] || 'application/octet-stream',
          'X-Content-Type-Options': 'nosniff',
          ...(liveDashboardAsset ? { 'Cache-Control': 'no-store' } : {}),
        });
        res.end(data);
      });
    });
  };
}
// Historically this module starts when required (also used by the isolated
// proxy integration harness). Only unit tests suppress listeners explicitly.
if (process.env.TVE_PROXY_TEST_NO_LISTEN !== '1') {
  for (const app of apps) http.createServer(handler(app)).listen(app.port, process.env.APP_LISTEN_HOST || process.env.SANDBOX_LISTEN_HOST || '127.0.0.1', () => console.log(`${app.name} frontend listening on http://127.0.0.1:${app.port}`));
}
module.exports = { api };
