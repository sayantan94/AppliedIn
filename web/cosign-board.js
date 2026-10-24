// Cosign search. Mounted outside #pane, like <career-board>, so pipeline polling
// never repaints it mid-selection. Picking a role only stages it: Score & tailor
// stops for review; Apply scores, tailors and submits what clears the bar.
// `network="a16z"` points the same board at the a16z portfolio index, whose
// routes (/a16z/…) answer in the same shape as Cosign's.
import { auth } from './auth.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const still = () => typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
const money = v => v >= 1000 ? `$${Math.round(v / 1000)}K` : `$${v}`;
const salary = job => job.salary_min && job.salary_max ? `${money(job.salary_min)} – ${money(job.salary_max)}`
  : job.salary_min || job.salary_max ? money(job.salary_min || job.salary_max) : '';
const age = ms => {
  if (!ms) return '';
  const mins = Math.max(1, Math.round((Date.now() - ms) / 60000));
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  const days = Math.round(mins / 1440);
  return days < 60 ? `${days}d ago` : `${Math.round(days / 30)}mo ago`;
};
// Cosign repeats each place in several spellings ("Austin, TX · Austin, Texas,
// United States") yet also lists several cities in one string ("San Francisco,
// Seattle, New York"). Drop the state and country parts, keep every city once.
const REGIONS = new Set(['united states', 'usa', 'us', 'united kingdom', 'uk', 'canada', 'india', 'germany', 'france',
  'alabama', 'alaska', 'arizona', 'arkansas', 'california', 'colorado', 'connecticut', 'delaware', 'florida', 'georgia',
  'hawaii', 'idaho', 'illinois', 'indiana', 'iowa', 'kansas', 'kentucky', 'louisiana', 'maine', 'maryland',
  'massachusetts', 'michigan', 'minnesota', 'mississippi', 'missouri', 'montana', 'nebraska', 'nevada',
  'new hampshire', 'new jersey', 'new mexico', 'north carolina', 'north dakota', 'ohio', 'oklahoma', 'oregon',
  'pennsylvania', 'rhode island', 'south carolina', 'south dakota', 'tennessee', 'texas', 'utah', 'vermont',
  'virginia', 'washington', 'west virginia', 'wisconsin', 'wyoming', 'ontario', 'british columbia', 'england']);
export const places = (location, max = 3) => {
  const seen = new Map();
  for (const part of String(location || '').split(/\s+·\s+|;\s*/)) {
    const chunks = part.split(',').map(c => c.trim());
    chunks.forEach((city, i) => {
      if (!city || (i > 0 && (/^[A-Z]{2}$/.test(city) || REGIONS.has(city.toLowerCase())))) return;
      const key = city.toLowerCase().replace(/[^a-z]/g, '');
      // "Rome, NY" keeps its state so it is not read as Rome, Italy.
      const code = /^[A-Z]{2}$/.test(chunks[i + 1] || '') ? `${city}, ${chunks[i + 1]}` : city;
      if (!seen.has(key)) seen.set(key, code);
      else if (code.length > seen.get(key).length) seen.set(key, code);
    });
  }
  const all = [...seen.values()];
  return all.length > max ? `${all.slice(0, max).join(' · ')} +${all.length - max}` : all.join(' · ');
};
const initials = name => esc(String(name || '?').split(/\s+/).map(w => w[0]).join('').slice(0, 2).toUpperCase());
const logo = (job, size) => job.logo
  ? `<img class="co-logo" src="${esc(job.logo.replace(/^http:/, 'https:'))}" alt="" width="${size}" height="${size}" loading="lazy" referrerpolicy="no-referrer" data-initials="${initials(job.company)}">`
  : `<span class="co-logo co-logo-text" style="--s:${size}px">${initials(job.company)}</span>`;
const SKELETON = n => Array.from({length: n}, (_, i) =>
  `<li class="co-skel" aria-hidden="true" style="--i:${i}"><span class="co-skel-box"></span><span class="co-skel-lines"><i></i><i></i><i></i></span></li>`).join('');
const STEP_NAMES = ['Score', 'Tailor', 'Check', 'Apply', 'Submitted'];
const clock = at => { const d = new Date(at); return Number.isFinite(+d) ? d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) : ''; };
const FINAL = new Set(['applied', 'attention']);
const NETWORKS = {cosign: 'Cosign network', a16z: 'a16z portfolio'};
// What was sent from this board, per viewer, so the pane survives a reload.
// Browser storage is a convenience here; the pipeline holds the real record.
const sentKey = network => network === 'cosign' ? 'appliedin.cosign.sent' : `appliedin.${network}.sent`;
const loadSent = network => { try { return new Map(JSON.parse(localStorage.getItem(sentKey(network)) || '[]')); } catch { return new Map(); } };
const saveSent = (network, sent) => { try { localStorage.setItem(sentKey(network), JSON.stringify([...sent].slice(-300))); } catch { /* private window */ } };
const CHECK = '<svg class="co-check" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 8.5 6.5 11.5 12.5 4.5"/></svg>';
// Adds a city's page to the results in place: a role listed in two cities appears
// once, and several cities interleave newest first. Returns only the new roles.
export function merge(jobs, page, interleave) {
  const known = new Set(jobs.map(j => j.id));
  const fresh = page.filter(j => !known.has(j.id) && known.add(j.id));
  jobs.push(...fresh);
  if (interleave) jobs.sort((a, b) => (b.posted || 0) - (a.posted || 0));
  return fresh;
}
// Where a posting actually lives, read from its address: the board it will be
// applied on, not the index that found it.
const BOARDS = [[/ashbyhq\.com/, 'Ashby'], [/greenhouse\.io/, 'Greenhouse'], [/lever\.co/, 'Lever'],
  [/myworkday(jobs|site)\.com/, 'Workday'], [/icims\.com/, 'iCIMS'], [/smartrecruiters\.com/, 'SmartRecruiters'],
  [/oraclecloud\.com/, 'Oracle'], [/workable\.com/, 'Workable']];
