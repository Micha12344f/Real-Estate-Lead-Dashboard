// Tab-local, non-secret references for retrying a DELETE whose result is unknown.
(function (root) {
  'use strict';
  const UUID = /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i;
  const ID = /^[1-9][0-9]*$/;
  const modes = new Set(['production', 'sandbox']);
  const prefix = (mode) => modes.has(mode) ? `tve-lead-delete:${mode}:` : '';

  function key(mode, id) {
    return prefix(mode) && ID.test(String(id)) ? prefix(mode) + id : null;
  }

  function parse(raw, contactUid) {
    try {
      const value = JSON.parse(raw);
      return value && UUID.test(value.id) && UUID.test(value.contactUid)
        && typeof value.at === 'string' && Number.isFinite(Date.parse(value.at))
        && (!contactUid || value.contactUid === contactUid)
        ? value : null;
    } catch { return null; }
  }

  function get(storage, mode, id, contactUid) {
    const name = key(mode, id);
    if (!name) return null;
    try { return parse(storage.getItem(name), contactUid); } catch { return null; }
  }

  function pending(storage, mode) {
    const scope = prefix(mode);
    if (!scope) return [];
    const items = [];
    try {
      for (let i = 0; i < storage.length; i++) {
        const name = storage.key(i);
        if (!name || !name.startsWith(scope)) continue;
        const id = name.slice(scope.length);
        if (!ID.test(id) || !Number.isSafeInteger(Number(id))) continue;
        const reference = get(storage, mode, id);
        if (reference && reference.attempted === true) items.push({ ...reference, leadId: Number(id) });
      }
    } catch { return []; }
    return items;
  }

  function save(storage, mode, id, reference) {
    const name = key(mode, id);
    if (!name || !parse(JSON.stringify(reference))) return false;
    try { storage.setItem(name, JSON.stringify({ id: reference.id, at: reference.at,
      contactUid: reference.contactUid, attempted: true })); return true; }
    catch { return false; }
  }

  function clear(storage, mode, id) {
    const name = key(mode, id);
    if (name) try { storage.removeItem(name); } catch { /* storage unavailable */ }
  }

  const api = { get, pending, save, clear };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TveDeleteRecovery = api;
})(typeof window !== 'undefined' ? window : globalThis);
