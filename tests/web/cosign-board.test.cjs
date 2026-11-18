// Offline: Apply sends an application under the owner's name, so one press must
// never be enough. And the odometer must land on the real number.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../web/cosign-board.js'), 'utf8');

function load(fetch) {
  let Board;
  const context = vm.createContext({HTMLElement: class {}, customElements: {define: (_, cls) => Board = cls},
    window: {}, location: {search: ''}, URL, URLSearchParams, matchMedia: () => ({matches: true}), fetch, setTimeout, clearTimeout, Set, Date, Number});
  vm.runInContext(source.replace("import { auth } from './auth.js';", "const auth = {header: () => ({})};")
    .replace('export function odometer', 'globalThis.odometer = function odometer').replace('export const places', 'globalThis.places').replace('export function merge', 'globalThis.merge = function merge').replace('export const sourceOf', 'globalThis.sourceOf'), context);
  return {Board, odometer: context.odometer, places: context.places, merge: context.merge, sourceOf: context.sourceOf};
}

test('one press of Apply sends the selection; the score gate lives on the server', async () => {
  const sent = [];
  const {Board} = load(async (url, opts) => { sent.push([url, JSON.parse(opts.body)]); return {ok: true, json: async () => ({started: 2, prepared: 2, duplicates: 0})}; });
  const board = new Board();
  Object.assign(board, {network: 'cosign', jobs: [{id: 'a', state: 'new'}, {id: 'b', state: 'new'}], selected: new Set(['a', 'b']),
    renderList() {}, open() {}, status(text) { this.said = text; }});
  await board.stage([...board.selected], true);
  assert.deepEqual(sent, [['/cosign/apply', {ids: ['a', 'b']}]]);
  assert.deepEqual(Array.from(board.jobs, j => j.state), ['pipeline', 'pipeline']);
});

test('Score & tailor acts at once because it stops for review', async () => {
  const sent = [];
  const {Board} = load(async url => { sent.push(url); return {ok: true, json: async () => ({prepared: 1, duplicates: 0})}; });
  const board = new Board();
  Object.assign(board, {network: 'cosign', jobs: [{id: 'a', state: 'new'}], selected: new Set(), armed: null, renderList() {}, open() {}, status() {}});
  await board.stage(['a'], false);
  assert.deepEqual(sent, ['/cosign/prepare']);
});

test('each odometer reel stops on its own digit', () => {
  const {odometer} = load(async () => {});
  const el = {setAttribute(k, v) { this[k] = v; }, innerHTML: ''};
  odometer(el, 71427);
  assert.equal(el['aria-label'], '71,427');
  const stops = [...el.innerHTML.matchAll(/--to:(-?[\d.]+)em/g)].map(m => Number(m[1]));
  // Reels hold 0–9 twice; landing in the second run guarantees a full turn.
  assert.deepEqual(stops.map(v => Math.round(-v / 1.2) - 10), [7, 1, 4, 2, 7]);
});

test('a location lists each city once, however Cosign spelled it', () => {
  const {places} = load(async () => {});
  assert.equal(places('Austin, TX · Austin, Texas, United States · Seattle, WA · Seattle, Washington, United States'), 'Austin, TX · Seattle, WA');
  assert.equal(places('A · B · C · D · E', 3), 'A · B · C +2');
  assert.equal(places('Rome, NY'), 'Rome, NY');
  assert.equal(places(''), '');
  // A comma can also separate cities; only state and country parts are dropped.
  assert.equal(places('San Francisco, Seattle, New York'), 'San Francisco · Seattle · New York');
  assert.equal(places('Seattle, Washington, United States · Washington, DC'), 'Seattle · Washington, DC');
});

test('several cities merge into one list: each role once, newest first', () => {
  const {merge} = load(async () => {});
  const jobs = [{id: 'a', posted: 1}, {id: 'b', posted: 3}];
  const fresh = merge(jobs, [{id: 'b', posted: 3}, {id: 'c', posted: 2}], true);
  assert.deepEqual(fresh.map(j => j.id), ['c']);
  assert.deepEqual(Array.from(jobs, j => j.id), ['b', 'c', 'a']);
});

function stubbed(Board) {
  const board = new Board();
  const el = () => ({hidden: false, disabled: false, innerHTML: '', textContent: '', dataset: {}, scrollTop: 0,
    replaceChildren() {}, querySelector: () => el(), classList: {add() {}, remove() {}, toggle() {}}});
  Object.assign(board, {network: 'cosign', jobs: [], selected: new Set(), cursors: {}, progress: {}, region: 'us', examples: [],
    classList: {add() {}, remove() {}, toggle() {}}, $: () => el(), filters: () => ({term: 'x'}),
    paintExample() {}, paintProgress() {}, renderList() {}, open() {}, skeleton() {}, status(t, e) { this.said = [t, !!e]; }});
  return board;
}

test('each city is its own request, and one failing city keeps the others', async () => {
  const bodies = [];
  const {Board} = load(async (url, opts) => {
    const body = JSON.parse(opts.body); bodies.push(body);
    if (body.city === 'austin') return {ok: false, status: 502, json: async () => ({detail: 'Cosign could not run this search.'})};
    return {ok: true, json: async () => ({jobs: [{id: body.city, posted: 1}], cursor: 'next', done: false})};
  });
  const board = stubbed(Board);
  board.cities = ['seattle', 'austin', 'boston'];
  await board.search();
  assert.deepEqual(Array.from(board.jobs, j => j.id).sort(), ['boston', 'seattle']);  // copied out of the vm realm
  assert.equal(board.progress.austin.state, 'fail');
  // Filling towards a hundred continues only the cities that answered, each from
  // its own cursor, and never retries the one that failed.
  assert.equal(bodies.filter(b => b.city === 'austin').length, 1);
  const later = bodies.slice(3);
  assert.ok(later.length > 0);
  assert.ok(later.every(b => ['seattle', 'boston'].includes(b.city) && b.cursor === 'next'));
});

