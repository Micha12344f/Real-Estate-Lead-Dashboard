const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const recovery = require('./dashboard/delete-recovery');

function storage() {
  const values = new Map();
  return {
    get length() { return values.size; },
    key(i) { return [...values.keys()][i] || null; },
    getItem(k) { return values.get(k) ?? null; },
    setItem(k, v) { values.set(k, v); },
    removeItem(k) { values.delete(k); },
  };
}

const uid = '00000000-0000-0000-0000-000000000007';
const op = 'bd2b6957-231a-4f09-9674-27d08ee8fd78';

test('only attempted operations in the current mode are offered for recovery', () => {
  const tab = storage();
  const reference = { id: op, contactUid: uid, at: new Date().toISOString() };
  assert.deepEqual(recovery.pending(tab, 'production'), []);
  tab.setItem('tve-lead-delete:production:7', JSON.stringify(reference)); // modal opened, never sent
  assert.deepEqual(recovery.pending(tab, 'production'), []);
  assert.equal(recovery.save(tab, 'production', 7, reference), true);
  assert.equal(recovery.save(tab, 'sandbox', 8, reference), true);
  tab.setItem('tve-lead-delete:production:garbage', JSON.stringify(reference));
  assert.deepEqual(recovery.pending(tab, 'production').map(item => item.leadId), [7]);
  assert.deepEqual(recovery.pending(tab, 'sandbox').map(item => item.leadId), [8]);
  assert.equal(recovery.get(tab, 'production', 7, 'different-uid'), null);
  assert.ok(!tab.getItem('tve-lead-delete:production:7').includes('credential'));
  recovery.clear(tab, 'production', 7);
  assert.deepEqual(recovery.pending(tab, 'production'), []);
});

test('reload with an absent lead retries using the original operation without another credential prompt', async () => {
  const tab = storage();
  const ref = { id: op, contactUid: uid, at: new Date().toISOString() };
  recovery.save(tab, 'production', 7, ref);

  class Node {
    constructor(tag) { this.tag = tag; this.children = []; this.handlers = {}; this.dataset = {}; this.value = ''; }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(k, v) { this[k] = v; }
    addEventListener(k, callback) { this.handlers[k] = callback; }
  }
  const notice = new Node('div');
  const context = vm.createContext({
    location: { pathname: '/sandbox/' }, URLSearchParams, Node,
    document: { createElement: tag => new Node(tag), createTextNode: text => { const node = new Node('text'); node.textContent = text; return node; },
      getElementById: id => id === 'pending-delete-notice' ? notice : null },
    sessionStorage: tab, TveDeleteRecovery: recovery, renderFormsPage: () => {}, crypto: { randomUUID: () => { throw new Error('new operation forbidden'); } },
    requests: [], modal: null, failOnce: true,
  });
  const source = fs.readFileSync(path.join(__dirname, 'dashboard/app.js'), 'utf8');
  assert.match(source, /\nboot\(\);\s*$/);
  vm.runInContext(source.replace(/\nboot\(\);\s*$/, ''), context);
  vm.runInContext(`state.mode = 'production'; state.leads = []; state.starred = new Set();
    openModal = (title, body, footer) => { globalThis.modal = { title, body, footer }; };
    closeModal = () => {}; persistStarred = () => {}; updateBadges = () => {};
    toast = () => {}; api = async (path, body, options) => {
      globalThis.requests.push({ path, options });
      if (globalThis.failOnce) { globalThis.failOnce = false; throw new Error('Deletion incomplete; retry'); }
      return { deleted: true };
    };
    updatePendingDeleteNotice();`, context);

  assert.equal(notice.children.length, 1);
  assert.match(notice.children[0].children[0].textContent, /lead #7/);
  notice.children[0].children[1].handlers.click();
  assert.match(context.modal.title, /Retry pending/);
  assert.equal(context.modal.body.some(node => node.tag === 'input'), false);
  await context.modal.footer[1].handlers.click();
  assert.equal(context.requests.length, 1);
  assert.equal(context.requests[0].path, 'leads/7');
  assert.equal(context.requests[0].options.headers['X-TVE-Delete-Operation'], op);
  assert.equal(context.requests[0].options.headers['X-TVE-Delete-Contact-UID'], uid);
  assert.equal(context.requests[0].options.headers['X-TVE-Delete-Auth'], undefined);
  assert.ok(tab.getItem('tve-lead-delete:production:7'));
  assert.equal(notice.children.length, 1);
  await context.modal.footer[1].handlers.click();
  assert.equal(context.requests.length, 2);
  assert.equal(context.requests[1].options.headers['X-TVE-Delete-Auth'], undefined);
  assert.equal(context.requests[1].options.headers['X-TVE-Delete-Operation'], op);
  assert.equal(tab.getItem('tve-lead-delete:production:7'), null);
  assert.equal(notice.children.length, 0);
});
