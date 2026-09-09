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
    requests.push({ url, body: body === undefined ? undefined : JSON.parse(JSON.stringify(body)) });
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
  h.context.fetch = async () => ({ ok: true, json: async () => h.state.prefs });
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
  h.context.fetch = async () => ({ ok: true, json: async () => ({ companies: ['Acme', 'Beta'] }) });
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

test('Run all acknowledges immediately and suppresses duplicates while the request is pending', async () => {
  const h = harness();
  h.state.picked = new Set(['Acme']);
  h.context.renderPane = () => {};
  let finish;
  h.context.reply = () => new Promise((resolve) => { finish = resolve; });
  const run = h.context.runProcess();
  assert.equal(h.state.requests.has('prepare'), true);
  assert.match(h.state.launch.label, /Starting preparation/);
  await h.context.runProcess();
  assert.equal(h.requests.length, 1);
  finish({ok:false,error:'Could not start'});
  await run;
  assert.equal(h.state.requests.has('prepare'), false);
  assert.equal(h.state.stats.processing, false);
});

test('follow-up reminders ignore completed outcomes and dates filter by actual submission', () => {
  const h = harness();
  assert.equal(h.context.followupDue({follow_up:'2026-09-01',outcome:'waiting'}, '2026-09-07'), true);
  assert.equal(h.context.followupDue({follow_up:'2026-09-01',outcome:'rejected'}, '2026-09-07'), false);
  h.state.appFrom = '2026-09-01'; h.state.appTo = '2026-09-07';
  const rows = h.context.trackerRows([
    {pk:'1',applied_at:'2026-09-03T12:00:00Z'},
    {pk:'2',updated_at:'2026-09-03T12:00:00Z'},
    {pk:'3',applied_at:'2026-08-01T12:00:00Z',updated_at:'2026-09-03T12:00:00Z'},
  ]);
  assert.equal(rows.length, 1); assert.equal(rows[0].pk, '1');
});

test('jobs still being tailored are never presented as ready with a missing completion date', () => {
  const h = harness();
  assert.equal(h.context.sectionOf({status:'tailoring'}, new Set()), 'preparing');
  assert.equal(h.context.sectionOf({status:'tailored'}, new Set()), 'ready');
});

test('company Approve all confirms and sends only visible eligible jobs', async () => {
  const h = harness(), confirmations = [], busy = [];
  h.state.coFilter = 'Acme'; h.state.query = 'Staff';
  h.state.apps = [
    {pk:'acme#1',company:'Acme',title:'Staff Engineer',status:'tailored'},
    {pk:'acme#2',company:'Acme',title:'Senior Engineer',status:'tailored'},
    {pk:'beta#1',company:'Beta',title:'Staff Engineer',status:'tailored'},
    {pk:'acme#3',company:'Acme',title:'Staff Engineer',status:'applied'},
  ];
  h.context.confirm = (message) => { confirmations.push(message); return true; };
  h.context.markBusy = (pk) => busy.push(pk);
  h.context.loadQueue = () => {}; h.context.scheduleReload = () => {};
  h.context.reply = async () => ({ok:true,queued:1});
  await h.context.approveAll();
  assert.match(confirmations[0], /Approve 1 job at Acme/);
  assert.deepEqual(h.requests[0].body, {company:'Acme',pks:['acme#1']});
  assert.deepEqual(busy, ['acme#1']);
});

test('the Needs you approval never pulls in tailored jobs from another view', () => {
  const h = harness(); h.state.tab = 'needs'; h.state.coFilter = 'Acme';
  h.state.apps = [
    {pk:'acme#1',company:'Acme',title:'Engineer',status:'tailored'},
    {pk:'acme#2',company:'Acme',title:'Engineer',status:'needs_human',gate_reason:'approval'},
    {pk:'acme#3',company:'Acme',title:'Engineer',status:'needs_human',gate_reason:'unknown_field'},
  ];
  assert.deepEqual(Array.from(h.context.visibleApprovalPicks(), r => r.pk), ['acme#2']);
});

test('ready approvals exclude jobs already in the queue and honor the location filter', () => {
  const h = harness(); h.state.locFilter = h.context.locTier('Seattle').key;
  h.state.apps = [
    {pk:'1',company:'Acme',title:'Engineer',status:'tailored',location:'Seattle'},
    {pk:'2',company:'Acme',title:'Engineer',status:'tailored',location:'Seattle'},
    {pk:'3',company:'Acme',title:'Engineer',status:'tailored',location:'London'},
  ];
  h.state.queue = {pending:[{pk:'2'}]};
  assert.deepEqual(Array.from(h.context.visibleApprovalPicks(), r => r.pk), ['1']);
});