test('with no city chosen, one search runs with the US scope', async () => {
  const bodies = [];
  const {Board} = load(async (url, opts) => { bodies.push(JSON.parse(opts.body)); return {ok: true, json: async () => ({jobs: [], cursor: null, done: true})}; });
  const board = stubbed(Board);
  board.cities = [];
  await board.search();
  assert.deepEqual(bodies.map(b => [b.city, b.region]), [['', 'us']]);
  assert.deepEqual(Array.from(board.said), ['No roles found', false]);
});

test('a thin first page keeps reading until about a hundred roles show', async () => {
  let n = 0;
  const {Board} = load(async (url, opts) => {
    const body = JSON.parse(opts.body);
    const jobs = Array.from({length: 30}, () => ({id: `j${n++}`, posted: 1}));
    return {ok: true, json: async () => ({jobs, cursor: body.cursor ? body.cursor + 1 : 1, done: false})};
  });
  const board = stubbed(Board);
  board.cities = [];
  await board.search();
  assert.equal(board.jobs.length, 120);  // 4 pages of 30 is the first to pass 100
  assert.equal(board.filling, false);
});

test('a search that genuinely has few roles stops instead of looping', async () => {
  let calls = 0;
  const {Board} = load(async () => { calls++; return {ok: true, json: async () => ({jobs: [], cursor: 'more', done: false})}; });
  const board = stubbed(Board);
  board.cities = [];
  await board.search();
  assert.ok(calls <= 9, `made ${calls} requests`);
});

test('sending roles follows every one of them in the side pane', async () => {
  const {Board} = load(async () => ({ok: true, json: async () => ({prepared: 1, duplicates: 1, started: 1, tracked: {a: 'acme#1', b: 'beta#2'}})}));
  const board = new Board();
  let shown = 0, polled = 0;
  Object.assign(board, {network: 'cosign', jobs: [{id: 'a', state: 'new', title: 'A'}, {id: 'b', state: 'new', title: 'B'}], selected: new Set(), sent: new Map(),
    renderList() {}, status() {}, showApps() { shown++; }, poll() { polled++; }});
  await board.stage(['a', 'b'], true);
  // The duplicate is followed too: it is a role the owner wants applied.
  assert.deepEqual([...board.sent.keys()].sort(), ['acme#1', 'beta#2']);
  assert.equal(board.jobs[1].pk, 'beta#2');
  assert.equal(shown, 1); assert.equal(polled, 1);
});

test('polling stops by itself once every application has landed', async () => {
  const calls = [];
  const {Board} = load(async (url, opts) => { calls.push(JSON.parse(opts.body).pks); return {ok: true, json: async () => ({'acme#1': {phase: 'applied'}, 'beta#2': {phase: 'attention', status: 'skipped'}})}; });
  const board = new Board();
  Object.assign(board, {sent: new Map([['acme#1', {}], ['beta#2', {}]]), live: {}, paneView: 'apps', hidden: false, paintApps() {}});
  await board.poll();
  assert.equal(calls.length, 1);
  assert.equal(board.pollTimer, undefined, 'no further poll is scheduled');
});

test('every posting names the board it lives on', () => {
  const {sourceOf} = load(async () => {});
  assert.equal(sourceOf('https://jobs.ashbyhq.com/replit/abc'), 'Ashby');
  assert.equal(sourceOf('https://job-boards.greenhouse.io/snorkelai/jobs/1'), 'Greenhouse');
  assert.equal(sourceOf('https://bostondynamics.wd1.myworkdayjobs.com/x'), 'Workday');
  assert.equal(sourceOf('https://careers.acme.com/job/1'), 'acme.com');
  assert.equal(sourceOf('not a url'), '');
});

test('a metered search waits for Search, then fetches just one page per press', async () => {
  const requests = [];
  const {Board} = load(async (url, options) => {
    requests.push([url, JSON.parse(options.body).cursor]);
    return {ok: true, json: async () => ({jobs: [{id: 'treg:a'}], cursor: '2', done: false})};
  });
  const board = stubbed(Board);
  Object.assign(board, {network: 'treg', manualSearch: true, options: {configured: true}, cities: [], lock() {}});
  board.filtersChanged();
  assert.deepEqual(requests, []);
  await board.search();
  assert.deepEqual(requests, [['/treg/search', null]]);
  await board.search(true);
  assert.deepEqual(requests, [['/treg/search', null], ['/treg/search', '2']]);
});

test('checking a saved key unlocks Treg search without a billable request', async () => {
  const requests = [];
  const {Board} = load(async (url, {method}) => {
    requests.push([url, method]);
    return {ok: true, json: async () => ({configured: true})};
  });
  const board = stubbed(Board);
  const fields = new Map();
  Object.assign(board, {network: 'treg', options: {configured: false}, lock(on) { this.locked = on; },
    $(selector) { if (!fields.has(selector)) fields.set(selector, {}); return fields.get(selector); }});
  await board.checkTregKey();
  assert.deepEqual(requests, [['/treg', 'GET']]);
  assert.equal(board.options.configured, true);
  assert.equal(board.locked, false);
  assert.equal(board.$('.co-treg-setup').hidden, true);
});
