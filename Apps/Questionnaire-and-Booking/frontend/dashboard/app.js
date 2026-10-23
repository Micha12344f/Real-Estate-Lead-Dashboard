'use strict';
/* ============================================================================
   TVE Lead Intelligence — dashboard SPA (vanilla JS, no build step)
   Served at http://127.0.0.1:3002 by frontend/serve-local.js (prefix /sandbox).
   All API calls go through the same-origin proxy: /sandbox/api/<key>.
   ========================================================================== */

/* ═══ 1. Constants ═════════════════════════════════════════════════════════ */

const sandboxPath = location.pathname.includes('/sandbox/')
  ? location.pathname.slice(0, location.pathname.indexOf('/sandbox/') + '/sandbox/'.length)
  : '/sandbox/';
const API_BASE = `${sandboxPath}api/`;
const STORAGE_KEY = 'tve-starred';
const AGENT_MODEL_KEY = 'tve-agent-model';
const SIDEBAR_KEY = 'tve-sidebar-collapsed';
const AGENT_MODELS = ['qwen3.5:27b', 'qwen3:14b', 'qwen3.6:latest'];
const POLL_LEADS_MS = 5000;
const POLL_OVERVIEW_MS = 10000;
const PAGES = ['overview', 'inbox', 'leads', 'views', 'myleads', 'properties', 'forms', 'agent', 'settings'];
const PAGE_TITLES = {
  overview: 'Overview', inbox: 'Inbox', leads: 'Leads',
  views: 'Views', myleads: 'My leads', properties: 'Properties', forms: 'Forms', agent: 'Agent', settings: 'Settings',
};
const STAGES = ['enquiried', 'Questionnaire_sent', 'questionnaire_completed', 'booking_link_sent', 'viewing_requested', 'booked', 'done'];
const STAGE_META = {
  enquiried: { label: 'Enquiried', tone: 'grey' },
  Questionnaire_sent: { label: 'Questionnaire sent', tone: 'blue' },
  questionnaire_completed: { label: 'Questionnaire completed', tone: 'violet' },
  booking_link_sent: { label: 'Booking link sent', tone: 'blue' },
  viewing_requested: { label: 'Viewing requested', tone: 'amber' },
  booked: { label: 'Booked', tone: 'teal' },
  done: { label: 'Done', tone: 'green' },
  unmatched_enquiry: { label: 'Needs review', tone: 'red' },
};
const UNMATCHED_STATUS = 'unmatched_enquiry';
const STAGE_SVGS = [
  // 0 Enquiried — empty ring (0% fill)
  '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3.14 0" stroke-dashoffset="-0.7"/><circle cx="7" cy="7" r="2" fill="none" stroke="currentColor" stroke-width="4" stroke-dasharray="11.3097 22.6195" stroke-dashoffset="11.3097" transform="rotate(-90 7 7)"/></svg>',
  // 1 Questionnaire sent — ~20% fill
  '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3.14 0" stroke-dashoffset="-0.7"/><circle cx="7" cy="7" r="2" fill="none" stroke="currentColor" stroke-width="4" stroke-dasharray="11.3097 22.6195" stroke-dashoffset="9.0478" transform="rotate(-90 7 7)"/></svg>',
  // 2 Questionnaire completed — ~40% fill
  '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3.14 0" stroke-dashoffset="-0.7"/><circle cx="7" cy="7" r="2" fill="none" stroke="currentColor" stroke-width="4" stroke-dasharray="11.3097 22.6195" stroke-dashoffset="6.7858" transform="rotate(-90 7 7)"/></svg>',
  // Booking link sent — envelope
  '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="1" y="2.5" width="12" height="9" rx="1.5" stroke="currentColor"/><path d="m1.5 3 5.5 4 5.5-4" stroke="currentColor"/></svg>',
  // Viewing requested — ~60% fill
  '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3.14 0" stroke-dashoffset="-0.7"/><circle cx="7" cy="7" r="2" fill="none" stroke="currentColor" stroke-width="4" stroke-dasharray="11.3097 22.6195" stroke-dashoffset="4.5239" transform="rotate(-90 7 7)"/></svg>',
  // 4 Booked — ~80% fill
  '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3.14 0" stroke-dashoffset="-0.7"/><circle cx="7" cy="7" r="2" fill="none" stroke="currentColor" stroke-width="4" stroke-dasharray="11.3097 22.6195" stroke-dashoffset="2.2619" transform="rotate(-90 7 7)"/></svg>',
  // 5 Done — 100% fill + checkmark cutout (exactly like Linear's Done)
  '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-dasharray="3.14 0" stroke-dashoffset="-0.7"/><circle cx="7" cy="7" r="3" fill="none" stroke="currentColor" stroke-width="6" stroke-dasharray="18.8496 37.6991" stroke-dashoffset="0" transform="rotate(-90 7 7)"/><path stroke="none" fill="var(--panel)" d="M10.951 4.24896C11.283 4.58091 11.283 5.11909 10.951 5.45104L5.95104 10.451C5.61909 10.783 5.0809 10.783 4.74896 10.451L2.74896 8.45104C2.41701 8.11909 2.41701 7.5809 2.74896 7.24896C3.0809 6.91701 3.61909 6.91701 3.95104 7.24896L5.35 8.64792L9.74896 4.24896C10.0809 3.91701 10.6191 3.91701 10.951 4.24896Z"/></svg>',
];
const FLAGS = {
  unmatched_property: { label: 'No property', tone: 'red', tip: 'No property matched' },
  unmatched_enquiry: { label: 'Needs review', tone: 'red', tip: 'Enquiry without a matched property' },
  stalled_questionnaire: { label: 'Stalled questionnaire', tone: 'amber', tip: 'Awaiting questionnaire >3d' },
  stalled_enquiried: { label: 'Invitation unsent', tone: 'amber', tip: 'Invitation unsent >24h' },
  expired_questionnaire_token: { label: 'Expired link', tone: 'amber', tip: 'Questionnaire link expired' },
  budget_mismatch: { label: 'Budget mismatch', tone: 'amber', tip: 'Budget below rent' },
  affordability_risk: { label: 'Affordability risk', tone: 'amber', tip: 'Income below affordability rule' },
  adverse_credit: { label: 'Adverse credit', tone: 'amber', tip: 'Adverse credit' },
  no_income_proof: { label: 'No income proof', tone: 'amber', tip: 'No income proof' },
  opted_out: { label: 'Opted out', tone: 'grey', tip: 'Booking opted out' },
  unresponsive: { label: 'Unresponsive', tone: 'red', tip: 'Unresponsive >7d' },
};
const NEEDS_REVIEW_FLAGS = ['unmatched_enquiry', 'unmatched_property', 'stalled_questionnaire', 'stalled_enquiried', 'expired_questionnaire_token', 'unresponsive'];
// Platform chip tints, keyed by the platform name with non-letters stripped.
// Unmapped platforms (e.g. "email") keep the default blue chip.
const PLATFORM_CLASSES = {
  rightmove: 'p-rightmove',
  onthemarket: 'p-onthemarket',
  zoopla: 'p-zoopla',
};
const QUESTIONNAIRE_FIELDS = [
  ['move_timing', 'Move timing', 'text'],
  ['monthly_budget', 'Monthly budget', 'text'],
  ['rental_duration', 'Rental duration', 'text'],
  ['household_income', 'Household income', 'text'],
  ['income_proof', 'Income proof', 'text'],
  ['occupants', 'Occupants', 'text'],
  ['pets', 'Pets', 'text'],
  ['smoke_vape', 'Smoke / vape', 'text'],
  ['adverse_credit', 'Adverse credit', 'text'],
  ['booking_opt_in', 'Booking opt-in', 'optin'],
  ['completed_at', 'Completed at', 'time'],
];
const ENQUIRY_FIELDS = [
  ['platform', 'Platform', 'text'],
  ['property_reference', 'Property reference', 'text'],
  ['move_in_date', 'Move-in date', 'text'],
  ['duration', 'Duration', 'text'],
  ['moving_with', 'Moving with', 'text'],
  ['pets', 'Pets', 'text'],
  ['smokers', 'Smokers', 'text'],
  ['employment', 'Employment', 'text'],
  ['affordability', 'Affordability', 'text'],
  ['adverse_credit', 'Adverse credit', 'text'],
  ['received_at', 'Received at', 'time'],
];

/* ═══ 2. State ═════════════════════════════════════════════════════════════ */

const state = {
  route: { page: 'overview', params: new URLSearchParams() },
  leads: [], leadsLoaded: false, leadsGeneratedAt: null,
  metrics: null, funnel: null, overviewAt: 0, funnelError: null,
  properties: [], propertiesAt: null, propertiesError: null,
  mode: null, starred: new Set(),
  search: '', propertySearch: '', stageFilter: 'all', sort: 'updated', dailyRange: 30,
  online: null, lastError: null, lastUpdatedAt: null,
  drawerId: null, drawerLead: null,
  refreshPage: null, pageSearchInput: null,
  agentMessages: [], agentBusy: false, agentDraft: '', agentStatsAt: 0,
  settingsCleanup: null, reloadSettings: null,
};

/* ═══ 3. Utilities ════════════════════════════════════════════════════════ */

const byId = (id) => document.getElementById(id);

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value == null) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (value === false) continue;
      else if (value === true) node.setAttribute(key, '');
      else node.setAttribute(key, value);
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false || child === true) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function loadStarred(mode = 'production') {
  try {
    const raw = JSON.parse(localStorage.getItem(`${STORAGE_KEY}:${mode}`) || (mode === 'production' ? localStorage.getItem(STORAGE_KEY) : null));
    return new Set(Array.isArray(raw) ? raw.map(String) : []);
  } catch { return new Set(); }
}
function persistStarred() {
  try { localStorage.setItem(`${STORAGE_KEY}:${state.mode || 'production'}`, JSON.stringify([...state.starred])); } catch { /* private mode */ }
}

