// Offline stream regressions: incomplete packets must never erase UI drafts.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../web/career-board.js'), 'utf8');
function harness(fetch) {
  let Board;
  const context = vm.createContext({HTMLElement: class {}, customElements: {define: (_, cls) => Board = cls},
    window: {}, fetch, AbortController, TextDecoder, Date, Set});
  vm.runInContext(source.replace("import { auth } from './auth.js';", "const auth = {header: () => ({Authorization:'Bearer test'})};"), context);
  const board = new Board();
  board.data = {running:true}; board.hidden = false;
  board.selected = new Set(['selected-role']); board.busy = false;
  board.paintStatus = () => {};
  return board;
}

test('split UTF-8 progress reaches the UI before completion without changing a selection', async () => {
  let controller, painted = [], refreshed = 0, headers;
  const body = new ReadableStream({start: c => controller = c});
  const board = harness(async (_, opts) => {
    headers = opts.headers;
    return new Response(body, {headers:{'content-type':'text/event-stream'}});
  });
  board.paintStatus = () => painted.push(board.data.progress.events[0].message);
  board.refresh = async () => {refreshed++; board.data.running = false;};
  const running = board.streamProgress();
  const message = 'Searched: Seattle — developer tools';
  const packet = new TextEncoder().encode('data: ' + JSON.stringify({running:true,run_id:'run',events:[{seq:1,message}],active_search:{}}) + '\n\n');
  for (const byte of packet) controller.enqueue(Uint8Array.of(byte));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(painted, [message], 'A live update must arrive while the stream is still open');
  assert.deepEqual([...board.selected], ['selected-role']);
  assert.equal(headers.Authorization, 'Bearer test');
  controller.enqueue(new TextEncoder().encode('data: {"running":false,"events":[]}\n\n'));
  await running;
  assert.equal(refreshed, 1);
  assert.equal(board.progressController, null);
});

test('a broken connection releases the stream so polling can reconnect', async () => {
  let attempts = 0;
  const board = harness(async () => { attempts++; throw Error('offline'); });
  await board.streamProgress();
  assert.equal(board.progressController, null);
  await board.streamProgress();
  assert.equal(attempts, 2);
  assert.deepEqual([...board.selected], ['selected-role']);
});

test('hidden and idle search pages do not open streaming connections', async () => {
  const board = harness(async () => { throw Error('Should not fetch'); });
  board.hidden = true;
  await board.streamProgress();
  board.hidden = false; board.data.running = false;
  await board.streamProgress();
  assert.equal(board.progressController, undefined);
});