test('a cancelled approval sends nothing and a second pending click cannot widen it', async () => {
  const h = harness(); h.state.coFilter = 'Acme';
  h.state.apps = [{pk:'1',company:'Acme',title:'Engineer',status:'tailored'}];
  h.context.confirm = () => false;
  await h.context.approveAll(); assert.equal(h.requests.length, 0);
  h.context.confirm = () => true; h.context.markBusy = () => {};
  h.context.loadQueue = () => {}; h.context.scheduleReload = () => {};
  let finish; h.context.reply = () => new Promise(resolve => { finish = resolve; });
  const pending = h.context.approveAll();
  h.state.coFilter = '';
  await h.context.approveAll(); assert.equal(h.requests.length, 1);
  assert.deepEqual(h.requests[0].body, {company:'Acme',pks:['1']});
  finish({ok:true,queued:1}); await pending;
  assert.equal(h.state.approvingAll, false);
});

function reviewHarness() {
  const h = harness();
  h.state.apps = [
    {pk:'openai#keep',company:'OpenAI',title:'Staff Engineer',status:'tailored'},
    {pk:'openai#reject',company:'OpenAI',title:'Senior Engineer',status:'tailored'},
    {pk:'beta#keep',company:'Beta',title:'Staff Engineer',status:'tailored'},
  ];
  h.context.confirm = () => true; h.context.markBusy = () => {};
  h.context.loadQueue = () => {}; h.context.scheduleReload = () => {};
  h.context.reply = async (url, body) => ({ok:true,queued:body.pks.length, results:body.pks.map(pk => ({pk,ok:true}))});
  h.context.renderPane = () => {};
  return h;
}


test('tailored roles appear in review before anything is queued to submit', () => {
  const h = reviewHarness(); h.state.queue = {pending:[]};
  const markup = h.context.reviewQueueSec(h.context.visibleApprovalPicks());
  assert.match(markup, /data-review-pick="openai#reject"/);
  assert.match(markup, /data-review-action="reject"/);
  assert.match(markup, /data-review-action="skip"/);
  assert.match(markup, /data-review-action="apply"/);
  assert.doesNotMatch(h.context.readySec(h.state.apps), /kcard/);
  assert.equal(h.requests.length, 0);
});

test('checkbox rejection closes only selected roles, then Apply all approves the remainder at that company', async () => {
  const h = reviewHarness();
  h.state.reviewPicked.add('openai#reject'); h.state.reviewPicked.add('beta#keep');
  await h.context.reviewAction('reject','OpenAI');
  assert.deepEqual(h.requests, [{url:'/actions/review-decision',body:{company:'OpenAI',pks:['openai#reject'],action:'reject'}}]);
  await h.context.reviewAction('apply','OpenAI');
  assert.deepEqual(h.requests.slice(1), [
    {url:'/actions/apply-selection',body:{company:'OpenAI',pks:['openai#keep']}},
  ]);
  assert.equal(h.state.reviewPicked.has('beta#keep'), true);
});

test('Skip leaves selected roles waiting and excludes them from Apply all', async () => {
  const h = reviewHarness(); h.state.reviewPicked.add('openai#reject');
  await h.context.reviewAction('skip','OpenAI');
  assert.equal(h.requests.length, 1);
  assert.equal(h.state.apps[1].status, 'tailored');
  assert.equal(h.state.apps[1].review_skipped, true);
  assert.equal(h.requests[0].body.action, 'skip');
  await h.context.reviewAction('apply','OpenAI');
  assert.deepEqual(h.requests[1].body, {company:'OpenAI',pks:['openai#keep']});
});

test('a skipped role can be explicitly selected to apply later without approving others', async () => {
  const h = reviewHarness(); h.state.reviewFilter = 'later'; h.state.reviewSkipped.add('openai#reject'); h.state.reviewPicked.add('openai#reject');
  await h.context.reviewAction('apply','OpenAI');
  assert.deepEqual(h.requests[0].body, {company:'OpenAI',pks:['openai#reject']});
});

test('a failed approval never starts a company and keeps the selection', async () => {
  const h = reviewHarness(); h.state.reviewPicked.add('openai#keep');
  h.context.reply = async () => ({ok:false,error:'Could not approve'});
  await h.context.reviewAction('apply','OpenAI');
  assert.equal(h.requests.length, 1);
  assert.equal(h.state.reviewPicked.has('openai#keep'), true);
  assert.equal(h.state.approvingAll, false);
});