export const sourceOf = url => {
  let host = '';
  try { host = new URL(url).hostname; } catch { return ''; }
  const hit = BOARDS.find(([re]) => re.test(host));
  return hit ? hit[1] : host.replace(/^(www|careers|jobs|boards|job-boards)\./, '');
};
const STATES = {pipeline:'In pipeline', handled:'Already handled', dismissed:'Dismissed'};
// Cosign serves 30 roles a request; a useful first screen is about a hundred.
const TARGET = 100;
const STAGE_MAX = 200;  // matches cosign.STAGE_MAX on the server
const SALARIES = [0, 100000, 150000, 200000, 250000, 300000];

// A number that rolls into place digit by digit, like an odometer. Each digit is
// a reel of 0–9 twice over, so it always travels at least one full turn.
export function odometer(el, value) {
  const text = Number(value).toLocaleString('en-US');
  el.setAttribute('aria-label', text);
  el.innerHTML = [...text].map((ch, i) => /\d/.test(ch)
    ? `<span class="co-digit" aria-hidden="true"><span class="co-digit-w">${ch}</span><span class="co-reel" style="--to:${-1.2 * (10 + Number(ch))}em;--delay:${45 * i}ms">${Array.from({length: 20}, (_, n) => `<span>${n % 10}</span>`).join('')}</span></span>`
    : `<span aria-hidden="true">${ch}</span>`).join('');
}

