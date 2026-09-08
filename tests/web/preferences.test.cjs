// Offline event/state regressions. No browser, Redis, or network is used.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../web/app.js'), 'utf8');

function harness() {
  const nodes = new Map(), requests = [], timers = [], storage = new Map();
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, {
      dataset: {}, value: '', checked: false, hidden: false, disabled: false,
      innerHTML: '', textContent: '', className: '', listeners: {},
      classList: { contains: () => true, add() {}, remove() {}, toggle() {} },
      addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
      async emit(type, event = {}) {
        for (const fn of this.listeners[type] || []) await fn(event);
      },
      contains(el) { return el === this.child; },
      setAttribute() {}, focus() {}, querySelectorAll: () => [],
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    window: { addEventListener() {} }, location: { search: '' }, URLSearchParams,
    document: { querySelector: node, querySelectorAll: () => [], activeElement: null,
                documentElement: { dataset: {} }, addEventListener() {} },
    localStorage: { getItem: (k) => storage.get(k) ?? null,
                    setItem: (k, v) => storage.set(k, v) },
    setTimeout: (fn) => { timers.push(fn); }, clearTimeout() {}, console,
    fetch: async () => { throw Error('Unexpected network request'); },
    loadRotation: async () => {},
  });
  vm.runInContext(source.replace('import { auth } from "./auth.js";',
    'const auth = { header: () => ({}) };').replace(/\nboot\(\);\s*$/, ''), context);
  const state = vm.runInContext('state', context);
  context.reply = async () => ({ ok: true, prefs: { acme: { titles: ['Staff Engineer'] } } });
  context.request = async (url, body) => {
    requests.push({ url, body: JSON.parse(JSON.stringify(body)) });
    return context.reply(url, body);
  };
  vm.runInContext(`post = request; toast = () => {}; loadApps = () => {};
    renderPicker = () => {}; renderDeck = () => {}; pollStats = () => {};
    demoGuard = () => false;`, context);
  const start = source.indexOf('  const rememberCpref =');
  const end = source.indexOf('  $("#cp-detail").addEventListener("click", async', start);
  vm.runInContext(`(() => { ${source.slice(start, end)}
    globalThis.saveCompany = saveCprefs; })()`, context);
  const pfStart = source.indexOf('  const pf = $("#prefpicker")');
  const pfEnd = source.indexOf('  const sp = $("#skippicker")', pfStart);
  vm.runInContext(`(() => { ${source.slice(pfStart, pfEnd)}
    globalThis.loadShared = loadPrefs; globalThis.saveShared = savePrefs; })()`, context);
  state.detailCo = 'Acme';
  state.companies = ['Acme', 'Beta'];
  state.prefs = { titles: ['Software Engineer'], min_match_score: 7, remote_only: false };
  return { context, state, node, requests, timers, storage };
}

function edit(h, field, value) {
  const target = { dataset: { co: 'Acme', cpref: field }, value,
                   classList: { contains: () => true } };
  return h.node('#cp-detail').emit('input', { target });
}

test('tabbing through edits never saves, starts a scan, or closes the popup', async () => {
  const h = harness();
  await edit(h, 'titles', 'Staff Engineer');
  await h.node('#cp-detail').emit('focusout', { target: {} });
  await edit(h, 'min_match_score', '8');
  assert.equal(h.requests.length, 0);
  assert.equal(h.timers.length, 0);
  assert.equal(await h.context.saveCompany('Acme'), true);
  assert.deepEqual(h.requests, [{ url: '/actions/company-prefs', body: {
    name: 'Acme', overrides: { titles: 'Staff Engineer', min_match_score: '8' },
  } }]);
  assert.equal(h.node('#copicker').hidden, false);
  assert.equal(h.timers.length, 0);
});

test('saving inherited values never detaches a company or accidentally resets other overrides', async () => {
  const h = harness();
  h.state.cprefs.acme = { min_match_score: 9 };
  await edit(h, 'titles', ' Software Engineer ');
  await h.context.saveCompany('Acme');
  assert.equal(h.requests.length, 0); // {} would reset the score override server-side.
  await edit(h, 'min_match_score', '7');
  await h.context.saveCompany('Acme');
  assert.deepEqual(h.requests[0].body.overrides, { min_match_score: null });
});

test('failed saves and edits made during a slow save are retained for retry', async () => {
  const h = harness();
  await edit(h, 'titles', 'Staff Engineer');
  h.context.reply = async () => ({ ok: false, error: 'offline' });
  assert.equal(await h.context.saveCompany('Acme'), false);
  assert.equal(h.state.cpDrafts.acme.titles, 'Staff Engineer');
  let finish;
  h.context.reply = () => new Promise((resolve) => { finish = resolve; });
  const saving = h.context.saveCompany('Acme');
  assert.equal(await h.context.saveCompany('Acme'), false); // double-click is not another write.
  await edit(h, 'titles', 'Principal Engineer');
  finish({ ok: true, prefs: { acme: { titles: ['Staff Engineer'] } } });
  assert.equal(await saving, false); // Scan now must not run with an unsaved newer edit.
  assert.equal(h.state.cpDrafts.acme.titles, 'Principal Engineer');
  assert.equal(h.requests.length, 2);
});