test('new roles and double clicks cannot widen a pending Apply selection', async () => {
  const h = reviewHarness(); h.state.reviewPicked.add('openai#keep');
  let finish;
  h.context.reply = (url) => url === '/actions/apply-selection'
    ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ok:true,queued:1});
  const pending = h.context.reviewAction('apply','OpenAI');
  h.state.apps.push({pk:'openai#new',company:'OpenAI',title:'New',status:'tailored'});
  await h.context.reviewAction('apply','Beta');
  assert.equal(h.requests.length, 1);
  assert.deepEqual(h.requests[0].body, {company:'OpenAI',pks:['openai#keep']});
  finish({ok:true,queued:1}); await pending;
});

test('partial rejection retains failures and Undo restores only successful rejections', async () => {
  const h = reviewHarness();
  h.state.reviewPicked.add('openai#keep'); h.state.reviewPicked.add('openai#reject');
  h.context.reply = async () => ({ok:false,results:[{pk:'openai#keep',ok:true},{pk:'openai#reject',ok:false,error:'Already approved'}]});
  await h.context.reviewAction('reject', 'OpenAI');
  assert.equal(h.state.reviewPicked.has('openai#reject'), true);
  assert.equal(h.state.reviewPicked.has('openai#keep'), false);
  assert.match(h.state.reviewNotices.OpenAI.message, /1 rejected; 1 could not/);
  assert.deepEqual(Array.from(h.state.reviewNotices.OpenAI.undo), ['openai#keep']);
  h.context.reply = async () => ({ok:true,results:[{pk:'openai#keep',ok:true}]});
  await h.context.reviewAction('undo','OpenAI',h.state.reviewNotices.OpenAI.undo);
  assert.equal(h.state.apps[0].status, 'tailored');
  assert.equal(h.requests.every(r => r.url === '/actions/review-decision'), true);
});

test('shift-click selects only a contiguous range in the visible company', () => {
  const h = reviewHarness();
  h.state.apps.push({pk:'openai#third',company:'OpenAI',title:'Third role',status:'tailored'});
  const tick = (pk, checked) => ({dataset:{reviewPick:pk},checked,closest:()=>null});
  h.context.paintReviewSelection(tick('openai#keep',true));
  h.context.paintReviewSelection(tick('openai#third',true),true);
  assert.deepEqual(Array.from(h.state.reviewPicked), ['openai#keep','openai#reject','openai#third']);
  h.context.paintReviewSelection(tick('beta#keep',true),true);
  assert.equal(h.state.reviewPicked.size, 4);
});

test('server-saved skips refresh the view and legacy migration never approves', async () => {
  const h = reviewHarness();
  const before = h.context.appsSig();
  h.state.apps[0].review_skipped = true;
  assert.notEqual(h.context.appsSig(), before);
  h.state.reviewSkipped.add('openai#reject');
  await h.context.migrateReviewSkips();
  assert.deepEqual(h.requests[0], {url:'/actions/review-decision',body:{company:'OpenAI',pks:['openai#reject'],action:'skip'}});
  assert.equal(h.state.reviewSkipped.size, 0);
  assert.equal(h.state.apps[1].review_skipped, true);
});

test('collapsed review keeps its count and selection while hiding the body', () => {
  const h = reviewHarness();
  h.state.reviewCollapsed = true;
  h.state.reviewPicked.add('openai#keep');
  const markup = h.context.reviewQueueSec(h.context.visibleApprovalPicks());
  assert.match(markup, /data-review-collapse aria-expanded="false"/);
  assert.match(markup, /id="review-body" hidden/);
  assert.match(markup, /3 need approval/);
  assert.equal(h.state.reviewPicked.has('openai#keep'), true);
});