function labelize(value) { return String(value || '').replaceAll('_', ' '); }
function capitalize(value) { const s = String(value || ''); return s ? s[0].toUpperCase() + s.slice(1) : s; }
function fmtNum(value) { return Number(value).toLocaleString('en-GB'); }
function pct(value) { return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value * 100)}%` : '—'; }
function fmtHours(value) { return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)}h` : '—'; }
function hhmmss(date) { return date.toLocaleTimeString('en-GB', { hour12: false }); }
function fullTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return String(iso);
  return d.toLocaleString('en-GB', { timeZone: 'Europe/London', dateStyle: 'medium', timeStyle: 'short' });
}
function relTime(iso) {
  if (!iso) return '—';
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 10) return 'just now';
  if (s < 60) return `${Math.floor(s)}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 30 * 86400) return `${Math.floor(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString('en-GB');
}
function ageText(lead) {
  const days = Number(lead.age_days);
  if (Number.isFinite(days) && days >= 0) {
    if (days < 1) return `${Math.max(1, Math.round(days * 24))}h`;
    return `${Math.floor(days)}d`;
  }
  return relTime(lead.created_at).replace(' ago', '');
}
function cleanName(value) {
  return String(value == null ? '' : value).replace(/\s+/g, ' ').replace(/^[\s,;:.!?"'`|\/-]+|[\s,;:.!?"'`|\/-]+$/g, '');
}
function nameOf(lead) {
  const name = ((cleanName(lead.first_name) + ' ' + cleanName(lead.last_name)).trim());
  return name || cleanName(lead.email) || 'Unknown lead';
}
function initialsOf(lead) {
  const a = cleanName(lead.first_name);
  const b = cleanName(lead.last_name);
  if (a || b) return ((a[0] || '') + (b[0] || '')).toUpperCase();
  return String(lead.email || '?').slice(0, 2).toUpperCase();
}
function stageMeta(status) { return STAGE_META[status] || { label: labelize(status) || 'Unknown', tone: 'grey' }; }
function stageIndexOf(lead) { const i = STAGES.indexOf(lead.status); return i; }
function needsReview(lead) { return (lead.flags || []).some((f) => NEEDS_REVIEW_FLAGS.includes(f)); }
function hasUnmatchedProperty(lead) {
  return !lead.property_address || (lead.flags || []).includes('unmatched_property');
}
function sortAttentionLeads(a, b) {
  const aUnmatched = hasUnmatchedProperty(a);
  const bUnmatched = hasUnmatchedProperty(b);
  if (aUnmatched !== bUnmatched) return aUnmatched ? -1 : 1;
  return new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0);
}
function optedIn(lead) {
  const v = String((lead.questionnaire && lead.questionnaire.booking_opt_in) || '').toLowerCase().replace(/[\s-]/g, '_');
  return v === 'opt_in' || v === 'yes' || v === 'true';
}
function isStarred(lead) { return state.starred.has(String(lead.id)); }
function leadPropertyId(lead) { return lead.catalogue_property_id ?? lead.property_id; }
function dayLabel(dateStr) {
  const d = new Date(dateStr);
  if (Number.isFinite(d.getTime())) return String(d.getDate());
  return String(dateStr || '').slice(8, 10);
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch {
    try {
      const ta = el('textarea', { style: 'position:fixed;opacity:0;pointer-events:none' });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return ok;
    } catch { return false; }
  }
}

/* ═══ 4. Icons (inline SVG, stroke currentColor) ══════════════════════════ */

const SVG_OPEN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
const ICONS = {
  form: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 7h6M9 12h6M9 16h4"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
  circle: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
  arrowUp: '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
  inbox: '<path d="M22 12h-5l-2 3h-6l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  list: '<path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/>',
  columns: '<rect x="3" y="4" width="5" height="16" rx="1"/><rect x="9.5" y="4" width="5" height="16" rx="1"/><rect x="16" y="4" width="5" height="16" rx="1"/>',
  sidebar: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M9 4v16"/>',
  star: '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
  building: '<path d="M4 21V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v16"/><path d="M2 21h20"/><path d="M9 7h2"/><path d="M13 7h2"/><path d="M9 11h2"/><path d="M13 11h2"/><path d="M9 15h2"/><path d="M13 15h2"/><path d="M10 21v-3h4v3"/>',
  chart: '<path d="M4 20V10"/><path d="M10 20V4"/><path d="M16 20v-8"/><path d="M22 20H2"/>',
  mail: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m2 7 10 6 10-6"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4"/><path d="M16 3v4"/><path d="M3 10h18"/>',
  external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><path d="M15 3h6v6"/><path d="M10 14 21 3"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36L21 8"/><path d="M21 3v5h-5"/>',
  alert: '<path d="m10.29 3.86-8.47 14.14A2 2 0 0 0 3.53 21h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m8.5 12.2 2.4 2.4 4.8-5"/>',
  send: '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  ellipsis: '<circle cx="5" cy="12" r="1.7" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.7" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.7" fill="currentColor" stroke="none"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/><path d="M12 14v3"/>',
  gear: '<path d="M18.837,9.168 L19.180,10.210 L21.356,10.350 L21.356,13.650 L19.180,13.790 L18.837,14.832 L18.343,15.811 L19.782,17.449 L17.449,19.782 L15.811,18.343 L14.832,18.837 L13.790,19.180 L13.650,21.356 L10.350,21.356 L10.210,19.180 L9.168,18.837 L8.189,18.343 L6.551,19.782 L4.218,17.449 L5.657,15.811 L5.163,14.832 L4.820,13.790 L2.644,13.650 L2.644,10.350 L4.820,10.210 L5.163,9.168 L5.657,8.189 L4.218,6.551 L6.551,4.218 L8.189,5.657 L9.168,5.163 L10.210,4.820 L10.350,2.644 L13.650,2.644 L13.790,4.820 L14.832,5.163 L15.811,5.657 L17.449,4.218 L19.782,6.551 L18.343,8.189 Z"/><circle cx="12" cy="12" r="3.25"/>',
  // Linear's Agent glyph — shipped as a complete 16×16 filled SVG, not a stroked path.
  agent: '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M4.07132 3.8283C4.04394 3.81721 4.01406 3.81379 3.98488 3.8184C3.95566 3.82301 3.92826 3.83551 3.90561 3.85453C3.88297 3.87356 3.86594 3.8984 3.85636 3.92639C3.84678 3.95437 3.84501 3.98443 3.85124 4.01335L5.80802 13.1405C5.81898 13.1915 5.83884 13.2155 5.85542 13.2298C5.87605 13.2476 5.9078 13.2631 5.94754 13.268C5.98728 13.2729 6.0217 13.2654 6.04578 13.2532C6.06507 13.2434 6.08993 13.2252 6.11273 13.1784L7.83779 9.64746C8.05513 9.20258 8.45077 8.87059 8.92663 8.73378L12.7035 7.64791C12.7535 7.63353 12.776 7.61215 12.789 7.59475C12.8052 7.57307 12.8186 7.54044 12.8207 7.50049C12.8228 7.46054 12.813 7.42669 12.7992 7.40342C12.788 7.38476 12.7681 7.36116 12.7199 7.34158L4.07132 3.8283C4.07129 3.82829 4.07135 3.82832 4.07132 3.8283ZM3.75083 2.33677C4.04945 2.2896 4.35527 2.32474 4.63541 2.43841L13.2843 5.95183C13.2843 5.95184 13.2843 5.95183 13.2843 5.95183C14.747 6.54596 14.6351 8.65343 13.1179 9.08953L9.34109 10.1754C9.27311 10.1949 9.21659 10.2424 9.18554 10.3059L7.46077 13.8363C7.46072 13.8364 7.46082 13.8362 7.46077 13.8363C6.76755 15.2562 4.67275 14.9979 4.34147 13.4555L2.38492 4.3294C2.38489 4.32925 2.38495 4.32956 2.38492 4.3294C2.32134 4.03401 2.33935 3.72642 2.43722 3.44054C2.53514 3.15452 2.70919 2.90061 2.94065 2.70612C3.17211 2.51164 3.45221 2.38394 3.75083 2.33677Z"/></svg>',
};
function icon(name) {
  const raw = ICONS[name] || '';
  return el('span', { class: 'icon', html: raw.startsWith('<svg') ? raw : SVG_OPEN + raw + '</svg>' });
}

/* ═══ 5. API layer — one helper for every call ════════════════════════════ */

function apiErrorMessage(status, data) {
  const err = data && data.error;
  if (err && typeof err.message === 'string') {
    if (err.code === 'backend_unavailable') return 'The backend is temporarily unavailable. Please try again shortly.';
    return err.message;
  }
  if (data && data.detail != null) {
    if (typeof data.detail === 'string') return data.detail;
    if (Array.isArray(data.detail)) return data.detail.map((d) => (d && d.msg) || JSON.stringify(d)).join('; ');
    if (typeof data.detail === 'object') return data.detail.message || data.detail.detail || JSON.stringify(data.detail);
  }
  if ([502, 504, 520, 522, 524].includes(status)) return 'The server could not confirm the result. Refresh the lead before retrying.';
  return `Request failed (HTTP ${status})`;
}

async function api(path, body, options = {}) {
  const response = await fetch(API_BASE + path, {
    ...options,
    method: options.method || (body === undefined ? 'GET' : 'POST'),
    headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...(state.mode && path !== 'settings/mode' ? { 'X-TVE-Mode': state.mode } : {}), ...options.headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  const receivedMode = response.headers.get('x-tve-mode');
  if (receivedMode && !state.mode) { state.mode = receivedMode; state.starred = loadStarred(receivedMode); }
  if (receivedMode && state.mode !== receivedMode) {
    window.location.reload();
    throw new Error('The environment changed. Refreshing the dashboard.');
  }
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    if (response.status === 409 && response.headers.get('x-tve-mode') && response.headers.get('x-tve-mode') !== state.mode) window.location.reload();
    const error = new Error(apiErrorMessage(response.status, data));
    error.status = response.status;
    error.code = (data && data.error && data.error.code) || null;
    throw error;
  }
  if (data == null) {
    const error = new Error('The backend returned an unreadable response.');
    error.status = response.status;
    throw error;
  }
  return data;
}

/* ═══ 6. Saved segments (Views) ══════════════════════════════════════════ */

const VIEW_DEFS = [
  { id: 'needs-review', title: 'Needs review', icon: 'alert', desc: 'Flagged leads that need a decision', filter: (l) => needsReview(l) },
  { id: 'high-intent', title: 'High intent', icon: 'star', desc: 'High score or opted in to booking', filter: (l) => (l.score && l.score.band === 'high') || optedIn(l) },
  { id: 'awaiting-questionnaire', title: 'Awaiting questionnaire', icon: 'mail', desc: 'Enquired or invitation sent', filter: (l) => l.status === 'enquiried' || l.status === 'Questionnaire_sent' },
  { id: 'awaiting-booking', title: 'Awaiting booking', icon: 'calendar', desc: 'Booking link sent, awaiting a viewing request', filter: (l) => l.status === 'booking_link_sent' },
  { id: 'viewings-requested', title: 'Viewings requested', icon: 'calendar', desc: 'A viewing slot has been requested', filter: (l) => !!l.viewing_slot_start },
  { id: 'stalled', title: 'Stalled', icon: 'alert', desc: 'Stalled at any stage of the journey', filter: (l) => (l.flags || []).some((f) => String(f).startsWith('stalled_')) },
  { id: 'unscored', title: 'Unscored', icon: 'list', desc: 'No intent score yet', filter: (l) => !l.score || l.score.value == null || l.score.band === 'unscored' },
];

/* ═══ 7. Shared components ═══════════════════════════════════════════════ */

function stageIcon(status) {
  const idx = STAGES.indexOf(status);
  const meta = stageMeta(status);
  const svg = STAGE_SVGS[idx >= 0 ? idx : 0];
  return el('span', { class: 'stage-icon', html: svg, style: `color:var(--${meta.tone})` });
}

function stagePill(status) {
  const meta = stageMeta(status);
  return el('span', { class: 'stage-pill' }, stageIcon(status), meta.label);
}

function scoreChip(score) {
  const band = score && score.band ? score.band : 'unscored';
  const value = score && typeof score.value === 'number' ? score.value : null;
  const chip = el('span', { class: `chip score chip-${band}` }, value == null ? '—' : String(value));
  if (value != null) {
    const reasons = (score && score.reasons && score.reasons.length) ? score.reasons.join(' · ') : `Intent score (${band})`;
    chip.title = reasons;
  } else chip.title = 'No intent score yet';
  return chip;
}

function flagMeta(flag) { return FLAGS[flag] || { label: labelize(flag), tone: 'grey', tip: labelize(flag) }; }

function flagChips(lead) {
  const flags = lead.flags || [];
  if (!flags.length) return el('span', { class: 'none-cell', text: '—' });
  return el('span', { class: 'flag-chips' }, flags.map((f) => {
    const meta = flagMeta(f);
    return el('span', { class: `chip chip-flag f-${meta.tone}`, title: meta.tip }, meta.label);
  }));
}

function flagDots(lead) {
  const flags = lead.flags || [];
  if (!flags.length) return el('span', { class: 'none-cell', text: '—' });
  const shown = flags.slice(0, 4);
  const dots = el('span', { class: 'flag-dots' }, shown.map((f) => {
    const meta = flagMeta(f);
    return el('span', { class: `dot c-${meta.tone === 'red' ? 'red' : meta.tone === 'amber' ? 'amber' : 'grey'}`, title: meta.tip });
  }));
  if (flags.length > 4) dots.append(el('span', { class: 'more', title: flags.slice(4).map((f) => flagMeta(f).tip).join(' · ') }, `+${flags.length - 4}`));
  return dots;
}

function platformChip(lead) {
  const platform = lead.enquiry && lead.enquiry.platform;
  if (!platform) return el('span', { class: 'none-cell', text: '—' });
  const tone = PLATFORM_CLASSES[String(platform).toLowerCase().replace(/[^a-z]/g, '')];
  const cls = 'chip chip-platform' + (tone ? ' ' + tone : '');
  return el('span', { class: cls, title: `Source platform: ${platform}` }, capitalize(platform));
}

function emptyState(iconName, title, sub, cta, ok) {
  const box = el('div', { class: 'empty' });
  box.append(el('div', { class: 'empty-icon' + (ok ? ' ok' : '') }, icon(iconName)));
  box.append(el('div', { class: 'empty-title', text: title }));
  if (sub) box.append(el('div', { class: 'empty-sub', text: sub }));
  if (cta) box.append(cta);
  return el('div', { class: 'panel' }, box);
}

function loadingState(text) { return el('div', { class: 'loading', text: text || 'Loading…' }); }

function searchHaystack(lead) {
  return [nameOf(lead), lead.email, lead.property_address, lead.enquiry && lead.enquiry.platform, (lead.flags || []).join(' ')]
    .filter(Boolean).join(' ').toLowerCase();
}

function sortLeads(list, sort) {
  const arr = [...list];
  if (sort === 'oldest') arr.sort((a, b) => new Date(a.created_at || 0) - new Date(b.created_at || 0));
  else if (sort === 'score') arr.sort((a, b) => ((b.score && b.score.value) ?? -1) - ((a.score && a.score.value) ?? -1) || new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
  else arr.sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
  return arr;
}

function filterLeads(list, extra) {
  let out = list;
  if (extra.viewDef) out = out.filter(extra.viewDef.filter);
    if (extra.propertyId != null) out = out.filter((l) => String(leadPropertyId(l) ?? '') === String(extra.propertyId));
  const q = state.search.trim().toLowerCase();
  if (q) out = out.filter((l) => searchHaystack(l).includes(q));
  if (state.stageFilter !== 'all') {
    if (state.stageFilter === 'unscored') out = out.filter((l) => !l.score || l.score.value == null || l.score.band === 'unscored');
    else out = out.filter((l) => l.status === state.stageFilter);
  }
  return sortLeads(out, state.sort);
}

/* ═══ 8. Shell: sidebar, header, status ══════════════════════════════════ */

const NAV_ITEMS = [
  { page: 'overview', label: 'Overview', icon: 'chart' },
  { page: 'inbox', label: 'Inbox', icon: 'inbox', badge: 'badge-inbox' },
  { page: 'leads', label: 'Leads', icon: 'list' },
  { page: 'views', label: 'Views', icon: 'columns' },
  { page: 'myleads', label: 'My leads', icon: 'star', badge: 'badge-myleads' },
  { page: 'properties', label: 'Properties', icon: 'building' },
  { page: 'forms', label: 'Forms', icon: 'form' },
];

let headerSearch = null;
let headerSearchWrap = null;

// Collapsing turns the sidebar into an icon rail. The choice is stored per browser,
// like the starred leads, so it survives reloads without touching the server.
function applySidebarState(collapsed) {
  byId('sidebar').classList.toggle('is-collapsed', collapsed);
  const toggle = byId('sidebar-toggle');
  const label = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
  toggle.setAttribute('aria-expanded', String(!collapsed));
  toggle.setAttribute('aria-label', label);
  toggle.title = label;
  for (const item of document.querySelectorAll('.nav-item[data-label]')) {
    item.title = collapsed ? item.dataset.label : '';
  }
}

function toggleSidebar() {
  const collapsed = !byId('sidebar').classList.contains('is-collapsed');
  applySidebarState(collapsed);
  try { localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0'); } catch { /* storage unavailable */ }
}

function buildShell() {
  const nav = byId('nav');
  nav.append(el('div', { class: 'nav-section', text: 'Workspace' }));
  for (const item of NAV_ITEMS) {
    nav.append(el('a', { class: 'nav-item', href: `#/${item.page}`, dataset: { page: item.page, label: item.label }, 'aria-label': item.label },
      icon(item.icon), el('span', { text: item.label }),
      item.badge ? el('span', { class: 'count-badge', id: item.badge }) : null));
  }
  nav.append(el('div', { class: 'nav-section', text: 'YOUR TEAMS' }));
  nav.append(el('a', { class: 'nav-item nav-item--agent', href: '#/agent', dataset: { page: 'agent', label: 'Agent' }, 'aria-label': 'Agent' },
    icon('agent'), el('span', { text: 'Agent' })));
  byId('foot-settings').replaceWith(icon('gear'));
  byId('banner-icon').replaceWith(icon('alert'));

  const sidebarToggle = byId('sidebar-toggle');
  sidebarToggle.append(icon('sidebar'));
  sidebarToggle.addEventListener('click', toggleSidebar);
  let sidebarCollapsed = false;
  try { sidebarCollapsed = localStorage.getItem(SIDEBAR_KEY) === '1'; } catch { /* storage unavailable */ }
  applySidebarState(sidebarCollapsed);

  headerSearch = el('input', { class: 'input', type: 'search', placeholder: 'Search leads  ( / )', 'aria-label': 'Search leads' });
  headerSearch.addEventListener('input', () => setSearch(headerSearch.value, headerSearch));

  const actions = byId('header-actions');
  headerSearchWrap = el('div', { class: 'search-wrap' }, icon('search'), headerSearch);
  actions.append(
    headerSearchWrap
  );
}

// The single header search box is scoped to the active page: Properties searches
// properties, every other page keeps the leads query.
const SEARCH_SCOPES = {
  properties: { key: 'propertySearch', label: 'Search properties' },
  default: { key: 'search', label: 'Search leads' },
};

function searchScope() { return SEARCH_SCOPES[state.route.page] || SEARCH_SCOPES.default; }

function syncHeaderSearch() {
  if (!headerSearch) return;
  // Agent and Settings have their own inputs; the global lead search does not apply.
  if (headerSearchWrap) headerSearchWrap.hidden = ['agent', 'settings', 'forms'].includes(state.route.page);
  const scope = searchScope();
  headerSearch.placeholder = `${scope.label}  ( / )`;
  headerSearch.setAttribute('aria-label', scope.label);
  headerSearch.value = state[scope.key] || '';
}

function setSearch(value, source) {
  const { key } = searchScope();
  state[key] = value || '';
  if (headerSearch && headerSearch !== source) headerSearch.value = state[key];
  if (state.pageSearchInput && state.pageSearchInput !== source) state.pageSearchInput.value = state[key];
  if (state.refreshPage) state.refreshPage();
}

function updateBadges() {
  const inboxBadge = byId('badge-inbox');
  const inboxCount = state.leads.filter(needsReview).length;
  inboxBadge.textContent = state.leadsLoaded ? String(inboxCount) : '';
  inboxBadge.classList.toggle('accent', state.leadsLoaded && inboxCount > 0);
  const starredBadge = byId('badge-myleads');
  starredBadge.textContent = state.starred.size ? String(state.starred.size) : '';
  starredBadge.classList.toggle('accent', state.starred.size > 0);
}

function updateStatusUI() {
  const text = state.lastUpdatedAt ? `Updated ${hhmmss(state.lastUpdatedAt)}` : 'Connecting…';
  const statusText = byId('status-text');
  const statusDot = byId('status-dot');
  if (statusText) {
    statusText.textContent = text;
    statusText.parentElement.title = state.online === false && state.lastError ? state.lastError : 'Live';
  }
  if (statusDot) statusDot.className = 'dot ' + (state.online === true ? 'live' : state.online === false ? 'dead' : 'c-grey');
  const show = state.online === false;
  byId('banner').hidden = !show;
  const bannerText = byId('banner-text');
  if (bannerText) bannerText.textContent = state.lastError || 'Backend offline — start the stack';
  document.body.classList.toggle('has-banner', show);
}

function markPoll(ok, message) {
  state.online = ok;
  state.lastError = ok ? null : message;
  state.lastUpdatedAt = new Date();
  updateStatusUI();
}

/* ═══ 9. Routing ══════════════════════════════════════════════════════════ */

function currentRoute() {
  const raw = (location.hash || '#/overview').slice(1);
  const [pathPart, queryPart] = raw.split('?');
  const page = (pathPart || '/overview').replace(/^\/+/, '').split('/')[0] || 'overview';
  return { page: PAGES.includes(page) ? page : 'overview', params: new URLSearchParams(queryPart || '') };
}

function onRouteChange() {
  if (state.formsCleanup) state.formsCleanup();
  if (state.settingsCleanup) state.settingsCleanup();
  state.route = currentRoute();
  state.refreshPage = null;
  state.pageSearchInput = null;
  renderApp();
}

function renderApp() {
  const page = state.route.page;
  document.title = `${PAGE_TITLES[page]} · TVE Lead Intelligence`;
  document.querySelectorAll('[data-page]').forEach((node) => node.classList.toggle('active', node.dataset.page === page));
  byId('breadcrumb').replaceChildren(
    el('span', { class: 'crumb-root', text: 'Thames Valley Estates' }),
    el('span', { class: 'crumb-sep', text: '/' }),
    el('span', { text: PAGE_TITLES[page] })
  );
  syncHeaderSearch();
  const view = byId('view');
  view.replaceChildren();
  view.append(el('div', { id: 'pending-delete-notice' }));
  updatePendingDeleteNotice();
  PAGE_RENDERERS[page](view);
  updateBadges();
}

const PAGE_RENDERERS = {
  overview: renderOverviewPage,
  inbox: renderInboxPage,
  leads: (view) => renderLeadsPage(view, 'leads'),
  views: renderViewsPage,
  myleads: (view) => renderLeadsPage(view, 'myleads'),
  properties: renderPropertiesPage,
  forms: renderFormsPage,
  agent: renderAgentPage,
  settings: renderSettingsPage,
};

function pageHead(title, sub) {
  return el('div', { class: 'page-head' },
    el('h1', { class: 'page-title', text: title }),
    el('span', { class: 'page-sub', text: sub || '' }));
}

/* ═══ 10. Leads table (shared by Leads + My leads) ═══════════════════════ */

function leadRow(lead) {
  const tr = el('tr');
  const starred = isStarred(lead);
  const starBtn = el('button', {
    class: 'icon-btn star-btn' + (starred ? ' starred' : ''),
    'aria-label': starred ? `Unstar ${nameOf(lead)}` : `Star ${nameOf(lead)}`,
    title: starred ? 'Remove from My leads' : 'Add to My leads',
    onclick: (e) => { e.stopPropagation(); toggleStar(lead); },
  }, icon('star'));
  const updated = el('td', { class: 'num muted' }, relTime(lead.updated_at));
  updated.title = fullTime(lead.updated_at);
  tr.append(
    el('td', {}, starBtn),
    el('td', {}, stagePill(lead.status)),
    el('td', { class: 'cell-name' }, nameOf(lead)),
    el('td', { class: 'cell-email' }, lead.email || '—'),
    el('td', { class: 'cell-prop' }, lead.property_address || '—'),
    el('td', {}, scoreChip(lead.score)),
    el('td', {}, flagDots(lead)),
    el('td', {}, platformChip(lead)),
    updated,
    el('td', { class: 'cell-menu' }, rowMenuButton(lead))
  );
  tr.addEventListener('click', (e) => {
    if (e.target.closest('button, a')) return;
    openDrawer(lead.id);
  });
  return tr;
}

function buildLeadsTable() {
  const thead = el('thead', {}, el('tr', {},
    el('th', { 'aria-label': 'Starred' }), el('th', { text: 'Stage' }), el('th', { text: 'Name' }),
    el('th', { text: 'Email' }), el('th', { text: 'Property' }), el('th', { text: 'Score' }),
    el('th', { text: 'Flags' }), el('th', { text: 'Source' }), el('th', { text: 'Updated' }),
    el('th', { 'aria-label': 'Row actions' })));
  const tbody = el('tbody');
  const table = el('table', { class: 'leads' }, thead, tbody);
  return { table, tbody };
}

function renderLeadsPage(view, mode) {
  const isMyLeads = mode === 'myleads';
  view.append(pageHead(
    isMyLeads ? 'My leads' : 'Leads',
    isMyLeads ? 'Starred leads, kept in this browser' : 'Every enquiry, parsed and scored'
  ));

  const toolbar = el('div', { class: 'toolbar' });
  const search = el('input', { class: 'input', type: 'search', placeholder: 'Search leads', 'aria-label': 'Search leads' });
  search.value = state.search;
  search.addEventListener('input', () => setSearch(search.value, search));
  state.pageSearchInput = search;
  toolbar.append(el('div', { class: 'search-wrap' }, icon('search'), search));

  const stageSelect = el('select', { class: 'input', 'aria-label': 'Filter by stage' },
    el('option', { value: 'all', text: 'All stages' }),
    STAGES.map((s) => el('option', { value: s, text: stageMeta(s).label })),
    el('option', { value: 'unscored', text: 'Unscored' }));
  stageSelect.value = state.stageFilter;
  stageSelect.addEventListener('change', () => { state.stageFilter = stageSelect.value; update(); });
  toolbar.append(stageSelect);

  const sortSelect = el('select', { class: 'input', 'aria-label': 'Sort leads' },
    el('option', { value: 'updated', text: 'Recently updated' }),
    el('option', { value: 'score', text: 'Score' }),
    el('option', { value: 'oldest', text: 'Oldest' }));
  sortSelect.value = state.sort;
  sortSelect.addEventListener('change', () => { state.sort = sortSelect.value; update(); });
  toolbar.append(sortSelect, el('span', { class: 'spacer' }));

  const countEl = el('span', { class: 'result-count' });
  toolbar.append(countEl);
  view.append(toolbar);

  const chipsRow = el('div', { class: 'toolbar', style: 'margin-top:-8px' });
  view.append(chipsRow);

  const { table, tbody } = buildLeadsTable();
  const wrap = el('div', { class: 'panel table-wrap' });
  view.append(wrap);

  function update() {
    const params = state.route.params;
    const viewId = isMyLeads ? null : params.get('view');
    const propertyId = isMyLeads ? null : params.get('property');
    const viewDef = viewId ? VIEW_DEFS.find((v) => v.id === viewId) || null : null;
    const source = isMyLeads ? state.leads.filter(isStarred) : state.leads;
    const list = filterLeads(source, { viewDef, propertyId });

    chipsRow.replaceChildren();
    if (viewDef) {
      chipsRow.append(filterChip(`View: ${viewDef.title}`, () => { location.hash = '#/leads'; }));
    } else if (viewId) {
      chipsRow.append(filterChip(`View: ${viewId}`, () => { location.hash = '#/leads'; }));
    }
    if (propertyId != null) {
      const known = state.leads.find((l) => String(leadPropertyId(l) ?? '') === String(propertyId));
      const prop = state.properties.find((p) => String(p.id) === String(propertyId));
      const label = (known && known.property_address) || (prop && propertyLabel(prop)) || `Property ${propertyId}`;
      chipsRow.append(filterChip(`Property: ${label}`, () => { location.hash = '#/leads'; }));
    }

    if (!state.leadsLoaded) {
      countEl.textContent = '';
      wrap.replaceChildren(loadingState('Waiting for the backend…'));
      return;
    }
    countEl.textContent = `${list.length} of ${source.length} lead${source.length === 1 ? '' : 's'}`;

    if (!list.length) {
      const noFilters = !state.search.trim() && state.stageFilter === 'all' && !viewDef && !viewId && propertyId == null;
      if (isMyLeads && state.starred.size === 0) {
        wrap.replaceChildren(emptyState('star', 'No starred leads', 'Star leads from the Leads table to keep them here.',
          el('button', { class: 'btn btn-ghost', onclick: () => { location.hash = '#/leads'; } }, icon('list'), 'Go to leads')));
      } else if (!isMyLeads && state.leads.length === 0 && noFilters) {
        wrap.replaceChildren(emptyState('list', 'No leads yet', 'New enquiries will appear here as they arrive.'));
      } else {
        wrap.replaceChildren(emptyState('search', 'No leads match your filters', 'Adjust the search, stage filter or saved view.',
          el('button', { class: 'btn btn-ghost', onclick: () => {
            state.stageFilter = 'all'; stageSelect.value = 'all'; setSearch('', null); if (viewDef || viewId || propertyId != null) location.hash = '#/leads'; else update();
          } }, 'Clear filters')));
      }
      return;
    }
    tbody.replaceChildren(...list.map(leadRow));
    wrap.replaceChildren(table);
  }

  update();
  state.refreshPage = update;
}

function filterChip(text, onRemove) {
  return el('span', { class: 'filter-chip' }, text,
    el('button', { class: 'chip-x', 'aria-label': 'Remove filter', onclick: onRemove }, icon('x')));
}

/* ═══ 11. Inbox (triage) ═════════════════════════════════════════════════ */

function renderInboxPage(view) {
  view.append(pageHead('Inbox', 'Leads flagged for triage, newest first'));
  const wrap = el('div', { class: 'panel' });
  view.append(wrap);

  function update() {
    if (!state.leadsLoaded) { wrap.replaceChildren(loadingState('Waiting for the backend…')); return; }
    const q = state.search.trim().toLowerCase();
    let list = state.leads.filter(needsReview);
    if (q) list = list.filter((l) => searchHaystack(l).includes(q));
    // Unmatched enquiries need triage before other attention flags. Keep the
    // newest-first order within each group so the inbox remains predictable.
    list.sort((a, b) => {
      const aNoProperty = !a.property_address || (a.flags || []).includes('unmatched_property');
      const bNoProperty = !b.property_address || (b.flags || []).includes('unmatched_property');
      if (aNoProperty !== bNoProperty) return aNoProperty ? -1 : 1;
      return new Date(b.created_at || b.updated_at || 0) - new Date(a.created_at || a.updated_at || 0);
    });

    if (!list.length) {
      wrap.replaceChildren(emptyState('check', 'All clear — no leads need attention.',
        state.leads.length ? 'Flagged leads appear here as they stall or fail to match a property.' : 'New enquiries will appear here as they arrive.', null, true));
      return;
    }

    wrap.replaceChildren(...list.map((lead) => {
      const row = el('div', { class: 'inbox-row', role: 'button', tabindex: '0' },
        el('div', { class: 'inbox-main' },
          el('div', { class: 'initials' }, initialsOf(lead)),
          el('div', { class: 'inbox-id' },
            el('div', { class: 'inbox-name' }, el('span', { text: nameOf(lead) }), flagChips(lead)),
            el('div', { class: 'inbox-email' }, lead.email || 'No email extracted')),
          el('div', { class: 'inbox-prop' + (lead.property_address ? '' : ' none'), title: lead.property_address || 'No property matched' },
            lead.property_address || 'No property matched')),
        el('span', { class: 'inbox-age', title: `Enquiry age — created ${fullTime(lead.created_at)}` }, ageText(lead)),
        el('div', { class: 'inbox-actions' },
          el('button', { class: 'btn btn-ghost btn-sm', onclick: (e) => { e.stopPropagation(); openAssignModal(lead); } }, icon('building'), 'Assign property'),
          el('button', { class: 'btn btn-ghost btn-sm', onclick: (e) => { e.stopPropagation(); openDrawer(lead.id); } }, icon('list'), 'View lead')));
      row.addEventListener('click', () => openDrawer(lead.id));
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') openDrawer(lead.id); });
      return row;
    }));
  }

  update();
  state.refreshPage = update;
}

/* ═══ 12. Overview ════════════════════════════════════════════════════════ */

function kpi(label, value, sub) {
  const missing = value == null || value === '—';
  return el('div', { class: 'panel kpi' },
    el('div', { class: 'kpi-label', text: label }),
    el('div', { class: 'kpi-value' + (missing ? ' muted' : ''), text: missing ? '—' : String(value) }),
    sub ? el('div', { class: 'kpi-sub', text: sub }) : null);
}

function renderOverviewPage(view) {
  view.append(pageHead('Overview', 'Live pipeline health across every stage'));
  const kpiRow = el('div', { class: 'card-grid kpi-grid' });
  view.append(kpiRow);
  const grid = el('div', { class: 'card-grid two-col' });
  const funnelCard = el('div', { class: 'panel panel-pad' });
  const chartCard = el('div', { class: 'panel chart-wrap' });
  grid.append(funnelCard, chartCard);
  view.append(grid);
  const bottomGrid = el('div', { class: 'card-grid two-col', style: 'margin-top:10px' });
  const attentionCard = el('div', { class: 'panel' });
  const activityCard = el('div', { class: 'panel' });
  bottomGrid.append(attentionCard, activityCard);
  view.append(bottomGrid);

  function update() {
    renderKpis(kpiRow);
    renderFunnel(funnelCard);
    renderDaily(chartCard);
    renderNeedsAttention(attentionCard);
    renderRecentActivity(activityCard);
  }

  update();
  state.refreshPage = update;
  if (!state.overviewAt || Date.now() - state.overviewAt > POLL_OVERVIEW_MS) pollOverview();
}

function renderKpis(kpiRow) {
  const m = state.metrics || {};
  const has = !!state.metrics;
  kpiRow.replaceChildren(
    kpi('Total leads', has ? m.total_leads : null, 'every enquiry on record'),
    kpi('New · 7 days', has ? m.new_7d : null, 'enquiries received this week'),
    kpi('Completion rate', has ? pct(m.completion_rate) : null, 'questionnaires completed'),
    kpi('Viewing rate', has ? pct(m.viewing_rate) : null, 'leads with a viewing slot'),
    kpi('Avg to completion', has ? fmtHours(m.avg_hours_to_completion) : null, 'hours from enquiry to questionnaire'),
    kpi('Needs review', has ? m.needs_review_count : null, 'flagged for triage')
  );
}

function renderFunnel(card) {
  card.replaceChildren(el('h3', { class: 'panel-title', text: 'Journey funnel' }));
  if (!state.funnel) {
    card.append(el('div', { class: 'section-note', text: state.funnelError ? `Funnel unavailable — ${state.funnelError}` : 'Waiting for funnel data…' }));
    return;
  }
  const stages = (state.funnel.stages || []).slice(0, STAGES.length);
  const total = (state.metrics && state.metrics.total_leads) ?? (stages[0] && stages[0].count) ?? 0;
  const max = Math.max(1, ...stages.map((s) => Number(s.count) || 0));
  for (const stage of stages) {
    const meta = stageMeta(stage.stage);
    const count = Number(stage.count) || 0;
    const share = total ? Math.round((count / total) * 100) : 0;
    card.append(el('div', { class: 'funnel-row' },
      el('span', { class: 'funnel-label' }, stageIcon(stage.stage), meta.label),
      el('div', { class: 'funnel-track' }, el('div', { class: 'funnel-bar', style: `width:${(count / max) * 100}%;background:var(--${meta.tone === 'grey' ? 'grey' : meta.tone})` })),
      el('span', { class: 'funnel-nums' },
        el('b', { text: String(count) }), ` · ${share}%`)));
  }
}

function renderDaily(card) {
  const history = (state.funnel?.daily_new || []).slice().sort((a, b) => a.date.localeCompare(b.date));
  const available = history.length;
  const choices = [[7, 'Last 7 days'], [30, 'Last month'], [180, 'Last 6 months'], [365, 'Last year']];
  const requested = state.dailyRange || 30;
  const range = choices.some(([days]) => days === requested && days <= available)
    ? requested : (choices.filter(([days]) => days <= available).at(-1)?.[0] || available);
  const controls = el('div', { class: 'history-controls', role: 'group', 'aria-label': 'Enquiry history range' });
  const focusLabel = card.contains(document.activeElement) ? document.activeElement.textContent : null;
  for (const [days, label] of choices) {
    const active = days === range;
    controls.append(el('button', { class: 'history-range' + (active ? ' is-active' : ''), type: 'button',
      disabled: days > available, 'aria-pressed': String(active),
      onclick: () => { state.dailyRange = days; renderDaily(card); }
    }, label));
  }
  card.replaceChildren(el('div', { class: 'history-heading' },
    el('h3', { class: 'panel-title', text: 'Enquiry activity' }), controls));
  if (!available) {
    card.append(el('div', { class: 'section-note', text: 'Waiting for enquiry history…' }));
    return;
  }
  const days = history.slice(-range);
  const monthly = range > 180;
  const weekly = range > 30 && !monthly;
  const buckets = [];
  for (const day of days) {
    const date = new Date(day.date + 'T00:00:00Z');
    const key = monthly ? day.date.slice(0, 7) : weekly
      ? new Date(date.getTime() - ((date.getUTCDay() + 6) % 7) * 86400000).toISOString().slice(0, 10) : day.date;
    let bucket = buckets.at(-1);
    if (!bucket || bucket.key !== key) { bucket = { key, start: day.date, end: day.date, count: 0 }; buckets.push(bucket); }
    bucket.end = day.date;
    bucket.count += Number(day.count) || 0;
  }
  const format = (value) => new Date(value + 'T00:00:00Z').toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  const total = days.reduce((sum, day) => sum + (Number(day.count) || 0), 0);
  card.append(el('div', { class: 'history-summary' },
    el('strong', { text: total.toLocaleString() }), el('span', { text: 'enquiries in this period' })));
  const caption = el('div', { class: 'history-caption', text: format(days[0].date) + ' – ' + format(days.at(-1).date) });
  card.append(caption);
  const max = Math.max(1, ...buckets.map(b => b.count));
  const ceiling = Math.ceil(max / 4) * 4;
  const plot = el('div', { class: 'history-plot' });
  const axis = el('div', { class: 'history-y', 'aria-hidden': 'true' });
  for (const value of [ceiling, ceiling / 2, 0]) axis.append(el('span', { text: String(value) }));
  const bars = el('div', { class: 'history-bars', role: 'group', 'aria-label': 'Enquiries grouped ' + (monthly ? 'monthly' : weekly ? 'weekly' : 'daily') });
  for (const bucket of buckets) {
    const description = format(bucket.start) + (bucket.end !== bucket.start ? ' – ' + format(bucket.end) : '') + ': ' + bucket.count + ' enquiries';
    const bar = el('button', { class: 'history-bar', type: 'button', style: 'height:' + (bucket.count / ceiling * 100) + '%',
      title: description, 'aria-label': description });
    const show = () => { caption.textContent = description; };
    const reset = () => { caption.textContent = format(days[0].date) + ' – ' + format(days.at(-1).date); };
    bar.addEventListener('mouseenter', show); bar.addEventListener('focus', show);
    bar.addEventListener('mouseleave', reset); bar.addEventListener('blur', reset);
    bars.append(bar);
  }
  plot.append(axis, bars);
  const labels = el('div', { class: 'history-x' + (monthly ? ' history-x-months' : ''), 'aria-hidden': 'true' });
  const labelIndexes = monthly ? buckets.map((_, index) => index)
    : [...new Set([0, Math.floor((buckets.length - 1) / 2), buckets.length - 1])];
  for (const index of labelIndexes) {
    labels.append(el('span', { text: new Date(buckets[index].start + 'T00:00:00Z').toLocaleDateString('en-GB', monthly
      ? { month: 'short', timeZone: 'UTC' } : { day: 'numeric', month: 'short', timeZone: 'UTC' }) }));
  }
  card.append(plot, labels, el('div', { class: 'history-foot', text: (monthly ? 'Monthly' : weekly ? 'Weekly' : 'Daily') + ' totals · Hover or focus a bar for details' }));
  if (focusLabel) [...controls.children].find(button => button.textContent === focusLabel)?.focus();
}

function renderNeedsAttention(card) {
  card.replaceChildren(el('div', { class: 'panel-pad' }, el('h3', { class: 'panel-title', text: 'Needs attention' })));
  if (!state.leadsLoaded) { card.append(loadingState('Waiting for the backend…')); return; }
  const flagged = state.leads.filter(needsReview)
    .sort(sortAttentionLeads);
  const visible = flagged.slice(0, 8);
  if (!flagged.length) {
    card.append(el('div', { class: 'panel-pad' },
      el('div', { class: 'empty', style: 'padding:24px 12px' },
        el('div', { class: 'empty-icon ok' }, icon('check')),
        el('div', { class: 'empty-sub', text: 'Nothing needs attention right now.' }))));
    return;
  }
  for (const lead of visible) {
    const time = el('span', { class: 'lr-time' }, relTime(lead.updated_at));
    time.title = fullTime(lead.updated_at);
    const row = el('button', { class: 'list-row', onclick: () => openDrawer(lead.id) },
      el('div', { class: 'initials' }, initialsOf(lead)),
      el('div', { class: 'lr-main' },
        el('div', { class: 'lr-name' }, nameOf(lead), flagChips(lead)),
        el('div', { class: 'lr-sub', text: lead.property_address || 'No property matched' })),
      time);
    card.append(row);
  }
  const more = el('button', { class: 'attention-more', type: 'button', onclick: () => { location.hash = '#/inbox'; } },
    flagged.length > visible.length ? `See more · ${flagged.length - visible.length} remaining` : 'See more in Inbox');
  card.append(more);
}

function renderRecentActivity(card) {
  card.replaceChildren(el('div', { class: 'panel-pad' }, el('h3', { class: 'panel-title', text: 'Recent activity' })));
  if (!state.leadsLoaded) { card.append(loadingState('Waiting for the backend…')); return; }
  const recent = sortLeads(state.leads, 'updated').slice(0, 8);
  if (!recent.length) { card.append(el('div', { class: 'panel-pad' }, el('div', { class: 'empty-sub', text: 'No activity yet.' }))); return; }
  for (const lead of recent) {
    const time = el('span', { class: 'lr-time' }, relTime(lead.updated_at));
    time.title = fullTime(lead.updated_at);
    card.append(el('button', { class: 'list-row', onclick: () => openDrawer(lead.id) },
      el('div', { class: 'initials' }, initialsOf(lead)),
      el('div', { class: 'lr-main' },
        el('div', { class: 'lr-name' }, nameOf(lead)),
        el('div', { class: 'lr-sub' }, lead.email || 'No email')),
      stagePill(lead.status),
      time));
  }
}

/* ═══ 13. Views page ═════════════════════════════════════════════════════ */

function renderViewsPage(view) {
  view.append(pageHead('Views', 'Saved segments across the pipeline'));
  const grid = el('div', { class: 'card-grid view-card-grid' });
  const refs = [];
  for (const def of VIEW_DEFS) {
    const countEl = el('span', { text: state.leadsLoaded ? String(state.leads.filter(def.filter).length) : '—' });
    grid.append(el('button', { class: 'view-card', 'aria-label': `Open view: ${def.title}`, onclick: () => { location.hash = `#/leads?view=${def.id}`; } },
      el('div', { class: 'vc-head' }, icon(def.icon), el('span', { class: 'vc-title', text: def.title })),
      el('div', { class: 'vc-desc', text: def.desc }),
      el('div', { class: 'vc-count' }, countEl, el('span', { class: 'vc-count-label', text: ' leads' }))));
    refs.push({ def, countEl });
  }
  view.append(grid);
  state.refreshPage = () => {
    for (const ref of refs) ref.countEl.textContent = state.leadsLoaded ? String(state.leads.filter(ref.def.filter).length) : '—';
  };
}

/* ═══ 14. Properties page ═════════════════════════════════════════════════ */

function propertyLabel(p) { return p.address || p.propertyName || `Property ${p.id}`; }
function propertyHaystack(p) {
  return [p.address, p.propertyName, p.street, p.town, p.postcode, p.altReference, p.id]
    .filter((v) => v != null && v !== '').join(' ').toLowerCase();
}
function propertyPriceText(p) {
  if (p.price == null) return null;
  // Live Acquaint sends pricePrefix as an int32 enum, never a currency symbol.
  const raw = p.pricePrefix != null ? String(p.pricePrefix).trim() : '';
  const prefix = raw && !/^\d+$/.test(raw) ? raw : '£';
  return `${prefix}${typeof p.price === 'number' ? fmtNum(p.price) : p.price}`;
}

async function ensureProperties(force) {
  if (!force && state.propertiesAt && Date.now() - state.propertiesAt < 60000) return;
  try {
    const data = await api('properties');
    state.properties = Array.isArray(data && data.properties) ? data.properties : [];
    state.propertiesAt = Date.now();
    state.propertiesError = null;
  } catch (err) {
    state.propertiesError = err.message;
  }
}

function renderPropertiesPage(view) {
  view.append(pageHead('Properties', 'Bookable properties and their lead pipelines'));
  const container = el('div', {});
  view.append(container);

  function update() {
    container.replaceChildren();
    if (!state.properties.length && state.propertiesError) {
      container.append(el('div', { class: 'panel panel-pad' },
        el('h3', { class: 'panel-title', text: 'Properties unavailable' }),
        el('div', { class: 'section-note', text: state.propertiesError }),
        el('div', { class: 'mt' }, el('button', { class: 'btn btn-ghost', onclick: () => { ensureProperties(true).then(update); } }, icon('refresh'), 'Retry'))));
      return;
    }
    if (!state.properties.length && !state.propertiesAt) {
      container.append(loadingState('Loading properties…'));
      return;
    }
    if (!state.properties.length) {
      container.append(emptyState('building', 'No properties available', 'The booking properties endpoint returned an empty list.'));
      return;
    }
    const q = state.propertySearch.trim().toLowerCase();
    const matches = q ? state.properties.filter((p) => propertyHaystack(p).includes(q)) : state.properties;
    if (!matches.length) {
      container.append(emptyState('search', 'No properties match your search', 'Try another address, town, postcode or reference.',
        el('button', { class: 'btn btn-ghost', onclick: () => { setSearch('', null); update(); } }, 'Clear search')));
      return;
    }
    const groups = new Map();
    for (const p of matches) {
      const key = (p.town && String(p.town).trim()) || 'Other locations';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(p);
    }
    const sortedGroups = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    for (const [town, props] of sortedGroups) {
      container.append(el('div', { class: 'group-head' },
        el('span', { class: 'group-title', text: town }),
        el('span', { class: 'group-count', text: `${props.length} propert${props.length === 1 ? 'y' : 'ies'}` })));
      const grid = el('div', { class: 'card-grid two-col' });
      for (const p of props) grid.append(propertyCard(p));
      container.append(grid);
    }
  }

  update();
  state.refreshPage = update;
  ensureProperties().then(() => { if (state.route.page === 'properties') update(); });
}

function propertyCard(p) {
  const leadsFor = state.leads.filter((l) => String(leadPropertyId(l) ?? '') === String(p.id));
  const stageMinis = STAGES.map((stage) => {
    const count = leadsFor.filter((l) => l.status === stage).length;
    return count ? el('span', { class: 'stage-mini', title: `${count} at ${stageMeta(stage).label}` },
      stageIcon(stage), String(count)) : null;
  }).filter(Boolean);
  return el('div', { class: 'panel property-card' },
    el('div', { class: 'prop-address' }, propertyLabel(p)),
    el('div', { class: 'prop-line' },
      propertyPriceText(p) ? el('span', { class: 'prop-meta' }, 'Rent ', el('b', { text: propertyPriceText(p) })) : null,
      p.bedrooms != null ? el('span', { class: 'prop-meta' }, `${p.bedrooms} bed${Number(p.bedrooms) === 1 ? '' : 's'}`) : null,
      p.altReference ? el('span', { class: 'prop-ref', title: 'Portal reference' }, p.altReference) : null),
    el('div', { class: 'prop-stats' },
      el('span', {}, el('b', { text: String(leadsFor.length) }), ` lead${leadsFor.length === 1 ? '' : 's'}`),
      leadsFor.length ? stageMinis : el('span', { class: 'muted', text: '— none yet' })),
    el('div', { class: 'prop-actions' },
      el('button', { class: 'btn btn-ghost btn-sm', onclick: () => { location.hash = `#/leads?property=${encodeURIComponent(p.id)}`; } }, icon('list'), 'View leads')));
}

/* ═══ 15. Row menu ═══════════════════════════════════════════════════════ */

let menuCleanup = null;

function closeMenus() {
  if (menuCleanup) { menuCleanup(); menuCleanup = null; }
  document.querySelectorAll('.menu').forEach((m) => m.remove());
}

function positionMenu(menu, anchor) {
  const rect = anchor.getBoundingClientRect();
  const height = menu.offsetHeight || 150;
  const width = menu.offsetWidth || 190;
  menu.style.top = `${Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - height - 8))}px`;
  menu.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
}

function rowMenuButton(lead) {
  return el('button', {
    class: 'icon-btn', 'aria-label': `Row actions for ${nameOf(lead)}`, title: 'Row actions',
    onclick: (e) => { e.stopPropagation(); openRowMenu(e.currentTarget, lead); },
  }, icon('ellipsis'));
}

function openRowMenu(anchor, lead) {
  closeMenus();
  const items = [];
  const resendItem = (label, endpoint, eligible, reason) => {
    const button = el('button', { class: 'menu-item', type: 'button', disabled: !eligible,
      title: eligible ? `Email a fresh link to ${lead.email}` : reason }, icon('send'), label);
    button.addEventListener('click', async (event) => {
      event.stopPropagation();
      if (button.disabled) return;
      button.disabled = true;
      button.textContent = 'Sending…';
      try {
        const refreshed = await api(`actions/${endpoint}`, { contact_id: lead.id });
        mergeLead(refreshed);
        closeMenus();
        toast(`${label === 'Send booking' ? 'Booking' : 'Questionnaire'} email sent`, 'success');
      } catch (err) {
        button.disabled = false;
        button.replaceChildren(icon('send'), document.createTextNode(label));
        toast(err.message, 'error');
      }
    });
    return button;
  };
  items.push(resendItem('Resend questionnaire', 'resend-invitation',
    !!lead.email && ['enquiried', 'Questionnaire_sent'].includes(lead.status),
    'Requires an email address and a lead awaiting their questionnaire.'));
  items.push(resendItem('Send booking', 'send-booking',
    !!lead.email && !!lead.property_id,
    'Requires an email address and an assigned property.'));
  if (lead.status === 'viewing_requested') {
    const decisionStatus = el('span', { class: 'viewing-decision-status', 'aria-live': 'polite' });
    const approve = el('button', { class: 'viewing-decision-option approve', type: 'button' }, icon('check'), 'Approve');
    const deny = el('button', { class: 'viewing-decision-option deny', type: 'button' }, icon('x'), 'Deny');
    const decide = async (decision) => {
      approve.disabled = true;
      deny.disabled = true;
      decisionStatus.textContent = 'Saving…';
      try {
        const refreshed = await api('actions/viewing-decision', { contact_id: lead.id, decision });
        mergeLead(refreshed);
        closeMenus();
        toast(decision === 'approve' ? 'Viewing approved' : 'Viewing denied', 'success');
      } catch (err) {
        approve.disabled = false;
        deny.disabled = false;
        decisionStatus.textContent = err.message;
        toast(err.message, 'error');
      }
    };
    approve.addEventListener('click', (event) => { event.stopPropagation(); decide('approve'); });
    deny.addEventListener('click', (event) => { event.stopPropagation(); decide('deny'); });
    items.push(el('div', { class: 'viewing-decision' },
      el('div', { class: 'viewing-decision-head' }, el('span', { text: 'Viewing request' }), decisionStatus),
      el('div', { class: 'viewing-decision-toggle', role: 'group', 'aria-label': `Decide viewing request for ${nameOf(lead)}` }, approve, deny)));
    items.push(el('div', { class: 'menu-sep' }));
  }
  items.push(el('button', { class: 'menu-item', onclick: () => { closeMenus(); openAssignModal(lead); } }, icon('building'), 'Assign property'));
  items.push(el('div', { class: 'menu-sep' }));
  items.push(el('button', { class: 'menu-item', onclick: () => { closeMenus(); copyEmail(lead); } }, icon('copy'), 'Copy email'));
  items.push(el('div', { class: 'menu-sep' }));
  items.push(el('button', { class: 'menu-item danger', onclick: () => { closeMenus(); confirmDeleteLead(lead); } }, icon('x'), 'Delete lead'));

  const menu = el('div', { class: 'menu', role: 'menu' }, items);
  document.body.append(menu);
  positionMenu(menu, anchor);
  const onDocClick = (e) => { if (!menu.contains(e.target) && e.target !== anchor) closeMenus(); };
  const onScroll = () => closeMenus();
  document.addEventListener('click', onDocClick, true);
  window.addEventListener('scroll', onScroll, true);
  menuCleanup = () => {
    document.removeEventListener('click', onDocClick, true);
    window.removeEventListener('scroll', onScroll, true);
  };
}

function updatePendingDeleteNotice() {
  const container = byId('pending-delete-notice');
  if (!container || !state.mode) return;
  let attempts = [];
  try { attempts = TveDeleteRecovery.pending(sessionStorage, state.mode); } catch { /* storage unavailable */ }
  container.replaceChildren(...attempts.map((attempt) => el('div', { class: 'panel panel-pad', role: 'status' },
    el('span', { text: `Deletion attempt for lead #${attempt.leadId} needs retry or review. ` }),
    el('button', { class: 'btn btn-ghost btn-sm', onclick: () => {
      const lead = state.leads.find((item) => Number(item.id) === attempt.leadId && item.contact_uid === attempt.contactUid)
        || { id: attempt.leadId, contact_uid: attempt.contactUid, first_name: `Lead #${attempt.leadId}` };
      confirmDeleteLead(lead, true);
    } }, 'Retry deletion'))));
}

function confirmDeleteLead(lead, recovery = false) {
  const error = el('div', { class: 'modal-error' });
  const confirm = el('button', { class: 'btn btn-danger' }, recovery ? 'Retry deletion' : 'Delete lead');
  // Never mint a fresh operation for the recovery button. A missing contact
  // can only be reconciled using its original ID, UUID and operation reference.
  let pendingOperation = null;
  try { pendingOperation = TveDeleteRecovery.get(sessionStorage, state.mode, lead.id, lead.contact_uid); } catch { /* storage unavailable */ }
  if (recovery && !pendingOperation) { toast('Recovery reference unavailable in this tab.', 'error'); return; }
  if (!pendingOperation) {
    pendingOperation = { id: crypto.randomUUID(), at: new Date().toISOString(), contactUid: lead.contact_uid };
  }
  let pending = false;
  confirm.addEventListener('click', async () => {
    if (pending) return;
    pending = true;
    confirm.disabled = true;
    try {
      // Confirmation immediately submits with the dashboard's standard
      // authenticated API client. Preserve the operation reference for retries.
      let saved = false;
      try { saved = TveDeleteRecovery.save(sessionStorage, state.mode, lead.id, pendingOperation); } catch { /* storage unavailable */ }
      if (!saved && lead.contact_uid) throw new Error('Could not save the recovery reference in this tab. Enable session storage before deleting.');
      updatePendingDeleteNotice();
      await api(`leads/${encodeURIComponent(lead.id)}`, undefined, { method: 'DELETE',
        headers: { 'X-TVE-Delete-Operation': pendingOperation.id, 'X-TVE-Delete-Initiated-At': pendingOperation.at,
          'X-TVE-Delete-Contact-UID': pendingOperation.contactUid || '' } });
      try { TveDeleteRecovery.clear(sessionStorage, state.mode, lead.id); } catch { /* storage unavailable */ }
      updatePendingDeleteNotice();
      state.leads = state.leads.filter((item) => String(item.id) !== String(lead.id));
      state.starred.delete(String(lead.id));
      persistStarred();
      if (String(state.drawerId) === String(lead.id)) closeDrawer();
      closeModal();
      updateBadges();
      if (state.refreshPage) state.refreshPage();
      toast(`${nameOf(lead)} deleted`, 'success');
    } catch (err) {
      error.textContent = err.message;
      confirm.disabled = false;
      pending = false;
    }
  });
  openModal(recovery ? 'Retry pending lead deletion?' : 'Delete this lead?', [
    el('div', { class: 'modal-text', text: recovery
      ? `Retry the saved deletion for lead #${lead.id}? The contact may no longer appear in the lead list.`
      : `Delete ${nameOf(lead)} from the lead list? This action cannot be undone.` }),
    error,
  ], [el('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancel'), confirm]);
}

/* ═══ 16. Drawer (lead 360) ═══════════════════════════════════════════════ */

function openDrawer(id) {
  state.drawerId = id;
  const drawer = byId('drawer');
  const scrim = byId('drawer-scrim');
  scrim.hidden = false;
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  const lead = state.leads.find((l) => String(l.id) === String(id));
  if (lead) {
    state.drawerLead = lead;
    renderDrawerContent(lead, { detailError: null });
  } else {
    drawer.replaceChildren(el('div', { class: 'drawer-body' }, loadingState('Loading lead…')));
  }
  api(`leads/${id}`).then((detail) => {
    if (String(state.drawerId) !== String(id)) return;
    const base = state.leads.find((l) => String(l.id) === String(id)) || {};
    const merged = { ...base, ...detail };
    state.drawerLead = merged;
    renderDrawerContent(merged, { detailError: null });
  }).catch((err) => {
    if (String(state.drawerId) !== String(id)) return;
    if (err.status === 404) {
      state.drawerLead = null;
      renderDrawerNotFound();
    } else {
      const base = state.leads.find((l) => String(l.id) === String(id));
      if (base) renderDrawerContent(base, { detailError: err.message });
      else byId('drawer').replaceChildren(el('div', { class: 'drawer-body' }, el('div', { class: 'drawer-error' }, icon('alert'), 'Could not load lead details.', el('div', { class: 'section-note', text: err.message }))));
    }
  });
}

function closeDrawer() {
  state.drawerId = null;
  state.drawerLead = null;
  byId('drawer-scrim').hidden = true;
  const drawer = byId('drawer');
  drawer.classList.remove('open');
  drawer.setAttribute('aria-hidden', 'true');
}

function refreshDrawerIfOpen() {
  if (state.drawerId == null) return;
  const lead = state.leads.find((l) => String(l.id) === String(state.drawerId));
  if (!lead) return;
  const raw = state.drawerLead && state.drawerLead.raw_email_body != null ? state.drawerLead.raw_email_body : null;
  const merged = { ...lead, raw_email_body: raw };
  state.drawerLead = merged;
  renderDrawerContent(merged, { detailError: null });
}

function renderDrawerNotFound() {
  byId('drawer').replaceChildren(el('div', { class: 'drawer-body' },
    el('div', { class: 'drawer-error' }, icon('alert'), el('div', { text: 'Lead not found.' }),
      el('div', { class: 'section-note', text: 'It may have been removed. Close this panel and refresh the list.' }))));
}

function fieldGrid(fields, obj) {
  const grid = el('div', { class: 'field-grid' });
  for (const [key, label, kind] of fields) {
    const raw = obj ? obj[key] : null;
    let text = 'Not provided';
    let missing = true;
    if (raw != null && raw !== '') {
      missing = false;
      if (kind === 'time') text = fullTime(raw);
      else if (kind === 'optin') {
        const v = String(raw).toLowerCase();
        text = v === 'opt_in' ? 'Opted in' : v === 'opt_out' ? 'Opted out' : String(raw);
      } else if (typeof raw === 'object') text = JSON.stringify(raw);
      else text = String(raw);
    }
    grid.append(el('div', {},
      el('div', { class: 'field-label', text: label }),
      el('div', { class: 'field-value' + (missing ? ' missing' : ''), text })));
  }
  return grid;
}

function journeySection(lead) {
  const entries = [];
  const push = (label, time, extra, muted) => { entries.push({ label, time, extra, muted }); };
  push('Enquiry received', lead.enquiry && lead.enquiry.received_at ? lead.enquiry.received_at : lead.created_at, null, false);
  const qToken = lead.tokens && lead.tokens.questionnaire;
  if (qToken && qToken.created_at) push('Invitation sent', qToken.created_at, null, false);
  else if (stageIndexOf(lead) >= 1) push('Invitation sent', null, null, true);
  if (qToken && qToken.consumed_at) push('Questionnaire link opened', qToken.consumed_at, null, false);
  if (lead.questionnaire && lead.questionnaire.completed_at) push('Questionnaire completed', lead.questionnaire.completed_at, null, false);
  const bToken = lead.tokens && lead.tokens.booking;
  if (lead.status === 'booking_link_sent') push('Booking link sent', lead.updated_at, null, false);
  if (bToken && bToken.consumed_at) push('Booking link opened', bToken.consumed_at, null, false);
  if (lead.viewing_slot_start) {
    const extra = lead.viewing_slot_end ? `Slot ${fullTime(lead.viewing_slot_start)} – ${fullTime(lead.viewing_slot_end)}` : null;
    push('Viewing requested', lead.viewing_slot_start, extra, false);
  }
  if (lead.updated_at) push('Last updated', lead.updated_at, null, true);

  const timeline = el('div', { class: 'timeline' });
  for (const entry of entries) {
    const timeEl = el('div', { class: 'tl-time' }, entry.time ? relTime(entry.time) : 'time unavailable');
    if (entry.time) timeEl.title = fullTime(entry.time);
    timeline.append(el('div', { class: 'tl-item' + (entry.muted ? ' muted' : '') },
      el('div', { class: 'tl-label', text: entry.label }), timeEl,
      entry.extra ? el('div', { class: 'tl-time', text: entry.extra }) : null));
  }
  return el('div', { class: 'drawer-section' },
    el('h3', { class: 'drawer-section-title', text: 'Journey' }), timeline);
}

function propertySection(lead) {
  const sec = el('div', { class: 'drawer-section' }, el('h3', { class: 'drawer-section-title', text: 'Property' }));
  if (lead.property_id != null || lead.property_address) {
    const meta = [];
    if (lead.property_rent != null) meta.push(`Rent: ${typeof lead.property_rent === 'number' ? '£' + fmtNum(lead.property_rent) : lead.property_rent}`);
    if (lead.property_bedrooms != null) meta.push(`${lead.property_bedrooms} bedroom${Number(lead.property_bedrooms) === 1 ? '' : 's'}`);
    sec.append(el('div', { class: 'drawer-card' },
      el('div', { class: 'field-value' }, lead.property_address || 'Property linked'),
      meta.length ? el('div', { class: 'section-note', text: meta.join(' · ') }) : null));
    return sec;
  }
  sec.append(el('div', { class: 'drawer-card warn' },
    el('div', { class: 'warn-line' }, icon('alert'),
      el('div', {}, el('b', { text: 'No property matched' }),
        el('div', { class: 'section-note', text: 'This enquiry did not match a property. Assign one to keep the journey moving.' }))),
    el('div', { class: 'mt' },
      el('button', { class: 'btn btn-ghost btn-sm', onclick: () => openAssignModal(lead) }, icon('building'), 'Assign property'))));
  return sec;
}

function questionnaireSection(lead) {
  const sec = el('div', { class: 'drawer-section' }, el('h3', { class: 'drawer-section-title', text: 'Questionnaire answers' }));
  if (lead.questionnaire) sec.append(fieldGrid(QUESTIONNAIRE_FIELDS, lead.questionnaire));
  else sec.append(el('div', { class: 'drawer-card' }, el('span', { class: 'muted', text: 'Questionnaire not completed yet.' })));
  return sec;
}

function enquirySection(lead) {
  const sec = el('div', { class: 'drawer-section' }, el('h3', { class: 'drawer-section-title', text: 'Parsed enquiry' }));
  if (!lead.enquiry) {
    sec.append(el('div', { class: 'drawer-card' }, el('span', { class: 'muted', text: 'No parsed enquiry data available.' })));
    return sec;
  }
  sec.append(fieldGrid(ENQUIRY_FIELDS, lead.enquiry));
  const details = el('details', { class: 'raw mt-sm' }, el('summary', {}, icon('chevron'), 'Raw email body'));
  if (lead.raw_email_body != null) details.append(el('pre', { class: 'raw-body', text: lead.raw_email_body }));
  else details.append(el('div', { class: 'section-note', text: 'No raw email body available for this lead.' }));
  sec.append(details);
  return sec;
}

function tokenRow(purpose, token, link) {
  if (!token && !link) {
    return el('div', { class: 'token-row' },
      el('div', { class: 'token-main' }, el('div', { class: 'token-purpose', text: purpose }),
        el('div', { class: 'token-state', text: 'Not created yet' })));
  }
  let stateLine = '';
  let stateClass = 'token-state';
  if (token && token.consumed_at) { stateLine = `Opened ${relTime(token.consumed_at)}`; stateClass += ' ok'; }
  else if (token && token.expires_at) {
    const expired = new Date(token.expires_at).getTime() < Date.now();
    stateLine = `${expired ? 'Expired' : 'Expires'} ${relTime(token.expires_at)}`;
  } else stateLine = link ? 'Link available' : '';
  const stateEl = el('div', { class: stateClass, text: stateLine });
  if (token && token.expires_at) stateEl.title = `Expiry: ${fullTime(token.expires_at)}`;
  if (token && token.consumed_at) stateEl.title = `Consumed: ${fullTime(token.consumed_at)}`;
  return el('div', { class: 'token-row' },
    el('div', { class: 'token-main' }, el('div', { class: 'token-purpose', text: purpose }), stateEl),
    el('div', { class: 'token-actions' },
      link ? el('a', { class: 'btn btn-ghost btn-sm', href: link, target: '_blank', rel: 'noopener', 'aria-label': `Open ${purpose.toLowerCase()} link` }, icon('external'), 'Open') : null,
      link ? el('button', { class: 'icon-btn', 'aria-label': `Copy ${purpose.toLowerCase()} link`, title: 'Copy link', onclick: async () => { const ok = await copyText(link); toast(ok ? 'Link copied to clipboard' : 'Could not access the clipboard', ok ? 'success' : 'error'); } }, icon('copy')) : null));
}

function tokensSection(lead) {
  const tokens = lead.tokens || {};
  const links = lead.links || {};
  return el('div', { class: 'drawer-section' },
    el('h3', { class: 'drawer-section-title', text: 'Links & tokens' }),
    el('div', { class: 'drawer-card' },
      tokenRow('Questionnaire', tokens.questionnaire, links.questionnaire),
      tokenRow('Booking', tokens.booking, links.booking)));
}

function flagsSection(lead) {
  const flags = lead.flags || [];
  const sec = el('div', { class: 'drawer-section' }, el('h3', { class: 'drawer-section-title', text: 'Flags' }));
  if (!flags.length) { sec.append(el('span', { class: 'muted', text: 'No flags on this lead.' })); return sec; }
  sec.append(el('div', { class: 'drawer-flags' }, flags.map((f) => {
    const meta = flagMeta(f);
    return el('span', { class: `chip chip-flag f-${meta.tone}`, title: meta.tip }, meta.label);
  })));
  return sec;
}

function renderDrawerContent(lead, opts) {
  const drawer = byId('drawer');
  const starred = isStarred(lead);
  const closeBtn = el('button', { class: 'icon-btn', 'aria-label': 'Close lead details', title: 'Close (Esc)', onclick: closeDrawer }, icon('x'));
  const starBtn = el('button', {
    class: 'icon-btn star-btn' + (starred ? ' starred' : ''),
    'aria-label': starred ? 'Unstar lead' : 'Star lead', title: starred ? 'Remove from My leads' : 'Add to My leads',
    onclick: () => toggleStar(lead),
  }, icon('star'));

  const head = el('div', { class: 'drawer-head' },
    el('div', { class: 'drawer-top' },
      el('div', { class: 'drawer-id' },
        el('div', { class: 'initials' }, initialsOf(lead)),
        el('div', {}, el('div', { class: 'drawer-name', text: nameOf(lead) }), el('div', { class: 'drawer-email', text: lead.email || 'No email address' }))),
      el('div', { class: 'drawer-tags' }, starBtn, closeBtn)),
    el('div', { class: 'drawer-meta' }, stagePill(lead.status), scoreChip(lead.score), el('span', { class: 'muted', text: `Lead ${lead.id}` })),
    (lead.score && Array.isArray(lead.score.reasons) && lead.score.reasons.length)
      ? el('div', { class: 'score-reasons' }, lead.score.reasons.map((r) => el('div', { class: 'score-reason', text: r }))) : null);

  const body = el('div', { class: 'drawer-body' });
  if (opts && opts.detailError) {
    body.append(el('div', { class: 'drawer-card warn', style: 'margin-bottom:14px' },
      el('div', { class: 'warn-line' }, icon('alert'), el('span', { text: `Details could not be refreshed — ${opts.detailError}` }))));
  }
  body.append(journeySection(lead), propertySection(lead), questionnaireSection(lead), enquirySection(lead), tokensSection(lead), flagsSection(lead));

  const foot = el('div', { class: 'drawer-actions' },
    el('button', { class: 'btn btn-ghost', onclick: () => openAssignModal(lead) }, icon('building'), 'Assign property'),
    el('button', { class: 'btn btn-ghost', onclick: () => copyEmail(lead) }, icon('copy'), 'Copy email'));

  drawer.replaceChildren(head, body, foot);
  closeBtn.focus();
}

/* ═══ 17. Modals ══════════════════════════════════════════════════════════ */

let modalCloseHandler = null;

function openModal(title, bodyNodes, footNodes) {
  if (!byId('modal').hidden) closeModal();
  const modal = byId('modal');
  modal.setAttribute('aria-label', title);
  modal.replaceChildren(
    el('div', { class: 'modal-head' },
      el('span', { class: 'modal-title', text: title }),
      el('button', { class: 'icon-btn', 'aria-label': 'Close dialog', title: 'Close (Esc)', onclick: closeModal }, icon('x'))),
    el('div', { class: 'modal-body' }, bodyNodes || []),
    footNodes ? el('div', { class: 'modal-foot' }, footNodes) : null);
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
  byId('modal-scrim').hidden = false;
}

function closeModal() {
  const modal = byId('modal');
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
  byId('modal-scrim').hidden = true;
  const onClose = modalCloseHandler;
  modalCloseHandler = null;
  if (onClose) onClose();
  modal.replaceChildren();
}

function propertyOptionLabel(p) {
  const parts = [propertyLabel(p)];
  const price = propertyPriceText(p);
  if (price) parts.push(price);
  if (p.bedrooms != null) parts.push(`${p.bedrooms} bed`);
  return parts.join(' · ');
}

function openAssignModal(lead) {
  const select = el('select', { class: 'input', 'aria-label': 'Property' }, el('option', { value: '', text: 'Loading properties…' }));
  const errEl = el('div', { class: 'modal-error' });
  const confirm = el('button', { class: 'btn btn-primary' }, icon('building'), 'Assign property');
  let pending = false;

  async function submit() {
    if (pending) return;
    const pid = select.value;
    if (!pid) { errEl.textContent = 'Choose a property first.'; return; }
    pending = true;
    confirm.disabled = true;
    try {
      const payload = { contact_id: lead.id, property_id: /^\d+$/.test(pid) ? Number(pid) : pid };
      const data = await api('actions/assign-property', payload);
      const refreshed = data && typeof data === 'object' && data.id != null ? data : (data && data.lead) || null;
      if (refreshed) mergeLead(refreshed);
      closeModal();
      toast('Property assigned', 'success');
    } catch (err) {
      errEl.textContent = err.message;
      toast(err.message, 'error');
    } finally {
      pending = false;
      confirm.disabled = false;
    }
  }
  confirm.addEventListener('click', submit);

  ensureProperties(true).then(() => {
    if (!state.properties.length) {
      select.replaceChildren(el('option', { value: '', text: state.propertiesError ? 'Properties unavailable' : 'No properties available' }));
      select.disabled = true;
      if (state.propertiesError) errEl.textContent = state.propertiesError;
      return;
    }
    select.replaceChildren(...state.properties.map((p) => {
      const option = el('option', { value: String(p.id) }, propertyOptionLabel(p));
      if (leadPropertyId(lead) != null && String(leadPropertyId(lead)) === String(p.id)) option.selected = true;
      return option;
    }));
  });

  openModal('Assign property', [
    el('div', { class: 'modal-text' }, `Choose the property to link to ${nameOf(lead)}. The lead's journey continues from there.`),
    el('div', { class: 'modal-field' }, el('span', { class: 'modal-label', text: 'Property' }), select),
    errEl,
  ], [el('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancel'), confirm]);
  setTimeout(() => select.focus(), 60);
}

/* ═══ 18. Toasts & actions ════════════════════════════════════════════════ */

function toast(message, kind) {
  const node = el('div', { class: `toast ${kind || 'success'}` },
    icon(kind === 'error' ? 'alert' : 'check'), el('span', { text: message }));
  byId('toasts').append(node);
  setTimeout(() => {
    node.classList.add('out');
    setTimeout(() => node.remove(), 240);
  }, 4000);
}

function mergeLead(refreshed) {
  if (!refreshed || refreshed.id == null) return;
  const key = String(refreshed.id);
  const index = state.leads.findIndex((l) => String(l.id) === key);
  if (index >= 0) state.leads[index] = { ...state.leads[index], ...refreshed };
  else state.leads.push(refreshed);
  updateBadges();
  if (state.refreshPage) state.refreshPage();
  refreshDrawerIfOpen();
}

async function copyEmail(lead) {
  if (!lead || !lead.email) { toast('No email address to copy', 'error'); return; }
  const ok = await copyText(lead.email);
  toast(ok ? 'Email copied to clipboard' : 'Could not access the clipboard', ok ? 'success' : 'error');
}

function toggleStar(lead) {
  const key = String(lead.id);
  if (state.starred.has(key)) state.starred.delete(key);
  else state.starred.add(key);
  persistStarred();
  updateBadges();
  if (state.refreshPage) state.refreshPage();
  refreshDrawerIfOpen();
}

/* ═══ 19. Polling ═════════════════════════════════════════════════════════ */

async function pollLeads() {
  let ok = true;
  let message = null;
  try {
    const data = await api('leads');
    state.leads = Array.isArray(data && data.leads) ? data.leads : [];
    state.leadsGeneratedAt = (data && data.generated_at) || null;
    state.leadsLoaded = true;
  } catch (err) {
    ok = false;
    message = err.message;
  }
  markPoll(ok, message);
  updatePendingDeleteNotice();
  updateBadges();
  if (state.refreshPage) state.refreshPage();
}

async function pollModeBadge() {
  const badge = byId('mode-badge');
  if (!badge) return;
  try {
    const mode = await api('settings/mode', undefined, { cache: 'no-store' });
    if (state.mode && state.mode !== mode.mode) { window.location.reload(); return; }
    if (!state.mode) state.starred = loadStarred(mode.mode);
    state.mode = mode.mode;
    updatePendingDeleteNotice();
    badge.hidden = false;
    badge.className = `mode-badge ${mode.sandbox ? 'is-sandbox' : 'is-production'}`;
    badge.textContent = mode.sandbox ? 'Sandbox · Mailpit' : 'Production';
    badge.title = `Mode source: ${mode.source}`;
  } catch {
    badge.hidden = true;
  }
}

async function pollOverview() {
  if (state.route.page !== 'overview' || document.visibilityState !== 'visible') return;
  const [metricsRes, funnelRes] = await Promise.allSettled([api('metrics'), api('funnel')]);
  if (state.route.page !== 'overview') return;
  let ok = true;
  let message = null;
  if (metricsRes.status === 'fulfilled') state.metrics = metricsRes.value;
  else { ok = false; message = metricsRes.reason.message; }
  if (funnelRes.status === 'fulfilled') { state.funnel = funnelRes.value; state.funnelError = null; }
  else { ok = false; message = message || funnelRes.reason.message; state.funnelError = funnelRes.reason.message; }
  state.overviewAt = Date.now();
  markPoll(ok, message);
  if (state.refreshPage) state.refreshPage();
}

/* ═══ Settings — candidates live only in this page's input elements ═══════ */

const SECRET_SETTING_KEYS = new Set(['ACQUAINT_API_KEY', 'ACQUAINT_SITE_PREFIX', 'TVE_IMAP_PASSWORD', 'TVE_SMTP_PASSWORD', 'TVE_TOKEN_KEY', 'TVE_WEBHOOK_SECRET']);
const ROTATION_KEYS = new Set(['TVE_TOKEN_KEY', 'TVE_WEBHOOK_SECRET']);

function settingMetadata(raw) {
  const field = { ...raw, is_secret: raw.is_secret || SECRET_SETTING_KEYS.has(raw.key) };
  if (field.is_secret) delete field.value;
  return field;
}

function confirmSettingRotation(field, action) {
  return new Promise((resolve) => {
    const token = field.key === 'TVE_TOKEN_KEY';
    const cancel = el('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancel');
    const confirm = el('button', { class: 'btn btn-danger', onclick: () => { resolve(true); closeModal(); } }, action === 'clear' ? 'Clear override' : 'Save new key');
    openModal(`${action === 'clear' ? 'Clear' : 'Rotate'} ${field.label}`, [
      el('div', { class: 'modal-text', text: action === 'clear'
        ? 'Clearing removes the database override and restores the environment value, or leaves this key unset if none exists.'
        : 'Saving replaces the effective key with the value you entered.' }),
      el('div', { class: 'warn-line' }, icon('alert'), token
        ? 'Changing the token key invalidates outstanding questionnaire and booking links. Applicants will need new links.'
        : 'To complete webhook secret rotation, update TVE_WEBHOOK_SECRET in backend/.env to the effective key and restart the frontend by recreating it with Compose --env-file backend/.env. Recreate the backend too to retire the old bootstrap secret. The proxy reads this secret only at startup.'),
    ], [cancel, confirm]);
    const modal = byId('modal');
    const trap = (event) => {
      if (event.key !== 'Tab') return;
      const buttons = [...modal.querySelectorAll('button')];
      if (event.shiftKey && document.activeElement === buttons[0]) { event.preventDefault(); buttons.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === buttons.at(-1)) { event.preventDefault(); buttons[0].focus(); }
    };
    modal.addEventListener('keydown', trap);
    modalCloseHandler = () => { modal.removeEventListener('keydown', trap); resolve(false); };
    cancel.focus();
  });
}

function confirmModeChange(sandbox) {
  return new Promise((resolve) => {
    const cancel = el('button', { class: 'btn btn-ghost', onclick: closeModal }, 'Cancel');
    const confirm = el('button', { class: sandbox ? 'btn btn-primary' : 'btn btn-danger', onclick: () => { resolve(true); closeModal(); } }, sandbox ? 'Switch to Sandbox' : 'Turn Production on');
    openModal(sandbox ? 'Switch to Sandbox mode?' : 'Turn Production on?', [
      el('div', { class: 'modal-text', text: sandbox
        ? 'Live intake pauses. The dashboard and test journeys switch to separate sandbox databases. Emails go to Mailpit; real Acquaint properties and appointments remain read-only.'
        : 'Live mailbox intake resumes and customer emails will be sent through the production SMTP account.' }),
      el('div', { class: 'warn-line' }, icon('alert'), 'This change applies to the running API and mail worker without a restart.'),
    ], [cancel, confirm]);
    const modal = byId('modal');
    const trap = (event) => {
      if (event.key !== 'Tab') return;
      const buttons = [...modal.querySelectorAll('button')];
      if (event.shiftKey && document.activeElement === buttons[0]) { event.preventDefault(); buttons.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === buttons.at(-1)) { event.preventDefault(); buttons[0].focus(); }
    };
    modal.addEventListener('keydown', trap);
    modalCloseHandler = () => { modal.removeEventListener('keydown', trap); resolve(false); };
    cancel.focus();
  });
}

function renderSettingsPage(view) {
  let active = true;
  let busy = false;
  let unlocked = false;
  let mode = null;
  let built = false;
  const controller = new AbortController();
  const fields = new Map();
  const groups = new Map();
  const sections = { integrations: ['Integrations', 'Connect your property data, email and agent.'], members: ['Team members', 'Manage access to your workspace.'], profile: ['Profile', 'Your personal account settings.'], security: ['Security', 'Manage signing keys and webhook authentication.'], workspace: ['Workspace', 'Choose the environment your workspace uses.'] };
  const params = currentRoute().params;
  const section = Object.hasOwn(sections, params.get('section')) ? params.get('section') : 'integrations';
  const selectedGroup = params.get('integration');
  const head = pageHead('Settings', 'Workspace preferences');
  const reload = el('button', { class: 'btn btn-ghost', onclick: load }, icon('refresh'), 'Refresh settings');
  head.append(reload);
  const vault = el('div', { class: 'settings-vault', role: 'status' });
  const notice = el('div', { class: 'settings-result', role: 'status', 'aria-live': 'polite' });
  const summary = el('div', { class: 'settings-summary', role: 'status', 'aria-live': 'polite' });
  const cards = el('div', { class: 'settings-grid' });
  const modeCard = el('section', { class: 'panel settings-card mode-card', 'aria-label': 'Sandbox and production mode' });
  const modeResult = el('div', { class: 'settings-result', role: 'status', 'aria-live': 'polite' });
  const modeTitle = el('h2', { text: 'Sandbox / Production' });
  const modeState = el('span', { class: 'setting-state is-unset', text: 'Loading mode…' });
  const modeSource = el('div', { class: 'settings-current', text: 'Sandbox uses separate test databases and Mailpit. Acquaint properties and appointments stay live and read-only.' });
  const seedButton = el('button', { class: 'btn btn-ghost', type: 'button', onclick: seedData }, 'Load test data');
  const modeToggle = el('button', { class: 'mode-toggle', type: 'button', 'aria-pressed': 'false', onclick: changeMode }, 'Loading…');
  modeCard.append(el('div', { class: 'settings-group-head' }, modeTitle, modeState),
    el('div', { class: 'mode-control' }, modeSource, modeToggle), seedButton, modeResult);
  const navigation = el('nav', { class: 'settings-nav', 'aria-label': 'Settings sections' },
    el('div', { class: 'settings-nav-caption', text: 'SETTINGS' }),
    ...Object.entries(sections).map(([id, [label]]) => el('a', {
      href: `#/settings?section=${id}`, class: `settings-nav-link${section === id ? ' is-active' : ''}`,
      'aria-current': section === id ? 'page' : null, text: label,
    })));
  const content = el('section', { class: 'settings-content' },
    el('div', { class: 'settings-section-heading' }, el('h2', { text: sections[section][0] }),
      el('p', { text: sections[section][1] })), notice);
  const directory = el('div', { class: 'settings-directory' });
  if (section === 'integrations') content.append(directory);
  if (section === 'workspace') content.append(modeCard);
  if (['integrations', 'security'].includes(section)) content.append(cards);
  if (['members', 'profile'].includes(section)) {
    content.append(el('div', { class: 'panel settings-empty' },
      el('span', { class: 'setting-state is-unset', text: 'Not available yet' }),
      el('h3', { text: section === 'members' ? 'Team access' : 'Personal profile' }),
      el('p', { text: section === 'members' ? 'Member invitations and roles are not connected to this dashboard yet.' : 'This dashboard does not yet have individual user profiles.' })));
    reload.hidden = true;
  } else {
    content.append(el('details', { class: 'settings-storage' }, el('summary', { text: 'Configuration status' }), summary, vault));
  }
  view.append(head, el('div', { class: 'settings-layout' }, navigation, content));

  // Lead polls keep updating the shell, but must never rebuild credential inputs.
  state.refreshPage = () => {};
  state.reloadSettings = load;
  const cleanup = () => {
    active = false;
    controller.abort();
    if (modalCloseHandler) closeModal();
    for (const ref of fields.values()) ref.input.value = '';
    fields.clear();
    groups.clear();
    state.settingsCleanup = null;
    state.reloadSettings = null;
    window.removeEventListener('pagehide', cleanup);
  };
  state.settingsCleanup = cleanup;
  window.addEventListener('pagehide', cleanup);

  function changed(ref) {
    return ref.field.is_secret ? ref.input.value !== '' : ref.input.value !== String(ref.field.value ?? '');
  }

  function syncControls() {
    reload.disabled = busy;
    vault.replaceChildren(icon('lock'), el('span', { class: `chip ${unlocked ? 'chip-high' : 'chip-medium'}`, text: unlocked ? 'Vault unlocked' : 'Vault locked' }),
      el('span', { text: unlocked ? 'Changes take effect through the backend settings store.' : 'Environment values are readable; saving and clearing require an unlocked backend vault.' }));
    for (const ref of fields.values()) {
      ref.input.disabled = busy || !unlocked || ref.field.readonly;
      ref.save.hidden = !changed(ref) || ref.input.value === '';
      ref.save.disabled = busy || !unlocked || ref.field.readonly || !changed(ref) || ref.input.value === '';
      ref.clear.hidden = ref.field.source !== 'database';
      ref.clear.disabled = busy || !unlocked || ref.field.readonly || ref.field.source !== 'database';
    }
    for (const group of groups.values()) if (group.test) group.test.disabled = busy;
    modeToggle.disabled = busy || !unlocked || !mode;
    seedButton.hidden = !mode?.sandbox;
    seedButton.disabled = busy || !mode?.sandbox;
    modeToggle.setAttribute('aria-pressed', String(!!mode?.sandbox));
    modeToggle.classList.toggle('is-sandbox', !!mode?.sandbox);
    modeToggle.textContent = mode ? (mode.sandbox ? 'Switch to Production' : 'Switch to Sandbox') : 'Mode unavailable';
    modeState.className = `setting-state ${mode ? (mode.sandbox ? 'is-unset' : 'is-set') : 'is-unset'}`;
    modeState.textContent = mode ? `${mode.mode[0].toUpperCase()}${mode.mode.slice(1)} · ${mode.source}` : 'Mode unavailable';
    const configured = [...fields.values()].filter(({ field }) => field.set).length;
    summary.replaceChildren(...[
      el('span', { class: 'summary-value', text: `${configured}/${fields.size || 0}` }),
      el('span', { text: 'fields configured' }),
      mode ? el('span', { class: `summary-mode ${mode.sandbox ? 'is-sandbox' : 'is-production'}`, text: mode.sandbox ? 'Sandbox active' : 'Production active' }) : null,
      !unlocked ? el('span', { class: 'summary-warning', text: 'Editing locked' }) : null].filter(Boolean));
    for (const [id, group] of groups) {
      if (!group.status) continue;
      const entries = [...fields.values()].filter(ref => ref.groupId === id);
      const count = entries.filter(ref => ref.field.set).length;
      group.status.textContent = `${count}/${entries.length} configured`;
    }
  }

  async function changeMode() {
    if (!active || busy || !unlocked || !mode) return;
    const sandbox = !mode.sandbox;
    if (!await confirmModeChange(sandbox) || !active) return;
    busy = true;
    modeResult.className = 'settings-result';
    modeResult.textContent = 'Changing mode…';
    syncControls();
    try {
      mode = await api('settings/mode', { sandbox }, { method: 'PUT', cache: 'no-store', signal: controller.signal });
      window.location.reload();
      modeResult.className = 'settings-result success';
      modeResult.textContent = mode.sandbox ? 'Sandbox is active. Test data is isolated and email goes to Mailpit. Use Load test data to add example leads.' : 'Production is active. Live IMAP intake and production SMTP are enabled.';
    } catch (err) {
      modeResult.className = 'settings-result error';
      modeResult.textContent = err.message;
    } finally {
      busy = false;
      if (active) syncControls();
    }
  }

  function updateMetadata(ref, raw, resetInput = false) {
    ref.field = settingMetadata(raw);
    const field = ref.field;
    ref.audit.textContent = field.updated_at ? `Updated ${fullTime(field.updated_at)}${field.updated_via ? ` · via ${field.updated_via}` : ''}` : 'No database update recorded';
    ref.current.textContent = field.readonly ? 'Shared production connection: read-only in Sandbox.' : field.is_secret ? 'Stored secrets are never displayed.' : `Effective value: ${field.value ?? '—'}`;
    if (resetInput) ref.input.value = field.is_secret ? '' : String(field.value ?? '');
  }

  function build(data) {
    cards.replaceChildren();
    directory.replaceChildren();
    const descriptions = { acquaint: 'Acquaint property catalogue and availability.', imap: 'Receive enquiries from your inbox.', smtp: 'Send questionnaires and viewing invitations.', agent: 'Configure your automation connection and model.' };
    const integrationNames = { acquaint: 'Acquaint', agent: 'N8N' };
    const selected = data.groups.find(group => group.id === selectedGroup && group.id !== 'security');
    if (section === 'integrations' && selected) {
      directory.append(el('a', { class: 'settings-back', href: '#/settings?section=integrations', text: '← All integrations' }));
    }
    for (const group of data.groups) {
      const displayLabel = integrationNames[group.id] || group.label;
      const result = el('div', { class: 'settings-result', role: 'status', 'aria-live': 'polite' });
      const groupHead = el('div', { class: 'settings-group-head' }, el('h2', { text: displayLabel }));
      const test = ['acquaint', 'imap', 'smtp'].includes(group.id)
        ? el('button', { class: 'btn btn-ghost btn-sm', onclick: () => testGroup(group.id) }, 'Test connection') : null;
      if (test) groupHead.append(test);
      const card = el('section', { class: 'panel settings-card', 'aria-label': displayLabel }, groupHead);
      const status = el('span', { class: 'settings-directory-status' });
      groups.set(group.id, { result, test, status });
      if (section === 'integrations' && !selected && group.id !== 'security') {
        directory.append(el('a', { class: 'settings-directory-row', href: `#/settings?section=integrations&integration=${encodeURIComponent(group.id)}` },
          el('span', { class: 'settings-integration-mark', 'aria-hidden': 'true' },
            group.id === 'agent' ? el('img', { src: 'assets/n8n-logo.svg', alt: '', width: 32, height: 26 })
              : group.id === 'acquaint' ? el('img', { src: 'assets/acquaint-logo.png', alt: '', width: 30, height: 30 })
              : ({ imap: '↓', smtp: '↑' })[group.id] || displayLabel.charAt(0)),
          el('span', { class: 'settings-directory-copy' }, el('strong', { text: displayLabel }), el('span', { text: descriptions[group.id] || 'Manage this connection.' })),
          status, el('span', { class: 'settings-directory-arrow', 'aria-hidden': 'true', text: '›' })));
      }
      card.append(el('p', { class: 'settings-intro', text: group.id === 'security' ? 'Replace keys only when needed. Existing secrets are never displayed.' : 'Update the fields you need, then save each change. Stored secrets stay hidden.' }));
      for (const raw of group.fields) {
        const field = settingMetadata(raw);
        const input = el('input', {
          id: `setting-${field.key}`, class: 'input', type: field.is_secret ? 'password' : 'text',
          autocomplete: field.is_secret ? 'new-password' : 'off', autocapitalize: 'none', spellcheck: 'false',
          placeholder: field.is_secret ? 'Enter a new secret to replace the current one' : (field.placeholder || 'Enter a value'),
          'aria-describedby': `setting-help-${field.key}`,
        });
        const save = el('button', { class: 'btn btn-primary btn-sm', onclick: () => writeField(field.key, 'save'), 'aria-label': `Save ${field.label}` }, 'Save');
        const clear = el('button', { class: 'btn btn-ghost btn-sm', onclick: () => writeField(field.key, 'clear'), 'aria-label': `Clear ${field.label} override`, title: 'Revert to environment or unset' }, 'Clear');
        const ref = {
          field, groupId: group.id, input, save, clear,
          audit: el('div', { class: 'settings-audit' }),
          current: el('div', { class: 'settings-current', id: `setting-help-${field.key}` }),
          details: el('details', { class: 'setting-details' }, el('summary', { text: 'Details' })),
          result: el('div', { class: 'settings-result', role: 'status', 'aria-live': 'polite' }),
        };
        ref.details.append(el('div', { class: 'setting-details-body' },
          el('code', { class: 'setting-key', text: field.key }), ref.current, ref.audit));
        fields.set(field.key, ref);
        updateMetadata(ref, field, true);
        input.addEventListener('input', () => {
          ref.result.textContent = '';
          result.textContent = '';
          syncControls();
        });
        card.append(el('div', { class: 'setting-row' },
          el('div', { class: 'setting-field-head' }, el('label', { class: 'setting-label', for: input.id, text: field.label })),
          el('div', { class: 'setting-editor' }, input, save, clear), ref.details, ref.result));
      }
      card.append(result);
      if ((section === 'security' && group.id === 'security') || (section === 'integrations' && selected?.id === group.id)) cards.append(card);
    }
    built = true;
  }

  async function load() {
    if (busy || !active) return;
    busy = true;
    notice.className = 'settings-result';
    notice.textContent = 'Loading settings…';
    syncControls();
    try {
      const [data, modeData] = await Promise.all([
        api('settings', undefined, { cache: 'no-store', signal: controller.signal }),
        api('settings/mode', undefined, { cache: 'no-store', signal: controller.signal }),
      ]);
      if (!active) return;
      if (!data || typeof data.vault_unlocked !== 'boolean' || !Array.isArray(data.groups) || !data.groups.every((g) => Array.isArray(g.fields))) throw new Error('The settings response was unreadable.');
      unlocked = data.vault_unlocked;
      if (modeData && typeof modeData.sandbox === 'boolean' && ['production', 'sandbox'].includes(modeData.mode)) mode = modeData;
      if (!built) build(data);
      else for (const group of data.groups) for (const field of group.fields) {
        const ref = fields.get(field.key);
        if (ref) {
          const dirty = changed(ref);
          updateMetadata(ref, field, !dirty);
        }
      }
      notice.textContent = '';
    } catch (err) {
      if (!active) return;
      unlocked = false;
      notice.className = 'settings-result error';
      notice.textContent = `${err.message} Use Refresh settings to retry.`;
    } finally {
      busy = false;
      if (active) syncControls();
    }
  }

  async function writeField(key, action) {
    const ref = fields.get(key);
    if (!active || busy || !unlocked || !ref || (action === 'save' && ref.save.disabled) || (action === 'clear' && ref.clear.disabled)) return;
    busy = true;
    syncControls();
    try {
      if (ROTATION_KEYS.has(key) && !await confirmSettingRotation(ref.field, action)) return;
      if (!active) return;
      ref.result.className = 'settings-result';
      ref.result.textContent = action === 'clear' ? 'Clearing override…' : 'Saving…';
      const data = await api(`settings/${encodeURIComponent(key)}`, action === 'save' ? { value: ref.input.value } : undefined,
        { method: action === 'save' ? 'PUT' : 'DELETE', cache: 'no-store', signal: controller.signal });
      if (!active) return;
      if (!data || data.key !== key || typeof data.is_secret !== 'boolean') throw new Error('The backend returned unreadable field metadata. Refresh settings before retrying.');
      updateMetadata(ref, data, true);
      if (ref.groupId === 'acquaint') {
        state.properties = [];
        state.propertiesAt = null;
        state.propertiesError = null;
      }
      ref.result.className = 'settings-result success';
      ref.result.textContent = action === 'clear' ? 'Override cleared; environment fallback restored.' : 'Saved.';
      groups.get(ref.groupId).result.textContent = '';
    } catch (err) {
      if (!active) return;
      if (err.status === 503) unlocked = false;
      ref.result.className = 'settings-result error';
      ref.result.textContent = err.message;
    } finally {
      busy = false;
      if (active) { syncControls(); ref.input.focus(); }
    }
  }

  async function testGroup(id) {
    if (!active || busy) return;
    const group = groups.get(id);
    const values = {};
    for (const [key, ref] of fields) if (ref.groupId === id && changed(ref)) values[key] = ref.input.value;
    busy = true;
    group.result.className = 'settings-result';
    group.result.textContent = 'Testing connection with unsaved edits…';
    syncControls();
    try {
      const data = await api(`settings/test/${id}`, { values }, { cache: 'no-store', signal: controller.signal });
      if (!active) return;
      if (typeof data.ok !== 'boolean' || typeof data.message !== 'string') throw new Error('The backend returned an unreadable connection-test result.');
      group.result.className = `settings-result ${data.ok ? 'success' : 'error'}`;
      group.result.textContent = `${data.ok ? 'Passed' : 'Failed'}: ${data.message}`;
    } catch (err) {
      if (!active) return;
      group.result.className = 'settings-result error';
      group.result.textContent = err.message;
    } finally {
      // Do not retain candidate values after the request has completed.
      for (const key of Object.keys(values)) delete values[key];
      busy = false;
      if (active) syncControls();
    }
  }

  async function seedData() {
    if (!active || busy || !mode?.sandbox) return;
    busy = true;
    syncControls();
    modeResult.textContent = 'Loading synthetic leads…';
    try {
      const result = await api('settings/sandbox/seed', {}, { cache: 'no-store', signal: controller.signal });
      if (!active) return;
      modeResult.className = 'settings-result success';
      modeResult.textContent = `${result.added} test leads added. Existing test journeys were preserved.`;
      pollLeads();
    } catch (error) {
      modeResult.className = 'settings-result error';
      modeResult.textContent = error.message;
    } finally {
      busy = false;
      if (active) syncControls();
    }
  }

  load();
}

/* ═══ 20. Agent panel ═════════════════════════════════════════════════════ */

// The Agent answers only from data the dashboard already holds: the polled lead
// list, the funnel breakdown and the metrics snapshot. The intent matcher is
// deterministic and client-side — no network beyond /sandbox/api, no invented numbers.

const AGENT_CAPABILITIES = [
  'needs attention — leads flagged for review, with a count per flag',
  'high intent — the strongest leads by score or booking opt-in',
  'stalled — anything stuck on a stalled flag, with how long since it moved',
  'funnel — the six journey stages with counts and the pipeline total',
  'metrics — totals, new leads, completion and viewing rates, upcoming viewings',
  'a property — type an address or street and I list the leads on that listing',
];

const AGENT_STOPWORDS = new Set(('a an the and or of in on at to for from with about into over under is are was were be been do does did how what which who whom where when why this that these those there their they them then than some more most many much any all my me our your you it its see show give list find tell please help can could would lead leads needs need want using use used stage stages journey pipeline funnel metrics metric kpi kpis summary total totals count counts number numbers rate rates stats statistic property properties address listing attention review flagged flag stalled high highest intent score scored').split(' '));

// Generic street suffixes are ignored when matching a property, so "Radstock Road"
// does not drag in every other "… Road" listing.
const AGENT_STREET_WORDS = new Set(('road street lane avenue close drive way court place crescent grove park gardens terrace mews hill rise view walk row square green vale grove').split(' '));

let agentThreadEl = null;
let agentInput = null;
let agentSendBtn = null;
let agentWrapEl = null;
let agentModelSelect = null;
let agentSessionId = null;

async function ensureAgentStats(force) {
  if (!force && state.agentStatsAt && Date.now() - state.agentStatsAt < 60000 && state.metrics && state.funnel) return;
  const [metricsRes, funnelRes] = await Promise.allSettled([api('metrics'), api('funnel')]);
  if (metricsRes.status === 'fulfilled') state.metrics = metricsRes.value;
  if (funnelRes.status === 'fulfilled') { state.funnel = funnelRes.value; state.funnelError = null; }
  else state.funnelError = funnelRes.reason.message;
  state.agentStatsAt = Date.now();
}

function agentChip(lead, meta) { return { id: lead.id, label: nameOf(lead), meta: meta || agentLeadMeta(lead) }; }

function agentLeadMeta(lead) {
  const bits = [];
  if (lead.score && lead.score.value != null) bits.push(`score ${lead.score.value}`);
  bits.push(stageMeta(lead.status).label);
  return bits.join(' · ');
}

function agentLeadChip(chip) {
  return el('button', {
    class: 'agent-lead-chip',
    title: chip.meta ? `${chip.label} · ${chip.meta}` : chip.label,
    onclick: () => openDrawer(chip.id),
  },
    el('span', { class: 'agent-chip-name', text: chip.label }),
    chip.meta ? el('span', { class: 'agent-chip-meta', text: chip.meta }) : null);
}

function agentNoData() {
  return {
    text: state.leadsLoaded
      ? 'The last leads poll came back empty, so I have nothing to work from.'
      : 'I have no lead data yet — the dashboard is still connecting to the backend.',
    bullets: ['I never invent numbers. Open Leads to review the available records or retry the poll.'],
  };
}

function agentHelpAnswer() {
  return {
    text: 'I answer from the data this dashboard has already loaded — the lead list, the journey funnel and the metrics snapshot. Ask me about:',
    bullets: AGENT_CAPABILITIES,
  };
}

function agentFallbackAnswer() {
  return {
    text: "I couldn't map that to anything I can answer, and I won't guess at it. Here is what I can answer from the live data:",
    bullets: AGENT_CAPABILITIES,
  };
}

function agentAttentionAnswer() {
  if (!state.leadsLoaded) return agentNoData();
  const flagged = state.leads.filter(needsReview)
    .sort(sortAttentionLeads);
  if (!flagged.length) {
    return {
      text: state.leads.length
        ? `All clear — none of the ${state.leads.length} loaded leads carry a review flag right now.`
        : 'There are no leads loaded yet, so there is nothing to review.',
      bullets: ['I watch for: no property matched, stalled invitation, stalled questionnaire, expired questionnaire link and unresponsive leads.'],
    };
  }
  const byFlag = new Map();
  for (const lead of flagged) {
    for (const flag of (lead.flags || [])) {
      if (NEEDS_REVIEW_FLAGS.includes(flag)) byFlag.set(flag, (byFlag.get(flag) || 0) + 1);
    }
  }
  return {
    text: `${flagged.length} of ${state.leads.length} loaded leads need attention — unmatched properties first.`,
    bullets: [...byFlag.entries()].map(([flag, count]) => `${flagMeta(flag).label}: ${count}`),
    chips: flagged.slice(0, 12).map((lead) => {
      const flag = (lead.flags || []).find((f) => NEEDS_REVIEW_FLAGS.includes(f));
      return agentChip(lead, `${flag ? flagMeta(flag).label : 'Flagged'} · ${relTime(lead.updated_at)}`);
    }),
    note: flagged.length > 12 ? `${flagged.length - 12} more are waiting in the Inbox.` : null,
  };
}

function agentIntentAnswer() {
  if (!state.leadsLoaded) return agentNoData();
  const strong = sortLeads(state.leads.filter((l) => (l.score && l.score.band === 'high') || optedIn(l)), 'score');
  if (!strong.length) {
    return {
      text: `No lead is scoring high or opted in to booking yet, out of ${state.leads.length} loaded.`,
    };
  }
  return {
    text: `${strong.length} lead${strong.length === 1 ? '' : 's'} look like your strongest intents — highest score first.`,
    chips: strong.slice(0, 12).map((lead) => {
      const score = lead.score && lead.score.value != null ? `score ${lead.score.value}` : 'unscored';
      return agentChip(lead, `${score}${optedIn(lead) ? ' · opted in' : ''} · ${relTime(lead.updated_at)}`);
    }),
    note: strong.length > 12 ? `Showing the top 12 of ${strong.length}.` : null,
  };
}

function agentStalledAnswer() {
  if (!state.leadsLoaded) return agentNoData();
  const stalled = state.leads
    .filter((l) => (l.flags || []).some((f) => String(f).startsWith('stalled_')))
    .sort((a, b) => new Date(a.updated_at || 0) - new Date(b.updated_at || 0));
  if (!stalled.length) {
    return {
      text: `Nothing is stalled — none of the ${state.leads.length} loaded leads carry a stalled flag.`,
    };
  }
  return {
    text: `${stalled.length} lead${stalled.length === 1 ? '' : 's'} stalled — longest waiting first.`,
    chips: stalled.slice(0, 12).map((lead) => {
      const labels = (lead.flags || []).filter((f) => String(f).startsWith('stalled_')).map((f) => flagMeta(f).label);
      return agentChip(lead, `${labels.join(', ')} · updated ${relTime(lead.updated_at)}`);
    }),
    note: stalled.length > 12 ? `Showing the 12 longest-waiting of ${stalled.length}.` : null,
  };
}

function agentFunnelAnswer() {
  if (!state.funnel || !Array.isArray(state.funnel.stages) || !state.funnel.stages.length) {
    return {
      text: 'The funnel breakdown is not loaded yet, so I have nothing accurate to show.',
      bullets: ['Give it a moment and ask again — the dashboard refreshes it in the background.'],
    };
  }
  const stages = state.funnel.stages;
  const total = stages.reduce((sum, stage) => sum + (Number(stage.count) || 0), 0);
  return {
    text: `Pipeline by stage — ${fmtNum(total)} in the funnel right now${state.funnel.generated_at ? `, generated ${relTime(state.funnel.generated_at)}` : ''}.`,
    bullets: stages.map((stage) => `${stageMeta(stage.stage).label}: ${fmtNum(Number(stage.count) || 0)}`),
  };
}

function agentMetricsAnswer() {
  const m = state.metrics;
  if (!m) {
    return {
      text: 'The metrics snapshot is not loaded yet, so I have no figures to quote.',
      bullets: ['Give it a moment and ask again — the dashboard fetches it in the background.'],
    };
  }
  const lines = [];
  const add = (label, value) => { if (value != null) lines.push(`${label}: ${value}`); };
  add('Total leads', m.total_leads != null ? fmtNum(m.total_leads) : null);
  add('New in the last 7 days', m.new_7d != null ? fmtNum(m.new_7d) : null);
  add('Questionnaire completion rate', typeof m.completion_rate === 'number' ? pct(m.completion_rate) : null);
  add('Viewing rate', typeof m.viewing_rate === 'number' ? pct(m.viewing_rate) : null);
  add('Viewings upcoming', m.viewings_upcoming != null ? fmtNum(m.viewings_upcoming) : null);
  add('High-intent leads', m.high_intent_count != null ? fmtNum(m.high_intent_count) : null);
  add('Needs review', m.needs_review_count != null ? fmtNum(m.needs_review_count) : null);
  if (!lines.length) {
    return { text: 'The metrics endpoint returned no figures I recognise, and I will not guess at them.' };
  }
  return {
    text: `Live snapshot from the metrics endpoint${m.generated_at ? `, generated ${relTime(m.generated_at)}` : ''}.`,
    bullets: lines,
  };
}

function agentPropertyMatches(query) {
  const tokens = query.split(/[^a-z0-9]+/)
    .filter((t) => t.length >= 4 && !AGENT_STOPWORDS.has(t) && !AGENT_STREET_WORDS.has(t));
  if (!tokens.length) return [];
  const hits = new Map();
  for (const lead of state.leads) {
    const address = String(lead.property_address || '').toLowerCase();
    if (!address) continue;
    if (tokens.some((t) => address.includes(t))) hits.set(String(lead.id), lead);
  }
  return [...hits.values()];
}

function agentPropertyAnswer(matches) {
  if (!state.leadsLoaded) return agentNoData();
  const list = sortLeads(matches, 'updated');
  return {
    text: `${list.length} loaded lead${list.length === 1 ? '' : 's'} match that property — most recently updated first.`,
    chips: list.slice(0, 12).map((lead) => agentChip(lead, `${lead.property_address || ''} · ${relTime(lead.updated_at)}`)),
    note: list.length > 12 ? `Showing 12 of ${list.length}.` : null,
  };
}

function agentResolve(query) {
  const q = String(query || '').toLowerCase();
  if (/(^|\b)(help|what can you do|what do you do|capabilit|commands|options|suggest)/.test(q)) return agentHelpAnswer();
  if (/(stall|stuck|overdue|chase)/.test(q)) return agentStalledAnswer();
  if (/(attention|review|flagged|triage|concern|problem|wrong)/.test(q)) return agentAttentionAnswer();
  if (/(intent|best|top|hot|priorit|strongest|hottest|score)/.test(q)) return agentIntentAnswer();
  if (/(funnel|pipeline|stage|journey|summary)/.test(q)) return agentFunnelAnswer();
  if (/(kpi|metric|how many|total|count|number|stat|rate|completion|upcoming)/.test(q)) return agentMetricsAnswer();
  const matches = agentPropertyMatches(q);
  if (matches.length) return agentPropertyAnswer(matches);
  return agentFallbackAnswer();
}

function agentWantsStats(query) {
  return /(funnel|pipeline|stage|journey|summary|kpi|metric|how many|total|count|number|stat|rate|completion|upcoming)/.test(String(query || '').toLowerCase());
}

function agentAvatar() {
  return el('div', { class: 'agent-avatar' }, el('img', { src: 'assets/meesix-mark.svg', alt: '' }));
}

function agentMessageNode(msg) {
  if (msg.role === 'user') {
    return el('div', { class: 'agent-msg user' },
      el('div', { class: 'agent-bubble' }, msg.text));
  }
  if (msg.thinking) {
    return el('div', { class: 'agent-msg' }, agentAvatar(),
      el('div', { class: 'agent-body' }, el('div', { class: 'agent-thinking', 'aria-label': 'Agent is thinking' }, el('i'), el('i'), el('i'))));
  }
  const body = el('div', { class: 'agent-body' }, el('div', { class: 'agent-prose', text: msg.text }));
  if (msg.model) body.append(el('div', { class: 'agent-note', text: `Model: ${msg.model}` }));
  if (msg.toolCalls && msg.toolCalls.length) body.append(el('div', { class: 'agent-tools' }, msg.toolCalls.map((call) => el('div', { class: 'agent-note', text: `${call.name}: ${call.summary}` }))));
  if (msg.error) body.append(el('div', { class: 'agent-note error', role: 'alert' }, msg.error));
  if (msg.bullets && msg.bullets.length) body.append(el('ul', { class: 'agent-list' }, msg.bullets.map((b) => el('li', { text: b }))));
  if (msg.chips && msg.chips.length) body.append(el('div', { class: 'agent-chips' }, msg.chips.map(agentLeadChip)));
  if (msg.note) body.append(el('div', { class: 'agent-note', text: msg.note }));
  return el('div', { class: 'agent-msg' }, agentAvatar(), body);
}

function renderAgentThread() {
  if (!agentThreadEl) return;
  if (agentWrapEl) agentWrapEl.classList.toggle('has-messages', state.agentMessages.length > 0);
  const pinned = agentThreadEl.scrollHeight - agentThreadEl.scrollTop - agentThreadEl.clientHeight < 40;
  if (!state.agentMessages.length) {
    agentThreadEl.replaceChildren();
  } else {
    agentThreadEl.replaceChildren(...state.agentMessages.map(agentMessageNode));
  }
  if (pinned || state.agentMessages.length <= 2) agentThreadEl.scrollTop = agentThreadEl.scrollHeight;
}

function agentSyncComposer() {
  if (!agentSendBtn || !agentInput) return;
  agentSendBtn.disabled = state.agentBusy || !agentInput.value.trim();
}

async function agentSend(text) {
  const value = String(text == null ? '' : text).trim();
  if (!value || state.agentBusy) return;
  state.agentMessages.push({ role: 'user', text: value });
  state.agentDraft = '';
  if (agentInput) agentInput.value = '';
  const thinking = { role: 'agent', thinking: true };
  state.agentMessages.push(thinking);
  state.agentBusy = true;
  renderAgentThread();
  agentSyncComposer();
  let answer;
  try {
    if (!agentSessionId) agentSessionId = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
    const previous = state.agentMessages.filter((m) => m.role === 'user' || (m.role === 'agent' && !m.thinking && !m.error));
    const data = await api('agent/chat', {
      message: value, sessionId: agentSessionId, model: agentModelSelect.value,
      history: previous.slice(0, -1).map((m) => ({ role: m.role === 'user' ? 'user' : 'assistant', content: m.text })),
    }, { method: 'POST', cache: 'no-store' });
    answer = { role: 'agent', text: data.reply, model: data.model || agentModelSelect.value, toolCalls: Array.isArray(data.toolCalls) ? data.toolCalls : [] };
  } catch (error) {
    answer = { role: 'agent', text: 'I could not reach the Agent service.', error: `${error.message} Check the Agent webhook connection in Settings and try again.` };
  }
  const index = state.agentMessages.indexOf(thinking);
  if (index >= 0) state.agentMessages.splice(index, 1, answer);
  state.agentBusy = false;
  renderAgentThread();
  agentSyncComposer();
  if (agentInput) agentInput.focus();
}

function agentComposer() {
  agentInput = el('input', {
    class: 'agent-input', type: 'text', autocomplete: 'off',
    placeholder: 'Ask Meesix about your leads…',
    'aria-label': 'Ask Meesix about dashboard leads',
  });
  agentInput.value = state.agentDraft || '';
  let selectedModel = 'qwen3.5:27b';
  try { const saved = localStorage.getItem(AGENT_MODEL_KEY); if (AGENT_MODELS.includes(saved)) selectedModel = saved; } catch { /* storage unavailable */ }
  agentModelSelect = el('select', { class: 'agent-model', 'aria-label': 'Choose local language model', title: 'Local model' }, AGENT_MODELS.map((model) => el('option', { value: model, text: model })));
  agentModelSelect.value = selectedModel;
  agentModelSelect.addEventListener('change', () => { try { localStorage.setItem(AGENT_MODEL_KEY, agentModelSelect.value); } catch { /* storage unavailable */ } });
  agentInput.addEventListener('input', () => {
    state.agentDraft = agentInput.value;
    agentSyncComposer();
  });
  agentInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      e.preventDefault();
      agentSend(agentInput.value);
    }
  });
  agentSendBtn = el('button', { class: 'agent-send', 'aria-label': 'Send message', title: 'Send', onclick: () => agentSend(agentInput.value) }, icon('arrowUp'));
  const composer = el('div', { class: 'agent-composer' }, agentInput, agentModelSelect, agentSendBtn);
  setTimeout(() => { agentInput.focus(); agentSyncComposer(); }, 0);
  return composer;
}

function renderAgentPage(view) {
  agentSessionId = agentSessionId || null;
  agentThreadEl = el('div', { class: 'agent-thread', 'aria-live': 'polite' });
  agentWrapEl = el('div', { class: 'agent-wrap' },
    el('div', { class: 'agent-brand' },
      el('img', { src: 'assets/meesix-mark.svg', alt: '' })),
    agentThreadEl,
    agentComposer());
  view.append(agentWrapEl);
  renderAgentThread();
  // Lead polls refresh this page every few seconds; re-render the thread only, so the
  // conversation and the composer state survive.
  state.refreshPage = renderAgentThread;
}

/* ═══ 21. Boot ═════════════════════════════════════════════════════════════ */

function onKeydown(e) {
  if (e.key === 'Escape') {
    if (document.querySelector('.menu')) { closeMenus(); return; }
    if (!byId('modal').hidden) { closeModal(); return; }
    if (byId('drawer').classList.contains('open')) { closeDrawer(); return; }
    return;
  }
  if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const t = e.target;
    const typing = t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement || t.isContentEditable;
    if (!typing && headerSearch && !headerSearchWrap?.hidden) {
      e.preventDefault();
      headerSearch.focus();
      headerSearch.select();
    }
  }
}

function boot() {
  buildShell();
  byId('drawer-scrim').addEventListener('click', closeDrawer);
  byId('modal-scrim').addEventListener('click', closeModal);
  window.addEventListener('hashchange', onRouteChange);
  document.addEventListener('keydown', onKeydown);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') pollLeads(); });
  onRouteChange();
  pollLeads();
  pollModeBadge();
  setInterval(pollLeads, POLL_LEADS_MS);
  setInterval(pollModeBadge, POLL_LEADS_MS);
  setInterval(pollOverview, POLL_OVERVIEW_MS);
}

boot();