class CosignBoard extends HTMLElement {
  static observedAttributes = ['hidden'];
  connectedCallback() {
    if (this.ready) return;
    this.ready = true;
    this.network = NETWORKS[this.getAttribute('network')] ? this.getAttribute('network') : 'cosign';
    this.jobs = []; this.selected = new Set(); this.cursors = {}; this.done = true;
    this.cities = []; this.progress = {}; this.region = 'us'; this.preferred = [];
    this.sent = loadSent(this.network); this.live = {}; this.paneView = 'role';
    this.active = null; this.showExcluded = false; this.examples = [];
    this.innerHTML = `<section class="co-page">
      <header class="co-hero">
        <p class="co-eyebrow">${NETWORKS[this.network]}</p>
        <h2>Search <span class="co-count">open</span> roles</h2>
        <p class="co-sub">Starts from your preferences. Anything you pick is scored and tailored first, then waits for your approval.</p>
        <form class="co-composer" role="search">
          <div class="co-compose-top">
          <label class="co-query">
            <svg aria-hidden="true" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>
            <span class="co-sr">Role or keywords</span>
            <input class="co-term" type="search" maxlength="200" autocomplete="off" spellcheck="false">
            <span class="co-example-slot" aria-hidden="true"><span class="co-example"></span></span>
          </label>
          <button type="submit" class="btn btn-primary co-go"><span class="co-go-text">Search</span><span class="co-go-icon" aria-hidden="true"></span></button>
          </div>
          <div class="co-refine">
            <label class="co-pillsel"><span class="co-sr">Role type</span><select class="co-role"><option value="">Any function</option></select></label>
            <div class="co-cities">
              <button type="button" class="co-cities-btn" aria-haspopup="true" aria-expanded="false"><svg aria-hidden="true" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11Z"/><circle cx="12" cy="10" r="2.3"/></svg><span class="co-cities-label">All of the US</span><svg aria-hidden="true" viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="m6 9 6 6 6-6"/></svg></button>
              <div class="co-cities-pop" role="group" aria-label="Cities" hidden>
                <div class="co-scope" role="radiogroup" aria-label="Where"><button type="button" role="radio" data-scope="us">All of the US</button><button type="button" role="radio" data-scope="">Anywhere</button></div>
                <button type="button" class="co-link co-preferred" hidden></button>
                <input class="co-cities-filter" type="search" placeholder="Or pick cities…" aria-label="Find a city">
                <p class="co-cities-note"></p>
                <div class="co-cities-list"></div>
                <div class="co-cities-foot"><button type="button" class="co-link co-cities-any">Clear cities</button><button type="button" class="btn btn-primary co-cities-done">Done</button></div>
              </div>
            </div>
            <label class="co-pillsel"><span class="co-sr">Minimum salary</span><select class="co-salary">${SALARIES.map(v => `<option value="${v}">${v ? money(v) + '+' : 'Any salary'}</option>`).join('')}</select></label>
            <button type="button" class="chip co-remote" aria-pressed="false">Remote only</button>
          </div>
        </form>
        <p class="co-prefs"></p>
      </header>
      <div class="co-loadbar" aria-hidden="true"></div>
      <div class="co-workspace">
        <section class="co-list-pane" aria-label="Search results">
          <div class="co-list-head"><label class="co-all" hidden><input type="checkbox"><span>Select all</span></label><span class="co-status" role="status" aria-live="polite"></span><button type="button" class="co-link co-toggle-excluded" hidden></button><div class="co-progress" aria-label="Search progress by city"></div></div>
          <ol class="co-list"></ol>
          <button type="button" class="btn co-more" hidden>Load more roles</button>
          <div class="co-selbar" hidden><div class="co-selrow"><span class="co-selcount"></span><button type="button" class="co-link co-clear">Clear</button><span class="co-spacer"></span><button type="button" class="btn co-prepare">Score & tailor</button><button type="button" class="btn btn-amber co-apply">Apply</button></div><p class="co-selnote">Each role is scored first. Only roles that clear your match bar are tailored and submitted, one at a time.</p></div>
        </section>
        <article class="co-detail" aria-live="polite"><div class="co-empty-detail">Pick a role to read it here.</div></article>
      </div>
    </section>`;
    this.$('.co-composer').onsubmit = event => { event.preventDefault(); this.search(); };
    this.$('.co-remote').onclick = event => {
      const on = event.currentTarget.getAttribute('aria-pressed') !== 'true';
      event.currentTarget.setAttribute('aria-pressed', String(on));
      this.search();
    };
    for (const name of ['.co-role', '.co-salary']) this.$(name).onchange = () => { this.paintFilters(); this.search(); };
    this.wireCities();
    this.$('.co-term').oninput = () => this.paintExample();
    this.$('.co-more').onclick = () => { this.target = this.visible().length + TARGET; this.search(true); };
    this.$('.co-all input').onchange = event => {
      const pickable = this.visible().filter(j => j.state === 'new').slice(0, STAGE_MAX);
      if (event.target.checked) pickable.forEach(j => this.selected.add(j.id)); else this.selected.clear();
      this.renderList(new Set());
    };
    this.$('.co-toggle-excluded').onclick = () => { this.showExcluded = !this.showExcluded; this.renderList(); };
    this.$('.co-clear').onclick = () => { this.selected.clear(); this.renderList(); };
    this.$('.co-prepare').onclick = () => this.stage([...this.selected], false);
    this.$('.co-apply').onclick = () => this.stage([...this.selected], true);
    this.$('.co-prefs').onclick = event => { if (event.target.closest('.co-reset')) { this.seed(this.defaults); this.search(); } };
    this.$('.co-list').onclick = event => {
      const row = event.target.closest('.co-row');
      if (!row) return;
      if (event.target.closest('.co-pick')) {
        const box = row.querySelector('input');
        box.checked ? this.selected.add(row.dataset.id) : this.selected.delete(row.dataset.id);
        this.paintSelection();
        return;
      }
      this.open(row.dataset.id);
    };
    this.$('.co-list').onkeydown = event => {
      if (!['ArrowDown', 'ArrowUp', 'j', 'k'].includes(event.key)) return;
      const rows = [...this.querySelectorAll('.co-row')];
      const at = rows.findIndex(r => r.dataset.id === this.active);
      const next = rows[Math.min(rows.length - 1, Math.max(0, at + (['ArrowDown', 'j'].includes(event.key) ? 1 : -1)))];
      if (next) { event.preventDefault(); this.open(next.dataset.id); next.querySelector('.co-open').focus(); }
    };
    this.$('.co-detail').onclick = event => {
      const button = event.target.closest('[data-act]');
      if (button) this.stage([this.active], button.dataset.act === 'apply');
      if (event.target.closest('.co-expand')) this.$('.co-desc').classList.remove('co-collapsed'), event.target.remove();
      if (event.target.closest('.co-back')) this.classList.remove('co-reading');
      const tab = event.target.closest('[data-pane]');
      if (tab) tab.dataset.pane === 'apps' ? this.showApps() : this.active && this.open(this.active);
      if (event.target.closest('.co-answer')) this.dispatchEvent(new CustomEvent('cosign-needs', {bubbles: true}));
      if (event.target.closest('.co-clear-done')) {
        for (const [pk, a] of this.sent) if (FINAL.has(this.live[pk]?.phase)) this.sent.delete(pk);
        saveSent(this.network, this.sent); this.sent.size ? this.showApps() : this.active && this.open(this.active);
      }
      const toggle = event.target.closest('.co-steps-toggle');
      if (toggle) {
        const tl = toggle.closest('.co-app').querySelector('.co-timeline');
        tl.hidden = !tl.hidden; toggle.setAttribute('aria-expanded', String(!tl.hidden));
        toggle.textContent = tl.hidden ? 'Steps' : 'Hide steps';
        return;
      }
      // Anywhere else on an application opens it in the pipeline, in a window
      // of its own, so the search and this pane stay where they were.
      const app = event.target.closest('.co-app[data-pk]');
      if (app && !event.target.closest('a, button, .co-timeline')) {
        const url = `${location.pathname}?job=${encodeURIComponent(app.dataset.pk)}`;
        window.open(url, 'appliedin-pipeline', 'popup,width=1280,height=900');
      }
    };
    // A logo that fails to load becomes the company's initials, not a broken image.
    this.addEventListener('error', event => {
      const img = event.target;
      if (img.tagName !== 'IMG' || !img.classList.contains('co-logo')) return;
      const box = document.createElement('span');
      box.className = 'co-logo co-logo-text'; box.style.setProperty('--s', img.width + 'px');
      box.textContent = img.dataset.initials; img.replaceWith(box);
    }, true);
    // Filters stay locked until preferences arrive; otherwise seeding them would
    // silently undo whatever the owner changed in the meantime.
    this.lock(true);
    if (!this.hidden) this.load();
  }
  paintFilters() {
    for (const name of ['.co-role', '.co-salary']) this.$(name).closest('label').classList.toggle('co-on', !['', '0'].includes(this.$(name).value));
  }
  lock(on) { this.querySelectorAll('.co-composer :is(input, select, button)').forEach(el => el.disabled = on); }
  disconnectedCallback() { clearInterval(this.rolodex); }
  attributeChangedCallback(name) {
    if (!this.ready) return;
    if (this.hidden) clearInterval(this.rolodex);
    else if (!this.options) this.load();
    else { this.startRolodex(); this.poll(); }
  }
  $(selector) { return this.querySelector(selector); }