test('refreshes cannot replace an active dropdown or a draft', () => {
  const h = harness(), box = h.node('#cp-detail');
  box.dataset.co = 'Acme'; box.innerHTML = 'existing form';
  box.child = {}; h.context.document.activeElement = box.child;
  vm.runInContext('renderDetail()', h.context);
  assert.equal(box.innerHTML, 'existing form');
  h.context.document.activeElement = null;
  h.state.cpDrafts.acme = { titles: 'Principal Engineer' };
  vm.runInContext('renderDetail()', h.context);
  assert.equal(box.innerHTML, 'existing form');
  box.dataset.co = 'Beta';
  vm.runInContext('renderDetail()', h.context);
  assert.match(box.innerHTML, /value="Principal Engineer"/);
});

test('shared Save immediately updates Discover defaults and keeps the panel open', async () => {
  const h = harness();
  h.context.fetch = async () => ({ json: async () => h.state.prefs });
  await h.context.loadShared();
  h.node('#pf-titles').value = 'Principal Engineer';
  h.context.reply = async () => ({ ok: true, preferences: { titles: ['Principal Engineer'] } });
  await h.context.saveShared();
  assert.deepEqual(Array.from(h.state.prefs.titles), ['Principal Engineer']);
  assert.equal(h.node('#prefpicker').hidden, false);
  assert.equal(h.node('#pf-state').className, 'saved');
  await edit(h, 'titles', 'Principal Engineer');
  await h.context.saveCompany('Acme');
  assert.equal(h.requests.length, 1); // It inherits the new default; no override write.
});

test('an empty company selection never means scan or process everything', async () => {
  const h = harness();
  assert.equal(vm.runInContext('pickedAll()', h.context), false);
  await vm.runInContext('runDiscover()', h.context);
  await vm.runInContext('runProcess()', h.context);
  assert.equal(h.requests.length, 0);
});

test('application dates are exact calendar dates and never fall back to last activity', () => {
  const h = harness();
  const markup = vm.runInContext('appliedDateHtml("2026-08-01T12:00:00Z")', h.context);
  assert.match(markup, /datetime="2026-08-01T12:00:00Z"/);
  assert.match(markup, /2026/);
  const missing = vm.runInContext('appliedRow({pk:"acme#1", company:"Acme", title:"Engineer", status:"applied", updated_at:"2026-09-07T12:00:00Z"})', h.context);
  assert.match(missing, /Not recorded/);
  assert.doesNotMatch(missing, /just now|ago/);
});

test('applied history sorts by submission date even after a later profile edit', () => {
  const h = harness();
  h.state.apps = [
    {pk:'old',status:'applied',applied_at:'2026-08-01T00:00:00Z',updated_at:'2026-09-07T00:00:00Z'},
    {pk:'new',status:'applied',applied_at:'2026-08-02T00:00:00Z',updated_at:'2026-08-02T00:00:00Z'},
  ];
  assert.deepEqual(Array.from(vm.runInContext('visible(state.apps).map(r => r.pk)', h.context)), ['new','old']);
});

test('saved company selections, including Clear, survive reload', async () => {
  const h = harness();
  h.context.fetch = async () => ({ json: async () => ({ companies: ['Acme', 'Beta'] }) });
  vm.runInContext('renderSkipPicker = () => {}; renderCompanyOptions = () => {};', h.context);
  h.state.picked.add('Acme');
  vm.runInContext('savePicked()', h.context);
  h.state.picked.clear();
  await vm.runInContext('loadCompanies()', h.context);
  assert.deepEqual(Array.from(h.state.picked), ['Acme']);
  h.state.picked.clear();
  vm.runInContext('savePicked()', h.context);
  h.state.pickedLoaded = false;
  await vm.runInContext('loadCompanies()', h.context);
  assert.equal(h.state.picked.size, 0);
});

test('a failed preference load cannot save blank fields over existing preferences', async () => {
  const h = harness();
  await h.context.loadShared(); // the default fetch fails without touching the network
  await h.context.saveShared();
  assert.equal(h.requests.length, 0);
});

test('a failed Discover keeps the popup open and clears its optimistic running state', async () => {
  const h = harness();
  h.state.picked.add('Acme');
  h.context.reply = async () => ({ ok: false, error: 'Scan unavailable' });
  vm.runInContext('closeAllPickers = () => { throw Error("Failed runs must keep the editor open"); };', h.context);
  await vm.runInContext('runDiscover()', h.context);
  assert.equal(h.state.stats.discovering, false);
  assert.equal(h.requests.length, 1);
});