test('one collapsible queue includes ready, waiting and applying with safe selection', async () => {
  const h = reviewHarness();
  h.state.queue = {pending:[{pk:'openai#reject',company:'OpenAI'}],in_flight:['beta#keep']};
  const markup = h.context.viewPipeline();
  assert.equal((markup.match(/id="review-queue"/g)||[]).length, 1);
  assert.doesNotMatch(markup, /class="psec ps-(queued|flight)/);
  assert.match(markup, /Application queue/);
  assert.match(markup, /review-status ready">Needs approval/);
  assert.match(markup, /review-status waiting">Waiting to apply/);
  assert.match(markup, /review-status applying">Applying/);
  assert.match(markup, /data-review-pick="beta#keep"[^>]*disabled/);
  assert.match(markup, /Stop all applying/);
  h.context.paintReviewSelection({dataset:{reviewPick:'beta#keep'},checked:true});
  assert.equal(h.state.reviewPicked.has('beta#keep'), false);
  await h.context.reviewAction('apply', 'Beta');
  assert.equal(h.requests.length, 0);
  h.state.reviewPicked.add('openai#reject');
  await h.context.reviewAction('skip', 'OpenAI');
  assert.deepEqual(h.requests[0].body, {company:'OpenAI',pks:['openai#reject'],action:'skip'});
});

test('a selection that starts or completes applying never falls back to Apply all', async () => {
  for (const status of ['submitting', 'applied']) {
    const h = reviewHarness();
    h.state.reviewPicked.add('openai#reject');
    h.state.apps[1].status = status;
    await h.context.reviewAction('apply', 'OpenAI');
    assert.equal(h.requests.length, 0);
  }
});

test('phase filters preserve exact company scope and skip running roles in a range', () => {
  const h = reviewHarness();
  h.state.apps.push({pk:'openai#third',company:'OpenAI',status:'tailored',title:'Third'});
  h.state.queue = {pending:[],in_flight:['openai#reject']};
  h.context.paintReviewSelection({dataset:{reviewPick:'openai#keep'},checked:true});
  h.context.paintReviewSelection({dataset:{reviewPick:'openai#third'},checked:true}, true);
  assert.deepEqual(Array.from(h.state.reviewPicked), ['openai#keep','openai#third']);
  h.state.reviewFilter = 'applying';
  assert.deepEqual(Array.from(h.context.reviewRows('OpenAI'), r=>r.pk), ['openai#reject']);
  assert.deepEqual(Array.from(h.context.reviewRows('Beta')), []);
});

test('Force apply is offered only for a low-score skip and confirms final review', async () => {
  const h = reviewHarness();
  const row = h.state.apps[0];
  Object.assign(row, {status:'skipped',skip_reason:'low_score',match_score:6});
  assert.match(h.context.closedRow(row), /data-act="force-apply"/);
  let prompt;
  h.context.confirm = message => { prompt=message; return false; };
  await h.context.forceApply(row.pk);
  assert.equal(h.requests.length, 0);
  assert.match(prompt, /final approval/);
  assert.match(prompt, /does not submit yet/);
  h.context.confirm = () => true;
  h.context.reply = async () => ({ok:true,next:"review"});
  h.context.markTailoring = pk => { h.state.apps.find(r=>r.pk===pk).status='tailoring'; };
  await h.context.forceApply(row.pk);
  assert.deepEqual(h.requests, [{url:'/actions/force-apply/openai%23keep',body:undefined}]);
  assert.equal(row.status, 'tailoring');
  assert.equal(row.score_override, true);
  row.status='skipped'; row.skip_reason='review_rejected';
  assert.doesNotMatch(h.context.closedRow(row), /data-act="force-apply"/);
});

test('Force apply failures preserve the skipped role and repeated clicks do not widen scope', async () => {
  const h=reviewHarness(), row=h.state.apps[0];
  Object.assign(row,{status:'skipped',skip_reason:'low_score',match_score:6});
  let finish;
  h.context.reply=()=>new Promise(resolve=>{finish=resolve;});
  const pending=h.context.forceApply(row.pk);
  await h.context.forceApply(row.pk);
  assert.equal(h.requests.length,1);
  finish({ok:false,error:'Reader unavailable'}); await pending;
  assert.equal(row.status,'skipped');
  assert.equal(h.state.forcePreparing.size,0);
});

test('posting read errors are visible in Found and trigger a refresh when the reader recovers', () => {
  const h=reviewHarness(), row=h.state.apps[0];
  Object.assign(row,{status:'found',jd_read_error:'Claude session limit resets at 10:10pm'});
  assert.match(h.context.foundRow(row), /Posting read paused/);
  assert.match(h.context.foundRow(row), /resets at 10:10pm/);
  const before=h.context.appsSig(); row.jd_read_error='';
  assert.notEqual(h.context.appsSig(),before);
});

test('drawer Force apply routes to preparation and unfamiliar actions never skip a role', async () => {
  const h=reviewHarness(), row=h.state.apps[0];
  Object.assign(row,{status:'skipped',skip_reason:'low_score',match_score:6});
  h.context.reply=async()=>({ok:true,next:'review'});
  h.context.markTailoring=()=>{}; h.context.closeDrawer=()=>{};
  const start=source.indexOf('  $("#drawer").addEventListener("click", (e) => {');
  const end=source.indexOf('  $("#scrim").addEventListener("click", closeDrawer);',start);
  vm.runInContext(source.slice(start,end),h.context);
  const click=kind=>h.node('#drawer').emit('click',{target:{closest:selector=>selector==='[data-act]'?{dataset:{act:kind,pk:row.pk}}:null}});
  await click('force-apply');
  assert.deepEqual(h.requests.map(r=>r.url),['/actions/force-apply/openai%23keep']);
  await click('unrecognised-action');
  assert.equal(h.requests.length,1);
});