  async request(path, body) {
    const cfg = window.APPLIEDIN_CONFIG || {};
    if (cfg.demo === true || new URLSearchParams(location.search).has('demo')) throw new Error(`${NETWORKS[this.network]} search is available when connected to your local AppliedIn server.`);
    const response = await fetch((cfg.apiUrl || '').replace(/\/$/, '') + `/${this.network}` + path, {
      method: body === undefined ? 'GET' : 'POST', headers: {'Content-Type': 'application/json', ...auth.header()},
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(response.status === 404 ? `Restart AppliedIn to enable ${NETWORKS[this.network]} search.` : typeof result.detail === 'string' ? result.detail : 'Could not complete this action. Try again.');
    return result;
  }

  async load() {
    if (this.loadingOptions) return;
    this.loadingOptions = true;
    try {
      const options = this.options = await this.request('');
      this.$('.co-role').insertAdjacentHTML('beforeend', Object.entries(options.roles).map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join(''));
      this.maxCities = options.max_cities || 5;
      if (options.count) odometer(this.$('.co-count'), options.count);
      this.defaults = options.defaults;
      this.examples = options.examples?.length ? options.examples : ['Staff engineer, AI infrastructure', 'Platform engineer', 'Developer tools'];
      const hide = options.excluding || [];
      this.$('.co-prefs').innerHTML = `${hide.length ? `Hiding titles with ${hide.slice(0, 4).map(w => `<b>${esc(w)}</b>`).join(', ')}${hide.length > 4 ? ` and ${hide.length - 4} more` : ''} from your preferences. ` : ''}<button type="button" class="co-link co-reset">Reset to my preferences</button>`;
      this.seed(options.defaults);
      this.lock(false);
      this.startRolodex();
      this.poll();
      this.search();
    } catch (error) {
      this.status(error.message, true);
    } finally {
      this.loadingOptions = false;
    }
  }

  seed(d) {
    if (!d) return;
    this.$('.co-term').value = d.term || '';
    this.$('.co-role').value = d.role || '';
    this.region = d.region ?? 'us';
    this.preferred = (d.preferred || []).filter(c => this.options?.cities?.[c]);
    this.setCities(d.cities || []);
    this.$('.co-salary').value = String(d.min_salary || 0);
    this.$('.co-remote').setAttribute('aria-pressed', String(!!d.remote));
    this.paintFilters();
    this.paintExample();
  }
  filters() {
    return {term: this.$('.co-term').value.trim(), role: this.$('.co-role').value,
      min_salary: Number(this.$('.co-salary').value), remote: this.$('.co-remote').getAttribute('aria-pressed') === 'true'};
  }

  // Example searches flip up like a rolodex while the box is empty.
  startRolodex() {
    clearInterval(this.rolodex);
    if (!this.examples.length) return;
    this.exampleAt = this.exampleAt ?? 0;
    this.paintExample();
    if (still()) return;
    this.rolodex = setInterval(() => {
      if (this.$('.co-term').value) return;
      this.exampleAt = (this.exampleAt + 1) % this.examples.length;
      this.paintExample(true);
    }, 2800);
  }
  paintExample(flip = false) {
    const slot = this.$('.co-example-slot');
    slot.hidden = !!this.$('.co-term').value || !this.examples.length;
    if (slot.hidden) return;
    const next = document.createElement('span');
    next.className = 'co-example' + (flip ? ' co-flip' : '');
    next.textContent = this.examples[this.exampleAt || 0];
    slot.replaceChildren(next);
  }

  status(text, error = false) {
    const el = this.$('.co-status');
    if (el.textContent === text) return;
    el.textContent = text;
    el.classList.toggle('co-error', error);
    el.classList.remove('co-swap'); void el.offsetWidth; el.classList.add('co-swap');
  }

  // Cosign filters one city per search, so several cities are several requests.
  // Each city resolves on its own: its pill settles and its roles land as soon as
  // it answers, rather than everything waiting on the slowest city.
  async search(more = false, auto = false) {
    const seq = this.seq = (this.seq || 0) + 1;
    if (!auto) this.fills = 0;
    if (!more) this.target = TARGET;
    this.filling = more;
    const filters = this.filters();
    const targets = more ? Object.keys(this.cursors).filter(c => this.cursors[c])
      : this.cities.length ? [...this.cities] : ['*'];
    if (!targets.length) return;
    this.paintExample();
    if (!more) {
      this.jobs = []; this.cursors = {}; this.progress = {}; this.selected.clear(); this.done = false;
      this.$('.co-progress').replaceChildren();
      this.$('.co-toggle-excluded').hidden = true;
      this.skeleton();
      this.status('Searching…');
    }
    for (const city of targets) this.progress[city] = {...this.progress[city], state: 'busy'};
    this.paintProgress();
    this.busy_(true);
    let opened = more;
    await Promise.all(targets.map(async city => {
      try {
        const page = await this.request('/search', {...filters, city: city === '*' ? '' : city, region: this.region, cursor: more ? this.cursors[city] : null});
        if (seq !== this.seq) return;  // a newer search replaced this one
        this.cursors[city] = page.done ? null : page.cursor;
        const fresh = merge(this.jobs, page.jobs, targets.length > 1 || this.cities.length > 1);
        const shown = page.jobs.filter(j => this.showExcluded || !j.excluded).length;
        this.progress[city] = {state: 'done', count: (this.progress[city].count || 0) + shown};
        this.done = Object.values(this.cursors).every(c => !c) && Object.keys(this.cursors).length >= targets.length;
        if (this.jobs.length) this.renderList(new Set(fresh.map(j => j.id)));
        if (!opened && this.visible().length) {
          opened = true;
          // On a phone the reader covers the list, so only open a role when asked.
          // Keep the role being read if it survived the new search.
          const reading = this.visible().some(j => j.id === this.active);
          if (!reading && this.paneView !== 'apps' && !(typeof matchMedia === 'function' && matchMedia('(max-width: 860px)').matches)) this.open(this.visible()[0].id);
        }
      } catch (error) {
        if (seq !== this.seq) return;
        this.cursors[city] = null;
        this.progress[city] = {...this.progress[city], state: 'fail', error: error.message};
      } finally {
        if (seq === this.seq) this.paintProgress();
      }
    }));
    if (seq !== this.seq) return;
    this.done = Object.values(this.cursors).every(c => !c);
    // Keep reading pages until about a hundred roles show. A bounded number of
    // rounds, so a narrow search that genuinely has few roles still finishes.
    if (!this.done && this.visible().length < this.target && (this.fills = (this.fills || 0) + 1) <= 8) {
      this.filling = true;
      if (this.jobs.length) this.renderList(new Set());
      return this.search(true, true);
    }
    this.filling = false;
    this.busy_(false);
    const failed = targets.filter(c => this.progress[c]?.state === 'fail');
    if (this.jobs.length) this.renderList(new Set());
    else {
      this.$('.co-list').replaceChildren();
      this.$('.co-more').hidden = true;
      this.$('.co-detail').innerHTML = '<div class="co-empty-detail">No roles match. Loosen a filter or add a city.</div>';
    }
    if (failed.length === targets.length) this.status(this.progress[failed[0]].error, true);
    else if (!this.jobs.length) this.status('No roles found');
    else if (failed.length) this.status(`${this.$('.co-status').textContent} · ${failed.map(c => this.cityName(c)).join(', ')} unavailable`, true);
  }

  // The button carries the search's own state: a spinner while any city is out,
  // then a check that draws itself before the label returns.
  busy_(on) {
    const go = this.$('.co-go');
    clearTimeout(this.goTimer);
    this.classList.toggle('co-loading', on);
    this.$('.co-more').disabled = on;
    go.dataset.state = on ? 'busy' : 'done';
    go.querySelector('.co-go-icon').innerHTML = on ? '<span class="co-spinner"></span>' : CHECK;
    if (!on) this.goTimer = setTimeout(() => { go.dataset.state = ''; go.querySelector('.co-go-icon').replaceChildren(); }, 900);
  }

  skeleton() {
    this.$('.co-more').hidden = true;
    this.$('.co-list').innerHTML = SKELETON(7);
  }

  // One pill per city. Pills are updated in place so only a pill whose state
  // changed replays its animation.
  paintProgress() {
    const box = this.$('.co-progress');
    // "Anywhere" is a single plain search; every other scope gets a pill.
    const cities = Object.keys(this.progress).filter(c => c !== '*' || this.region);
    box.hidden = !cities.length;
    for (const city of cities) {
      const p = this.progress[city];
      let pill = box.querySelector(`[data-city="${CSS.escape(city)}"]`);
      if (!pill) {
        pill = document.createElement('span');
        pill.className = 'co-pill'; pill.dataset.city = city;
        pill.innerHTML = `<span class="co-pill-icon"></span><span>${esc(this.cityName(city))}</span><b></b>`;
        box.append(pill);
      }
      if (pill.dataset.state !== p.state) {
        pill.dataset.state = p.state;
        pill.querySelector('.co-pill-icon').innerHTML = p.state === 'busy' ? '<span class="co-spinner"></span>' : p.state === 'done' ? CHECK : '<span class="co-x">✕</span>';
        pill.title = p.error || '';
      }
      const count = p.state === 'fail' ? '' : p.count ? String(p.count) : '';
      const b = pill.querySelector('b');
      if (b.textContent !== count) { b.textContent = count; b.classList.remove('co-tick'); void b.offsetWidth; b.classList.add('co-tick'); }
    }
  }

  cityName(slug) { return slug && slug !== '*' ? this.options?.cities?.[slug] || slug : this.region === 'us' ? 'All of the US' : 'Anywhere'; }

  // ─── City picker ───
  wireCities() {
    const pop = this.$('.co-cities-pop'), btn = this.$('.co-cities-btn');
    const close = () => {
      if (pop.hidden) return;
      pop.hidden = true; btn.setAttribute('aria-expanded', 'false');
      clearTimeout(this.cityTimer);
      if (this.citiesDirty) { this.citiesDirty = false; this.search(); }
    };
    this.closeCities = close;
    btn.onclick = () => {
      if (!pop.hidden) return close();
      this.renderCityList(); pop.hidden = false; btn.setAttribute('aria-expanded', 'true');
      this.$('.co-cities-filter').value = ''; this.$('.co-cities-filter').focus();
    };
    this.$('.co-cities-filter').oninput = () => this.renderCityList();
    this.$('.co-cities-done').onclick = close;
    this.$('.co-cities-any').onclick = () => { this.setCities([]); this.citiesDirty = true; this.renderCityList(false); };
    this.$('.co-scope').onclick = event => {
      const b = event.target.closest('[data-scope]');
      if (!b) return;
      this.region = b.dataset.scope; this.setCities([]); this.citiesDirty = true; close();
    };
    this.$('.co-preferred').onclick = () => { this.setCities(this.preferred); this.citiesDirty = true; close(); };
    this.$('.co-cities-list').onchange = event => {
      const box = event.target;
      const next = box.checked ? [...this.cities, box.value] : this.cities.filter(c => c !== box.value);
      this.setCities(next.slice(0, this.maxCities || 5));
      this.renderCityList(false);
      this.citiesDirty = true;
      // Ticking several cities in a row should be one search, not one per tick.
      clearTimeout(this.cityTimer);
      this.cityTimer = setTimeout(() => { if (this.citiesDirty) { this.citiesDirty = false; this.search(); } }, 700);
    };
    this.addEventListener('keydown', event => { if (event.key === 'Escape' && !pop.hidden) { close(); btn.focus(); } });
    document.addEventListener('pointerdown', event => { if (!this.$('.co-cities').contains(event.target)) close(); });
  }

  setCities(list) {
    this.cities = [...new Set(list)].filter(c => this.options?.cities?.[c]);
    const names = this.cities.map(c => this.cityName(c));
    const label = !names.length ? this.cityName('*') : names.length === 1 ? names[0]
      : names.join(', ').length <= 24 ? names.join(', ') : `${names[0]} +${names.length - 1}`;
    this.$('.co-cities-label').textContent = label;
    this.$('.co-cities-btn').classList.toggle('co-on', !!names.length);
    this.$('.co-cities-btn').title = names.join(', ') || this.cityName('*');
    this.querySelectorAll('.co-scope [data-scope]').forEach(b => b.setAttribute('aria-checked', String(!names.length && b.dataset.scope === this.region)));
  }

  renderCityList(scroll = true) {
    const q = this.$('.co-cities-filter').value.trim().toLowerCase();
    const cap = this.maxCities || 5;
    const full = this.cities.length >= cap;
    const all = Object.entries(this.options?.cities || {});
    // Chosen cities stay on top so they can be removed without scrolling.
    const rows = [...this.cities.map(c => [c, this.cityName(c)]),
      ...all.filter(([c]) => !this.cities.includes(c)).sort((a, b) => a[1].localeCompare(b[1]))]
      .filter(([, name]) => !q || name.toLowerCase().includes(q));
    const pref = this.$('.co-preferred');
    pref.hidden = !this.preferred.length || this.preferred.join() === this.cities.join();
    pref.textContent = `Your cities: ${this.preferred.map(c => this.cityName(c)).join(', ')}`;
    this.$('.co-cities-any').hidden = !this.cities.length;
    this.$('.co-cities-note').textContent = full ? `Up to ${cap} cities; remove one to add another.` : `Pick up to ${cap}. Each city is searched separately.`;
    const list = this.$('.co-cities-list');
    list.innerHTML = rows.map(([c, name]) => {
      const on = this.cities.includes(c);
      return `<label class="co-city-opt${on ? ' co-on' : ''}"><input type="checkbox" value="${esc(c)}" ${on ? 'checked' : ''} ${!on && full ? 'disabled' : ''}>${esc(name)}</label>`;
    }).join('') || '<p class="co-cities-note">No city by that name.</p>';
    if (scroll) list.scrollTop = 0;
  }

  visible() { return this.jobs.filter(j => this.showExcluded || !j.excluded); }

  renderList(arrived = null) {
    const rows = this.visible();
    const hidden = this.jobs.length - this.jobs.filter(j => !j.excluded).length;
    this.status(this.jobs.length ? `${rows.length.toLocaleString()} role${rows.length === 1 ? '' : 's'}${this.filling ? ' · finding more…' : this.done ? '' : ' so far'}` : 'No roles found');
    const toggle = this.$('.co-toggle-excluded');
    toggle.hidden = !hidden;
    toggle.textContent = this.showExcluded ? `Hide ${hidden} excluded` : `Show ${hidden} excluded`;
    let stagger = 0;
    this.$('.co-list').innerHTML = rows.map(job => {
      const tag = STATES[job.state] || (job.excluded ? `Excluded · ${job.excluded}` : '');
      const enter = !arrived || arrived.has(job.id) ? ` co-enter" style="--i:${Math.min(stagger++, 12)}` : '';
      const meta = [salary(job), places(job.location, 2)].filter(Boolean).map(esc).join(' · ');
      return `<li class="co-row${job.excluded ? ' co-dim' : ''}${enter}" data-id="${esc(job.id)}" aria-selected="${job.id === this.active}">
        <label class="co-pick" title="${job.state === 'new' ? 'Select' : esc(tag)}"><input type="checkbox" ${this.selected.has(job.id) ? 'checked' : ''} ${job.state === 'new' ? '' : 'disabled'} aria-label="Select ${esc(job.title)}"></label>
        <button type="button" class="co-open">${logo(job, 34)}<span class="co-row-text"><span class="co-row-title">${esc(job.title)}</span><span class="co-row-company">${esc(job.company)}</span><span class="co-row-meta">${meta}</span></span><span class="co-row-side"><time>${esc(age(job.posted))}</time><span class="co-src">via ${esc(sourceOf(job.url))}</span>${tag ? `<span class="co-tag${STATES[job.state] ? ' co-tag-on' : ''}">${esc(tag)}</span>` : ''}</span></button></li>`;
    }).join('') + (this.filling ? SKELETON(2) : '');
    this.$('.co-more').hidden = this.done || !this.jobs.length || this.filling;
    this.paintSelection();
  }

  paintSelection() {
    for (const id of this.selected) if (!this.jobs.some(j => j.id === id && j.state === 'new')) this.selected.delete(id);
    const n = this.selected.size;
    const pickable = this.visible().filter(j => j.state === 'new').length;
    const all = this.$('.co-all');
    all.hidden = !pickable;
    all.querySelector('input').checked = n > 0 && n >= Math.min(pickable, STAGE_MAX);
    all.querySelector('input').indeterminate = n > 0 && n < Math.min(pickable, STAGE_MAX);
    all.querySelector('span').textContent = n ? `${n} selected` : `Select all ${Math.min(pickable, STAGE_MAX)}`;
    this.$('.co-selbar').hidden = !n;
    this.$('.co-selcount').textContent = `${n} selected`;
    this.$('.co-apply').textContent = n > 1 ? `Apply to ${n}` : 'Apply';
    this.$('.co-prepare').textContent = n > 1 ? `Score & tailor ${n}` : 'Score & tailor';
  }

  open(id) {
    const job = this.jobs.find(j => j.id === id);
    if (!job) return;
    this.active = id;
    this.paneView = 'role';
    this.querySelectorAll('.co-row').forEach(r => r.setAttribute('aria-selected', String(r.dataset.id === id)));
    this.classList.add('co-reading');
    const pay = salary(job);
    const meta = [places(job.location, 6), job.remote ? 'Remote' : '', `Posting on ${sourceOf(job.url)}`, job.employment.replace(/([a-z])([A-Z])/g, '$1 $2'), job.posted ? `Posted ${age(job.posted)}` : ''].filter(Boolean);
    const staged = job.state !== 'new';
    const live = job.pk && this.live[job.pk];
    this.$('.co-detail').innerHTML = `${this.paneTabs()}<div class="co-detail-body">
      <button type="button" class="co-link co-back">← Results</button>
      <div class="co-detail-org">${logo(job, 40)}<span>${esc(job.company)}${job.team ? `<small>${esc(job.team)}</small>` : ''}</span></div>
      <h3>${esc(job.title)}</h3>
      <p class="co-detail-meta">${meta.map(esc).join(' · ')}</p>
      ${pay ? `<p class="co-detail-pay">${esc(pay)}</p>` : ''}
      ${job.excluded ? `<p class="co-note">This title matches <b>${esc(job.excluded)}</b>, which your preferences exclude.</p>` : ''}
      <div class="co-detail-actions">${staged
        ? `<span class="co-tag co-tag-on">${esc(STATES[job.state] || job.state)}</span>${live ? `<span class="co-live" data-phase="${esc(live.phase)}">${esc(live.label)}</span>` : ''}`
        : `<button type="button" class="btn btn-primary" data-act="prepare">Score & tailor</button><button type="button" class="btn btn-amber" data-act="apply">Apply</button>`}
        <a class="btn btn-ghost" href="${esc(job.url)}" target="_blank" rel="noopener noreferrer">Posting ↗</a></div>
      ${staged ? '' : '<p class="co-hint">Score & tailor stops for your review. Apply scores, tailors and submits if it passes your bar.</p>'}
      <hr>
      <div class="co-desc${job.description.length > 1600 ? ' co-collapsed' : ''}">${esc(job.description || 'No description was found for this role. Open the posting to read it.')}</div>
      ${job.description.length > 1600 ? '<button type="button" class="co-link co-expand">Show full description</button>' : ''}
    </div>`;
    this.$('.co-detail').scrollTop = 0;
  }

  // ─── Applications pane: live progress of everything sent from this board ───
  paneTabs() {
    if (!this.sent.size) return '';
    const n = this.sent.size;
    return `<div class="co-pane-tabs" role="tablist"><button type="button" role="tab" data-pane="role" aria-selected="${this.paneView === 'role'}"${this.active ? '' : ' disabled'}>Role</button><button type="button" role="tab" data-pane="apps" aria-selected="${this.paneView === 'apps'}">Applications <b>${n}</b></button></div>`;
  }

  showApps() {
    if (!this.sent.size) return;
    this.paneView = 'apps';
    this.classList.add('co-reading');
    const rows = [...this.sent].reverse();
    this.$('.co-detail').innerHTML = `${this.paneTabs()}<div class="co-detail-body co-apps">
      <button type="button" class="co-link co-back">← Results</button>
      <h3>Your applications</h3>
      <p class="co-apps-summary" aria-live="polite"></p>
      <div class="co-meter" aria-hidden="true"><i data-k="applied"></i><i data-k="active"></i><i data-k="attention"></i><i data-k="skipped"></i></div>
      <p class="co-hint">Each role is scored first. Only roles that clear your match bar are tailored and submitted, one per company at a time. You're only asked when a form needs an answer you haven't given.</p>
      <ol class="co-app-list">${rows.map(([pk, a], i) => `<li class="co-app" data-pk="${esc(pk)}" data-id="${esc(a.id || '')}" style="--i:${Math.min(i, 12)}">
        <span class="co-app-icon"></span>
        <span class="co-app-text"><span class="co-app-title">${esc(a.title || 'Role')}</span><span class="co-app-co">${esc(a.company || '')}</span>
          <ol class="co-steps" aria-label="Progress">${STEP_NAMES.map(n => `<li><i></i><span>${n}</span></li>`).join('')}</ol>
          <span class="co-app-detail" hidden></span>
          <ol class="co-timeline" hidden></ol></span>
        <span class="co-app-side"><span class="co-app-label">Waiting to start</span><b class="co-app-score"></b>
          <button type="button" class="co-link co-steps-toggle" aria-expanded="false">Steps</button></span></li>`).join('')}</ol>
      <button type="button" class="co-link co-clear-done" hidden>Clear finished</button>
    </div>`;
    this.paintApps();
  }

  paintApps() {
    const counts = {applied: 0, active: 0, attention: 0, skipped: 0};
    for (const pk of this.sent.keys()) {
      const p = this.live[pk];
      const bucket = !p ? 'active' : p.phase === 'applied' ? 'applied' : p.status === 'skipped' ? 'skipped' : p.phase === 'attention' ? 'attention' : 'active';
      counts[bucket]++;
      const row = this.querySelector(`.co-app[data-pk="${CSS.escape(pk)}"]`);
      if (!row || !p) continue;
      if (row.dataset.phase !== p.phase || row.dataset.status !== p.status) {
        row.dataset.phase = p.phase; row.dataset.status = p.status;
        row.querySelector('.co-app-icon').innerHTML = p.phase === 'applied' ? CHECK
          : p.phase === 'attention' ? `<span class="co-mark">${p.status === 'skipped' ? '–' : '!'}</span>`
          : p.phase === 'queued' || p.phase === 'unknown' ? '<span class="co-dot"></span>' : '<span class="co-spinner"></span>';
      }
      const label = row.querySelector('.co-app-label');
      if (label.textContent !== p.label) { label.textContent = p.label; label.classList.remove('co-swap'); void label.offsetWidth; label.classList.add('co-swap'); }
      row.querySelector('.co-app-score').textContent = p.match_score != null ? `${p.match_score}/10` : '';
      const steps = row.querySelector('.co-steps');
      if (p.steps && (steps.dataset.at !== String(p.steps.at) || steps.dataset.state !== p.steps.state)) {
        steps.dataset.at = p.steps.at; steps.dataset.state = p.steps.state;
        steps.style.setProperty('--at', p.steps.at);
        [...steps.children].forEach((li, i) => {
          li.className = i < p.steps.at || p.steps.state === 'done' ? 'done' : i === p.steps.at ? p.steps.state : '';
          li.querySelector('i').innerHTML = li.className === 'done' ? CHECK : li.className === 'active' ? '<span class="co-spinner"></span>' : li.className === 'stopped' ? '!' : '';
        });
      }
      const tl = row.querySelector('.co-timeline');
      const items = p.timeline || [];
      if (Number(tl.dataset.n || 0) !== items.length) {
        const before = Number(tl.dataset.n || 0);
        tl.dataset.n = items.length;
        tl.innerHTML = items.map((m, i) => `<li class="${m.tone ? 'co-tone-' + esc(m.tone) : ''}${i >= before ? ' co-new' : ''}"><time>${esc(clock(m.at))}</time><span>${esc(m.text)}</span></li>`).join('')
          || '<li><span>Nothing has happened yet.</span></li>';
      }
      const detail = row.querySelector('.co-app-detail');
      const needs = p.status === 'needs_human';
      detail.hidden = p.phase !== 'attention';
      detail.innerHTML = p.phase === 'attention' ? `${esc(p.detail)}${needs ? ' <button type="button" class="co-link co-answer">Answer in Needs you →</button>' : ''}` : '';
    }
    const total = this.sent.size;
    const summary = this.querySelector('.co-apps-summary');
    if (summary) {
      summary.textContent = [counts.applied && `${counts.applied} applied`, counts.active && `${counts.active} in progress`,
        counts.attention && `${counts.attention} need${counts.attention === 1 ? 's' : ''} you`, counts.skipped && `${counts.skipped} skipped`].filter(Boolean).join(' · ');
      for (const [k, v] of Object.entries(counts)) this.querySelector(`.co-meter [data-k="${k}"]`).style.width = `${(100 * v / total).toFixed(2)}%`;
      this.querySelector('.co-clear-done').hidden = counts.active === total;
    }
    const tab = this.querySelector('[data-pane="apps"] b');
    if (tab) tab.textContent = counts.active ? `${counts.active} active` : String(total);
  }

  // Polls while anything is still moving, and stops by itself when all of it has
  // landed, so an idle board makes no requests.
  async poll() {
    clearTimeout(this.pollTimer);
    const pks = [...this.sent.keys()];
    if (!pks.length || this.hidden) return;
    try {
      this.live = {...this.live, ...await this.request('/progress', {pks: pks.slice(-600)})};
      if (this.paneView === 'apps') this.paintApps();
      else if (this.active) {
        const job = this.jobs.find(j => j.id === this.active);
        const p = job?.pk && this.live[job.pk];
        const el = this.querySelector('.co-live');
        if (p && el && el.textContent !== p.label) { el.textContent = p.label; el.dataset.phase = p.phase; }
      }
    } catch { /* the next tick tries again */ }
    if (pks.some(pk => !FINAL.has(this.live[pk]?.phase))) this.pollTimer = setTimeout(() => this.poll(), 4000);
  }

  // One press applies: the owner asked for an easy flow. The guards that matter
  // are not this button: every role is scored first and only those clearing the
  // match bar are submitted, and the server refuses a role already applied to.
  async stage(ids, apply) {
    ids = ids.filter(Boolean);
    if (!ids.length || this.busy) return;
    this.busy = true;
    try {
      const result = await this.request(apply ? '/apply' : '/prepare', {ids});
      for (const job of this.jobs) if (ids.includes(job.id)) Object.assign(job, {state: 'pipeline', pk: result.tracked?.[job.id] || job.pk});
      for (const [id, pk] of Object.entries(result.tracked || {})) {
        const job = this.jobs.find(j => j.id === id) || {};
        this.sent.set(pk, {id, title: job.title, company: job.company, logo: job.logo, apply, at: Date.now()});
      }
      saveSent(this.network, this.sent);
      this.selected.clear();
      this.renderList(new Set());
      const dupes = result.duplicates ? ` · ${result.duplicates} already tracked` : '';
      this.status(apply ? `Applying to ${ids.length}${dupes}` : `${ids.length} sent to scoring & tailoring${dupes}`);
      this.showApps();
      this.poll();
    } catch (error) {
      this.status(error.message, true);
    } finally {
      this.busy = false;
    }
  }
}
customElements.define('cosign-board', CosignBoard);
