// This element stays mounted outside #pane so routine pipeline polling cannot
// close source settings, lose selections or interrupt a button press.
import { auth } from './auth.js';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Posting links come from scraped pages: web URLs only, never a script scheme.
const href = value => { const s = String(value ?? '').trim(); return esc(/^https?:\/\//i.test(s) ? s : '#'); };
const date = value => {
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value || '') ? value + 'T12:00:00' : value);
  return value && Number.isFinite(+d) ? d.toLocaleDateString(undefined, {month:'short', day:'numeric'}) : 'Date unavailable';
};
const plainWhy = value => String(value || '').replace(/\s*\(\[[^\]]+\]\(https?:\/\/[^)]+\)\)/g, '').replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/g, '$1').trim();
const SOURCES = {web_search:'Found by web search', greenhouse:'Greenhouse', lever:'Lever', ashby:'Ashby', workday:'Workday', icims:'iCIMS', oraclecloud:'Oracle Cloud', cosign:'Cosign network', smartrecruiters:'SmartRecruiters'};
const labels = {new:'New', pipeline:'In pipeline', handled:'Already handled', dismissed:'Dismissed'};
class CareerBoard extends HTMLElement {
  static observedAttributes = ['company', 'query', 'hidden'];
  connectedCallback() {
    this.selected = new Set(); this.data = null; this.busy = false; this.page = 1;
    this.pendingApplications = new Map(); this.detailVersion = 0;
    this.innerHTML = `<section class="cb-panel cb-page"><header class="cb-heading"><div><p class="cb-eyebrow">Career Ops search</p><h2>Find roles across company job boards</h2><p class="cb-lede">Greenhouse, Lever, Ashby and Workday, searched from your preferences. Anything you pick is scored and tailored before it is sent.</p></div><button type="button" class="btn cb-pipeline">View pipeline →</button></header>
      <div class="cb-body">
        <aside class="cb-search-column" aria-label="Search preferences"><div class="cb-network"><h3 class="cb-search-title">Your search <span>Preferences</span></h3>
          <div class="cb-search-brief"><label for="cb-search-query">Interests <small>Optional</small></label><textarea id="cb-search-query" rows="2" maxlength="600" placeholder="AI infrastructure, developer tools, fintech…"></textarea></div>
          <div class="cb-search-options"><label>Search with <select class="cb-client"><option value="claude">Claude</option><option value="codex">Codex</option></select></label><fieldset class="cb-board-picker"><legend>Job boards</legend><div class="cb-ats">${['greenhouse','lever','ashby','workday','icims'].map(name => `<label><input type="checkbox" value="${name}" ${name !== 'icims' ? 'checked' : ''}>${({greenhouse:'Greenhouse',lever:'Lever',ashby:'Ashby',workday:'Workday',icims:'iCIMS'})[name]}</label>`).join('')}</div></fieldset></div>
          <div class="cb-search-fields"><label>Roles to find<textarea class="cb-roles" rows="3" placeholder="Software Engineer, Platform Engineer" aria-describedby="cb-filter-help"></textarea></label><label>Locations<textarea class="cb-locations" rows="3" placeholder="Seattle, California, Remote (US)"></textarea></label></div>

          <div class="cb-toolbar"><label>Posted within <select class="cb-days"><option value="1">24 hours</option><option value="3">3 days</option><option value="7">7 days</option><option value="30" selected>30 days</option><option value="90">90 days</option><option value="365">1 year</option></select></label><span class="cb-search-hint">Uses your selected client and preferences</span><button type="button" class="btn cb-network-start" disabled>Search jobs</button><button type="button" class="btn cb-network-continue" hidden>Continue scan</button><button type="button" class="btn cb-network-stop" hidden>Stop scan</button></div>
          <details class="cb-refine"><summary>More preferences</summary><p id="cb-filter-help" class="cb-note">Separate roles and locations with commas. Leave a field blank to include all.</p><label class="cb-check"><input type="checkbox" class="cb-undated" checked> Include jobs without a posting date</label><label>Exclude titles containing<input class="cb-exclude" placeholder="Intern, Sales Engineer"></label><div class="cb-toolbar"><label>Companies per source <select class="cb-depth"><option value="150">150 per batch</option><option value="500">500 per batch</option><option value="1000">1,000 per batch</option><option value="0">Entire directory</option></select></label><button type="button" class="btn cb-reset-filters">Use saved preferences</button></div><p class="cb-note">Continue scans the next batch. Entire-directory scans can take hours; completed results are saved and can be continued.</p></details>

        </div>
        <details class="cb-settings"><summary>Tracked companies & automation</summary>
        <div class="cb-toolbar"><p class="cb-context">Search company feeds, then prepare roles for your approval queue.</p><button type="button" class="btn cb-scan">Search now</button></div>

          <form><p>Search by interests to discover employers, or choose company feeds to monitor.</p><label><input name="scheduled_interests" type="checkbox"> Run an additional web search every 6 hours with my selected client</label>
            <label><input name="scheduled" type="checkbox"> Search every 6 hours while AppliedIn is running and unpaused</label>
            <label><input name="auto_prepare" type="checkbox"> Automatically prepare matches using my job preferences</label>
            <p class="cb-note">Preparation uses your AI model and per-company limit, up to 50 roles per scan. Every new application waits for your approval.</p>
            <input type="search" class="cb-source-search" placeholder="Find a company source…" aria-label="Find a company source">
            <div class="cb-sources"></div><button type="submit" class="btn">Save sources & automation</button><span class="cb-saved" role="status"></span>
          </form>
        </details>
        </aside><section class="cb-results" aria-label="Discovered jobs"><div class="cb-results-heading"><h3>Matching jobs</h3><span>Choose roles to score, tailor, and review.</span></div>
        <span class="cb-count" hidden></span><div class="cb-status"><p class="cb-message" role="status" aria-live="polite"></p>
        <div class="cb-coverage" hidden aria-live="polite"></div>
        <details class="cb-progress" hidden><summary><span class="cb-progress-heading"><strong>Search activity</strong><span class="cb-progress-state"></span></span><span class="cb-progress-latest"></span></summary><div class="cb-progress-tools"><label>Show <select class="cb-progress-filter"><option value="all">All updates</option><option value="matches">Matches found</option><option value="issues">Issues</option></select></label><span class="cb-progress-count"></span></div><ol aria-label="Search progress, newest first"></ol></details></div>
        <div class="cb-workspace"><div class="cb-list-pane">
        <div class="cb-toolbar cb-filters"><label>Show <select class="cb-state"><option value="network">Latest search</option><option value="interest">Earlier interest searches</option><option value="new">All new jobs</option><option value="pipeline">In pipeline</option><option value="dismissed">Dismissed</option><option value="all">All results</option></select></label>
          <label>Sort <select class="cb-sort"><option value="found">Recently found</option><option value="posted">Recently posted</option><option value="company">Company</option></select></label>
          <span class="cb-result-count"></span>
        </div>
        <div class="cb-toolbar cb-actions"><label><input class="cb-all" type="checkbox"> Select visible</label><span class="cb-selection">Select roles to take action</span><button type="button" class="btn cb-clear" hidden>Clear</button><div class="cb-selected-actions" hidden><button type="button" class="btn cb-apply" disabled hidden title="Score, tailor, and submit only the selected roles">Apply selected</button><button type="button" class="btn cb-prepare" disabled>Score & tailor selected</button><button type="button" class="btn cb-dismiss" disabled>Dismiss</button></div></div>
        <p class="cb-apply-help" hidden>Apply selected scores, tailors, and submits. Score & tailor stops for review.</p><div class="cb-list"></div><div class="cb-pagination"><button type="button" class="btn cb-prev">Previous</button><span></span><button type="button" class="btn cb-next">Next</button></div>
        </div><div class="cb-side" data-view="role"><div class="cb-side-tabs" role="tablist"><button type="button" role="tab" data-side="role" aria-selected="true">Role</button><button type="button" role="tab" data-side="apps" aria-selected="false" hidden>Applications <b class="cb-side-n"></b></button></div><div class="cb-detail-empty">Pick a role to read it here.</div><dialog class="cb-job-drawer" aria-labelledby="cb-detail-title"><div class="cb-drawer-head"><span>Role details</span><button type="button" class="btn cb-drawer-close" aria-label="Close role details">✕</button></div><div class="cb-drawer-content"></div><footer class="cb-drawer-foot"><label><input type="checkbox" class="cb-detail-select"> Include in selection</label><button type="button" class="btn cb-drawer-done">Back to results</button></footer></dialog><section class="cb-applications" hidden aria-label="Selected applications"><header><div><h3>Your applications</h3><p class="cb-application-summary" role="status"></p></div><span class="cb-application-refresh">Updates every 6 seconds</span></header><div class="cb-application-list"></div></section></div></div>
        </section>
      </div></section>`;
    this.mode = 'network';
    this.$('.cb-client').onchange = async () => {
      try { await this.request('/provider', {provider:this.$('.cb-client').value}); }
      catch (error) { this.message(error.message, true); }
    };
    this.$('.cb-network-start').onclick = () => this.network(false);
    this.$('.cb-network-continue').onclick = () => this.network(true);
    this.$('.cb-network-stop').onclick = async () => {
      try { await this.request('/network/stop', {}); this.message('Stopping scan; completed matches are saved.'); }
      catch (error) { this.message(error.message, true); }
    };
    this.$('.cb-reset-filters').onclick = () => { this.seedNetwork(null); this.paintStatus(); };
    this.$('.cb-network').oninput = () => this.paintStatus();
    this.$('.cb-scan').onclick = () => this.scan();
    this.$('#cb-search-query').onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.network(false); } };
    this.$('.cb-pipeline').onclick = () => this.dispatchEvent(new CustomEvent('career-pipeline', {bubbles:true, detail:{company:this.preparedCompany || this.getAttribute('company') || ''}}));
    this.$('form').onsubmit = event => { event.preventDefault(); this.save(); };
    this.$('.cb-progress-filter').onchange = () => { this.progressSignature = ''; this.paintProgress(); };
    this.$('.cb-source-search').oninput = event => {
      const q = event.target.value.toLowerCase();
      this.querySelectorAll('.cb-source').forEach(el => el.hidden = !el.textContent.toLowerCase().includes(q));
    };
    for (const name of ['.cb-state', '.cb-sort']) this.$(name).onchange = () => { this.page = 1; this.selected.clear(); this.renderRows(); this.paintStatus(); };
    this.$('.cb-all').onchange = event => {
      for (const row of this.visibleRows || []) if (row.state === 'new') {
        event.target.checked ? this.selected.add(row.id) : this.selected.delete(row.id);
      }
      this.renderRows();
    };
    this.$('.cb-list').onchange = event => {
      const id = event.target.dataset.pick;
      if (id) { event.target.checked ? this.selected.add(id) : this.selected.delete(id); this.paintSelection(); }
    };
    this.$('.cb-clear').onclick = () => { this.selected.clear(); this.renderRows(); };
    this.$('.cb-list').onclick = event => {
      if (event.target.closest('input, a')) return;
      const target = event.target.closest('[data-detail]') || event.target.closest('.cb-job[data-id]');
      if (target) this.openDetail(target.dataset.detail || target.dataset.id);
    };
    const drawer = this.$('.cb-job-drawer');
    this.$('.cb-drawer-close').onclick = this.$('.cb-drawer-done').onclick = () => drawer.close();
    drawer.onclick = event => { if (event.target === drawer && event.clientX < drawer.getBoundingClientRect().left) drawer.close(); };
    drawer.onclose = () => {
      const id = this.detailId;
      this.detailDismissed = true;  // closed by hand: do not reopen on the next poll
      this.querySelectorAll('.cb-job.is-active').forEach(el => el.classList.remove('is-active'));
      this.detailVersion++; this.detailId = null;
      const trigger = this.detailTrigger?.isConnected ? this.detailTrigger : [...this.querySelectorAll('[data-detail]')].find(el => el.dataset.detail === id);
      trigger?.focus({preventScroll:true});
    };
    this.$('.cb-detail-select').onchange = event => {
      const row = this.data.jobs.find(r => r.id === this.detailId);
      if (row?.state !== 'new' || this.busy) return;
      event.target.checked ? this.selected.add(row.id) : this.selected.delete(row.id);
      this.renderRows();
    };
    this.$('.cb-apply').onclick = () => this.act('apply');
    this.$('.cb-side-tabs').onclick = event => { const tab = event.target.closest('[data-side]'); if (tab) this.showSide(tab.dataset.side); };
    this.$('.cb-application-list').onpointerdown = () => { this.applicationPointerUntil = Date.now() + 500; };
    this.$('.cb-application-list').onclick = event => {
      const button = event.target.closest('[data-application-company]');
      if (button) this.dispatchEvent(new CustomEvent('career-pipeline', {bubbles:true, detail:{company:button.dataset.applicationCompany}}));
    };
    this.$('.cb-prepare').onclick = () => this.act('prepare');
    this.$('.cb-dismiss').onclick = () => this.act('dismiss');
    this.$('.cb-prev').onclick = () => { this.page--; this.renderRows(); };
    this.$('.cb-next').onclick = () => { this.page++; this.renderRows(); };
    this.refresh();
    this.timer = setInterval(() => { if (!this.hidden) this.refresh(); }, 6000);
  }
  // The right column shows either the role being read or your applications,
  // like the Cosign board's Role / Applications tabs.
  showSide(view) {
    const side = this.$('.cb-side');
    if (!side || side.dataset.view === view) return;
    side.dataset.view = view;
    this.querySelectorAll('.cb-side-tabs [data-side]').forEach(b => b.setAttribute('aria-selected', String(b.dataset.side === view)));
  }
  disconnectedCallback() { clearInterval(this.timer); this.progressController?.abort(); }
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue || !this.data) return;
    if (name === 'hidden') {
      if (this.hidden) { this.progressController?.abort(); this.$('.cb-job-drawer').close(); } else this.refresh();
      return;
    }
    this.selected.clear(); this.page = 1; this.renderRows(); this.paintStatus();
  }
  $(selector) { return this.querySelector(selector); }
  async request(path = '', body) {
    const cfg = window.APPLIEDIN_CONFIG || {};
    if (cfg.demo === true || new URLSearchParams(location.search).has('demo')) throw new Error('Career Ops is available when connected to your local AppliedIn server.');
    const response = await fetch((cfg.apiUrl || '').replace(/\/$/, '') + '/career-ops' + path, {
      method: body === undefined ? 'GET' : 'POST', headers: {'Content-Type':'application/json', ...auth.header()},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(response.status === 404 ? 'The search service is updating. Refresh this page and try again.' : typeof result.detail === 'string' ? result.detail : 'Could not complete this action. Try again.');
    return result;
  }
  async refresh() {
    if (this.loading || this.busy) return;
    this.loading = true;
    try {
      const data = await this.request();
      if (!this.data) {
        this.seedNetwork(data.network_filters, data.preferences);
        if (!data.last_network && data.jobs.length) this.$('.cb-state').value = 'new';
        this.$('[name=scheduled]').checked = data.settings.scheduled;
        this.$('[name=scheduled_interests]').checked = !!data.settings.scheduled_interests;
        this.$('.cb-client').value = data.network_filters?.provider || data.settings.search_provider || 'claude';
        this.$('[name=auto_prepare]').checked = data.settings.auto_prepare;
        this.$('.cb-sources').innerHTML = data.sources.map(c => `<label class="cb-source"><input type="checkbox" name="company" value="${esc(c.name)}" ${data.settings.companies.includes(c.name) ? 'checked' : ''}>${esc(c.name)}</label>`).join('');
      }
      const changed = JSON.stringify(this.data?.jobs) !== JSON.stringify(data.jobs);
      const searchChanged = this.data?.last_search?.started_at !== data.last_search?.started_at || this.data?.last_network?.started_at !== data.last_network?.started_at;
      this.data = data;
      this.applicationRefreshFailed = false;
      for (const row of data.jobs) if (row.application) this.pendingApplications.delete(row.id);
      this.paintApplications();
      this.dispatchEvent(new CustomEvent('career-sources', {bubbles:true, detail:{companies: [...new Set([...data.settings.companies, ...data.jobs.map(j => j.company)])]}}));
      this.paintStatus();
      this.streamProgress();
      // Updating only when rows change keeps native dropdowns and selection stable.
      if (changed || searchChanged) this.renderRows();
    } catch (error) { this.applicationRefreshFailed = true; this.paintApplications(); this.message(error.message, true); }
    finally { this.loading = false; }
  }
  paintApplications() {
    const jobs = new Map((this.data?.jobs || []).filter(r => r.application).map(r => [r.id, r]));
    for (const [id, row] of this.pendingApplications) if (!jobs.has(id)) jobs.set(id, row);
    const active = p => ['starting', 'queued', 'score', 'tailor', 'waiting', 'apply'].includes(p);
    const rows = [...jobs.values()].sort((a,b) => Number(active(b.application.phase)) - Number(active(a.application.phase)) || (b.application.requested_at || '').localeCompare(a.application.requested_at || ''));
    const panel = this.$('.cb-applications'); panel.hidden = !rows.length;
    const tab = this.$('.cb-side-tabs [data-side="apps"]');
    tab.hidden = !rows.length;
    if (!rows.length) { this.showSide('role'); return; }
    const counts = rows.reduce((n,r) => { const a = r.application; n[active(a.phase) ? 'active' : a.phase === 'applied' ? 'applied' : a.status === 'skipped' ? 'skipped' : a.status === 'job_gone' ? 'closed' : 'attention']++; return n; }, {active:0, applied:0, attention:0, skipped:0, closed:0});
    const running = rows.filter(r => active(r.application.phase)).length;
    this.$('.cb-side-n').textContent = running ? `${running} active` : String(rows.length);
    this.$('.cb-application-summary').textContent = [counts.active && `${counts.active} in progress`, counts.applied && `${counts.applied} applied`, counts.attention && `${counts.attention} need attention`, counts.skipped && `${counts.skipped} skipped`, counts.closed && `${counts.closed} closed`].filter(Boolean).join(' · ');
    this.$('.cb-application-refresh').textContent = this.applicationRefreshFailed ? 'Connection interrupted · retrying…' : 'Updates every 6 seconds';
    // A poll must not replace a button between pointerdown and click, or steal
    // keyboard focus while the user is opening a role's pipeline.
    if (Date.now() < (this.applicationPointerUntil || 0) || this.$('.cb-application-list').contains(document.activeElement)) return;
    const list = this.$('.cb-application-list');
    const expanded = new Map([...list.querySelectorAll('[data-application-group]')].map(el => [el.dataset.applicationGroup, el.open]));
    const groups = this.applicationGroups(rows);
    list.innerHTML = '<div class="cb-application-columns" aria-hidden="true"><span>Role & company</span><span>Status</span><span>Match / applied</span><span></span></div>' + groups.map(({key, label, items}) => `<details class="cb-application-group" data-application-group="${key}" ${(expanded.get(key) ?? !['applied','skipped','closed'].includes(key)) ? 'open' : ''}><summary>${label}<span>${items.length}</span></summary>${items.slice(0,50).map(r => {
      const a = r.application;
      const stamp = a.applied_at ? new Date(a.applied_at).toLocaleDateString(undefined, {month:'short', day:'numeric'}) : '';
      const time = a.phase === 'applied' ? (stamp ? `Applied ${stamp}` : 'Date unavailable') : a.match_score != null ? `${a.match_score}/10 match` : '';
      return `<article class="cb-application-row ${active(a.phase) ? 'is-active' : a.phase === 'applied' ? 'is-applied' : 'needs-attention'}"><button type="button" class="cb-application-role" data-application-company="${esc(r.company)}" title="${esc(r.title)} at ${esc(r.company)} — open in pipeline">${esc(r.title)} <span>· ${esc(r.company)}</span></button><span class="cb-application-state" data-phase="${esc(a.phase)}" title="${esc(a.detail || a.label)}">${active(a.phase) ? '<i class="cb-live-dot" aria-hidden="true"></i>' : ''}${esc(a.label)}</span><span class="cb-application-time">${esc(time)}</span><button type="button" class="btn cb-application-open" data-application-company="${esc(r.company)}" aria-label="View ${esc(r.title)} at ${esc(r.company)} in pipeline">View →</button></article>`;
    }).join('')}${items.length > 50 ? '<p class="cb-note">Showing the latest 50. Open the pipeline for all roles.</p>' : ''}</details>`).join('');
  }
  applicationGroups(rows) {
    const definitions = [['apply','Applying now'], ['score','Scoring'], ['tailor','Tailoring'], ['waiting','Waiting to apply'], ['queued','Queued'], ['attention','Needs attention'], ['skipped','Skipped'], ['closed','Posting closed'], ['applied','Applied']];
    const key = a => a.phase === 'starting' ? 'queued' : a.phase === 'attention' && a.status === 'skipped' ? 'skipped' : a.status === 'job_gone' ? 'closed' : definitions.some(([k]) => k === a.phase) ? a.phase : 'attention';
    return definitions.map(([group, label]) => ({key:group, label, items:rows.filter(r => key(r.application) === group)})).filter(g => g.items.length);
  }

  message(text, error = false) {
    this.$('.cb-message').hidden = !text; this.$('.cb-message').textContent = text; this.$('.cb-message').classList.toggle('cb-error', error);
  }
  paintStatus() {
    if (!this.data) return;
    const company = this.getAttribute('company') || '';
    const supported = !company || this.data.sources.some(s => s.name === company);
    const scan = this.$('.cb-scan'); scan.disabled = this.busy || this.data.running || (!company && !!this.data.error);
    scan.textContent = this.data.running ? 'Searching…' : company ? `Search ${company}` : 'Search selected sources';
    this.$('.cb-context').textContent = company ? `More jobs at ${company}. Prepared roles join this company’s application queue.` : 'Search company feeds, then prepare roles for your approval queue.';
    const elapsed = this.data.active_search?.started_at ? Math.max(0, Math.floor((Date.now() - Date.parse(this.data.active_search.started_at)) / 1000)) : 0;
    this.$('.cb-list').setAttribute('aria-busy', String(!!this.data.running));
    const latest = [this.data.last_scan, this.data.last_search, this.data.last_network].filter(Boolean).sort((a,b) => (b.started_at || '').localeCompare(a.started_at || ''))[0];
    const receipt = this.$('.cb-state').value === 'network' ? this.data.last_network : this.$('.cb-state').value === 'interest' ? this.data.last_search : latest;
    this.paintNetwork();
    let status = this.data.running ? (this.data.active_search?.kind === 'feeds' ? 'Searching public feeds…' : `Searching for matching roles and companies${elapsed ? ` · ${elapsed}s` : ''}. This can take 1–2 minutes…`) : receipt ? `${receipt.found} postings read · ${receipt.added} added to board · ${receipt.companies} source${receipt.companies === 1 ? '' : 's'} · ${date(receipt.finished_at)}` : 'Choose Search jobs to scan public company boards.';
    if (this.data.running && this.data.progress?.events?.length) status = `${this.data.progress.events.at(-1).message} · ${elapsed}s`;
    this.paintProgress();
    if (!this.data.running && !receipt) status = '';
    if (!supported && !this.data.running) status += ` ${company} uses web search because no direct feed is connected.`;
    if (receipt?.kind === 'web' && !this.data.running) status = `${receipt.found} match${receipt.found === 1 ? '' : 'es'} · ${receipt.companies} compan${receipt.companies === 1 ? 'y' : 'ies'}. ${receipt.summary || ''}`;
    const errors = this.data.running ? [] : receipt?.errors || [];
    if (!this.data.running && receipt?.kind !== 'web' && receipt?.kind !== 'network' && receipt?.sources?.length) status += ' ' + receipt.sources.slice(0, 4).map(s => `${s.company}: ${s.found} postings`).join(' · ') + '.';
    if (!this.data.running && receipt?.kind === 'network') status = `${receipt.found} matches saved this batch · ${receipt.read} postings read · ${receipt.unreachable} boards unreachable. ${receipt.complete ? 'Reached the end of the selected directories.' : 'Partial scan — continue to check more companies.'}`;
    if (!this.data.running && receipt?.kind === 'network') {
      const failure = receipt.scan_error || errors.find(e => e.company === 'Scanner')?.error;
      if (failure) status = `${receipt.found || 0} matches saved. Scan failed: ${failure}`;
      const clientFailure = errors.find(e => ['claude', 'codex'].includes(e.company));
      if (clientFailure) status += ` ${clientFailure.company === 'claude' ? 'Claude' : 'Codex'} search: ${clientFailure.error}`;
    }
    if (!this.data.running && this.data.error) status = this.data.error + (status ? ` ${status}` : '');
    if (errors.length && receipt?.kind !== 'network') status += ' ' + errors.slice(0, 3).map(e => `${e.company}: ${e.error}`).join(' ');
    this.message(status, !!this.data.error || !!receipt?.scan_error || errors.some(e => e.company === 'Scanner') || (!!errors.length && (receipt?.kind !== 'network' || !receipt.found)));
    this.$('.cb-count').textContent = `${this.rows().filter(r => r.state === 'new').length} new${company ? ` · ${company}` : ''}`;
  }
  progressRows(events, filter = 'all') {
    const groups = [];
    for (const event of events) {
      const message = String(event.message || '');
      const issue = /HTTP [45]\d\d|failed|timed? out|unreachable|could not|cannot|error/i.test(message);
      const match = /\b[1-9]\d* matches?\b/i.test(message);
      if (filter === 'issues' && !issue || filter === 'matches' && !match) continue;
      const previous = groups.at(-1);
      if (previous?.message === message) { previous.count++; previous.at = event.at; }
      else groups.push({...event, message, issue, match, count:1});
    }
    return groups.reverse();
  }
  paintProgress() {
    const latest = [this.data.last_search, this.data.last_scan, this.data.last_network].filter(Boolean).sort((a,b) => (b.started_at || '').localeCompare(a.started_at || ''))[0];
    const shown = this.$('.cb-state').value === 'network' ? this.data.last_network : this.$('.cb-state').value === 'interest' ? this.data.last_search : latest;
    const progress = this.data.running ? this.data.progress : shown?.progress;
    const box = this.$('.cb-progress'), events = progress?.events || [];
    box.hidden = !events.length;
    if (!events.length) return;
    if (this.progressRun !== progress.run_id) {
      this.progressRun = progress.run_id; this.progressSignature = ''; box.open = false;
      this.$('.cb-progress-filter').value = 'all';
    }
    const state = this.$('.cb-progress-state');
    state.textContent = this.data.running ? 'Live' : shown?.scan_error ? 'Stopped with an error' : shown?.complete === false ? 'Partial scan' : 'Finished';
    state.classList.toggle('is-live', !!this.data.running);
    this.$('.cb-progress-latest').textContent = events.at(-1).message;
    this.$('.cb-progress-latest').title = events.at(-1).message;
    const filter = this.$('.cb-progress-filter').value;
    const signature = progress.run_id + ':' + events.at(-1).seq + ':' + filter;
    if (signature === this.progressSignature) return;
    const sameView = this.progressSignature?.split(':').at(-1) === filter;
    this.progressSignature = signature;
    const list = box.querySelector('ol'), previousHeight = list.scrollHeight, previousTop = list.scrollTop;
    const rows = this.progressRows(events, filter);
    this.$('.cb-progress-count').textContent = `${rows.length} update${rows.length === 1 ? '' : 's'} · newest first${events[0].seq > 1 ? ' · recent history' : ''}`;
    list.innerHTML = rows.map(event => {
      const seconds = Math.max(0, Math.floor((Date.parse(event.at) - Date.parse(progress.run_id)) / 1000)) || 0;
      const elapsed = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
      return `<li class="${event.issue ? 'cb-progress-issue' : event.match ? 'cb-progress-match' : ''}"><time title="Time since this search started">${elapsed}</time><span>${esc(event.message)}${event.count > 1 ? `<small class="cb-progress-repeat">${event.count} repeated updates</small>` : ''}</span></li>`;
    }).join('') || `<li class="cb-progress-empty">${filter === 'issues' ? 'No issues in recent activity.' : 'No matches reported in recent activity yet.'}</li>`;
    // Keep the newest update visible, but do not pull readers away from older entries.
    list.scrollTop = sameView && previousTop > 12 ? previousTop + list.scrollHeight - previousHeight : 0;
  }
  async streamProgress() {
    if (!this.data?.running || this.hidden || this.progressController) return;
    const controller = new AbortController(); this.progressController = controller;
    let reader;
    try {
      const cfg = window.APPLIEDIN_CONFIG || {};
      const response = await fetch((cfg.apiUrl || '').replace(/\/$/, '') + '/career-ops/progress', {
        headers: auth.header(), signal: controller.signal
      });
      if (!response.ok || !response.headers.get('content-type')?.includes('text/event-stream')) return;
      reader = response.body.getReader();
      const decoder = new TextDecoder(); let buffer = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, {stream:true});
        let boundary;
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          const packet = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
          if (!packet.startsWith('data: ')) continue;
          const progress = JSON.parse(packet.slice(6));
          this.data.progress = progress; this.data.active_search = progress.active_search;
          if (!progress.running) { await this.refresh(); return; }
          this.paintStatus();
        }
      }
    } catch {
      // Regular polling recovers lost streams without replacing the user's form.
    } finally {
      if (reader) await reader.cancel().catch(() => {});
      this.progressController = null;
    }
  }
  rows() {
    const company = this.getAttribute('company') || '', query = (this.getAttribute('query') || '').toLowerCase().trim();
    return (this.data?.jobs || []).filter(r => !['applied', 'applied_manual'].includes(r.pipeline_status || r.application?.status) && r.application?.phase !== 'applied' && (!company || r.company === company) && (!query || `${r.title} ${r.company} ${r.location}`.toLowerCase().includes(query)));
  }
  renderRows() {
    if (!this.data) return;
    const kind = this.$('.cb-state').value, sort = this.$('.cb-sort').value;
    const rows = this.rows().filter(r => kind === 'all' || (kind === 'network' ? !!this.data.last_network && r.search_id === this.data.last_network.search_id : kind === 'interest' ? !!this.data.last_search && r.search_id === this.data.last_search.started_at : r.state === kind)).sort((a,b) => sort === 'company' ? a.company.localeCompare(b.company) || a.title.localeCompare(b.title) : (Date.parse(b[sort === 'posted' ? 'posted_at' : 'first_seen']) || 0) - (Date.parse(a[sort === 'posted' ? 'posted_at' : 'first_seen']) || 0));
    const eligible = new Set(rows.filter(r => r.state === 'new').map(r => r.id));
    this.selected = new Set([...this.selected].filter(id => eligible.has(id)));
    const pages = Math.max(1, Math.ceil(rows.length / 25)); this.page = Math.min(Math.max(this.page, 1), pages);
    this.visibleRows = rows.slice((this.page-1)*25, this.page*25);
    this.$('.cb-result-count').textContent = `${rows.length} role${rows.length === 1 ? '' : 's'}`;
    this.$('.cb-list').innerHTML = this.visibleRows.map(r => `<article class="cb-job cb-job-compact${r.id === this.detailId ? ' is-active' : ''}" data-id="${esc(r.id)}"><input type="checkbox" data-pick="${esc(r.id)}" aria-label="Select ${esc(r.title)} at ${esc(r.company)}" ${r.state !== 'new' ? 'disabled' : ''} ${this.selected.has(r.id) ? 'checked' : ''}>
      <span class="cb-company-mark" aria-hidden="true">${esc(r.company.slice(0,2).toUpperCase())}</span><div class="cb-job-info"><button type="button" class="cb-job-title" data-detail="${esc(r.id)}" aria-haspopup="dialog">${esc(r.title)}</button><p>${esc(r.company)}${r.location ? ` <span>· ${esc(r.location)}</span>` : ''} <span>· ${r.posted_at ? `Posted ${esc(date(r.posted_at))}` : `Found ${esc(date(r.first_seen))}`}</span></p></div><div class="cb-job-meta"><span class="cb-src">${esc(SOURCES[r.provider] || r.provider || 'Career page')}</span>${r.application?.match_score != null ? `<span class="cb-score">${esc(r.application.match_score)}/10 match</span>` : ''}<span class="cb-state-label">${esc(r.application?.label || (r.state === 'new' && r.verification === 'verified' ? 'Posting checked' : labels[r.state] || r.state))}</span><button type="button" class="cb-details-link" data-detail="${esc(r.id)}" aria-label="Details for ${esc(r.title)} at ${esc(r.company)}">Details →</button></div></article>`).join('') || '<div class="cb-empty"><span class="cb-empty-icon" aria-hidden="true">⌕</span><strong>No roles in this view</strong><p>Choose All new jobs to browse earlier discoveries, or adjust your search.</p></div>';
    this.$('.cb-actions').hidden = !rows.length;
    this.$('.cb-pagination').hidden = pages <= 1;
    this.$('.cb-pagination span').textContent = `${this.page} / ${pages}`;
    this.$('.cb-prev').disabled = this.page <= 1; this.$('.cb-next').disabled = this.page >= pages;
    this.paintSelection();
    // Like the Cosign board, a wide screen always has a role open to read.
    if (this.wide() && !this.detailId && !this.detailDismissed && this.visibleRows.length) this.openDetail(this.visibleRows[0].id, {quiet: true});
  }
  wide() { return typeof matchMedia === 'function' && matchMedia('(min-width: 1000px)').matches; }
  paintSelection() {
    const size = this.selected.size;
    this.$('.cb-selection').textContent = size ? `${size} selected${size > 50 ? ' · select up to 50 at a time' : ''}` : 'Select roles to take action';
    this.$('.cb-selected-actions').hidden = !size;
    this.$('.cb-clear').hidden = !size;
    this.$('.cb-clear').disabled = this.busy;
    this.$('.cb-actions').classList.toggle('has-selection', !!size);
    const detailRow = this.data?.jobs.find(r => r.id === this.detailId);
    this.$('.cb-detail-select').checked = !!detailRow && this.selected.has(detailRow.id);
    this.$('.cb-detail-select').disabled = this.busy || detailRow?.state !== 'new';
    this.$('.cb-apply').hidden = !this.data?.capabilities?.apply_selected;
    this.$('.cb-apply').disabled = this.busy || !size || size > 50;
    this.$('.cb-apply-help').hidden = !size || !this.data?.capabilities?.apply_selected;
    this.$('.cb-prepare').disabled = this.busy || !size || size > 50;
    this.$('.cb-dismiss').disabled = this.busy || !size || size > 50;
    const eligible = (this.visibleRows || []).filter(r => r.state === 'new');
    const checked = eligible.filter(r => this.selected.has(r.id)).length;
    this.$('.cb-all').checked = !!eligible.length && checked === eligible.length;
    this.$('.cb-all').indeterminate = checked > 0 && checked < eligible.length;
    this.$('.cb-all').disabled = !eligible.length || this.busy;
  }
  async openDetail(id, {quiet = false} = {}) {
    const row = this.data?.jobs.find(r => r.id === id);
    if (!row) return;
    this.detailId = id; this.detailDismissed = false;
    this.showSide('role');
    if (!quiet) this.detailTrigger = document.activeElement;
    this.querySelectorAll('.cb-job[data-id]').forEach(el => el.classList.toggle('is-active', el.dataset.id === id));
    const version = ++this.detailVersion;
    const drawer = this.$('.cb-job-drawer'), body = this.$('.cb-drawer-content');
    body.innerHTML = `<div class="cb-detail-company"><span class="cb-company-mark" aria-hidden="true">${esc(row.company.slice(0,2).toUpperCase())}</span><span>${esc(row.company)}</span></div><h2 id="cb-detail-title">${esc(row.title)}</h2><p class="cb-detail-location">${esc(row.location || 'Location not listed')}</p><div class="cb-detail-tags"><span>${esc(labels[row.state] || row.state)}</span>${row.posted_at ? `<span>Posted ${esc(date(row.posted_at))}</span>` : ''}<span>${esc(SOURCES[row.provider] || row.provider || 'Career page')}</span></div><a class="cb-original-link" href="${href(row.url)}" target="_blank" rel="noopener noreferrer">Open original posting ↗</a>${row.why ? `<section class="cb-detail-section"><h3>Why it was found</h3><p>${esc(plainWhy(row.why))}</p></section>` : ''}<section class="cb-detail-section"><h3>Application & résumé</h3><div class="cb-detail-application">Loading saved information…</div></section><section class="cb-detail-section"><h3>Job description</h3><p class="cb-detail-description" aria-live="polite">Loading saved description…</p></section>`;
    this.paintSelection();
    const inline = this.wide();
    // show() moves focus into the panel; beside the list that would pull the
    // keyboard (and the page) away from the row the owner was on.
    const before = document.activeElement;
    if (!drawer.open) inline ? drawer.show() : drawer.showModal();
    if (inline && before instanceof HTMLElement && before.isConnected) before.focus({preventScroll: true});
    else if (inline) document.activeElement?.blur?.();
    this.$('.cb-drawer-content').scrollTop = 0;
    // An overlay takes focus; a pane beside the list must not steal it.
    if (!inline) this.$('.cb-drawer-close').focus();
    try {
      const detail = await this.request('/job/' + encodeURIComponent(id));
      if (version !== this.detailVersion || !drawer.open) return;
      this.$('.cb-detail-description').textContent = detail.description || 'A full description has not been saved yet. Open the original posting to read it; AppliedIn will check it before tailoring.';
      const cfg = window.APPLIEDIN_CONFIG || {}, resume = detail.resume_url ? (cfg.apiUrl || '').replace(/\/$/, '') + detail.resume_url : '';
      this.$('.cb-detail-application').innerHTML = this.receiptHtml(row, detail, resume);
    } catch (error) {
      if (version !== this.detailVersion || !drawer.open) return;
      this.$('.cb-detail-description').textContent = 'Saved details could not be loaded. You can still open the original posting.';
      this.$('.cb-detail-application').textContent = error.message;
    }
  }
  receiptHtml(row, detail, resume) {
    const status = detail.status || row.pipeline_status || row.application?.status || '';
    const applied = ['applied', 'applied_manual'].includes(status);
    const labels = {found:'Waiting for preparation', tailoring:'Preparing résumé', tailored:row.application?.requested_at ? 'Waiting to apply' : 'Ready for review', submitting:'Applying in browser', applied:'Application confirmed', applied_manual:'Marked applied manually', needs_human:'Needs your attention', failed:'Could not finish', error:'Could not finish', skipped:'Skipped', job_gone:'Posting closed', uncertain:'Check the application outcome'};
    const label = labels[status] || (status ? 'Status unavailable' : 'Not started');
    const tone = applied ? 'complete' : ['needs_human','failed','error','uncertain'].includes(status) ? 'attention' : 'pending';
    const score = detail.match_score ?? row.application?.match_score;
    const stamp = detail.applied_at || row.application?.applied_at;
    const requested = row.application?.requested_at;
    return `<div class="cb-receipt" data-outcome="${tone}"><header><span>Application status</span><strong>${esc(label)}</strong></header><dl><div><dt>Match score</dt><dd>${score != null ? `${esc(score)} / 10` : 'Not scored yet'}</dd></div><div><dt>Tailored résumé</dt><dd>${resume ? `<a href="${esc(resume)}" target="_blank" rel="noopener noreferrer">Open résumé ↗</a>` : 'Not available yet'}</dd></div><div><dt>Submission</dt><dd>${applied ? status === 'applied_manual' ? 'Recorded by you' : 'Confirmed' : 'Not confirmed'}</dd></div>${applied ? `<div><dt>Applied on</dt><dd>${stamp ? esc(date(stamp)) : 'No date recorded'}</dd></div>` : requested ? `<div><dt>Requested on</dt><dd>${esc(date(requested))}</dd></div>` : ''}</dl><p>${applied ? 'Saved in your application history.' : status && status !== 'found' ? 'Open the pipeline to review this role and its next step. No confirmed submission is recorded.' : 'Prepare a résumé for review, or use Apply selected to score, tailor and submit. Submission still depends on the score and required answers.'}</p></div>`;
  }
  seedNetwork(filters, prefs = this.data?.preferences || {}) {
    filters ||= {positive:prefs.titles || [], negative:prefs.exclude_keywords || [], locations:prefs.locations || [], ats:['greenhouse','lever','ashby','workday'], days:30, include_undated:true, limit:150};
    this.$('.cb-client').value = filters.provider || this.data?.settings.search_provider || 'claude';
    this.$('#cb-search-query').value = filters.interests || '';
    this.$('.cb-roles').value = filters.positive.join(', ');
    this.$('.cb-exclude').value = filters.negative.join(', ');
    this.$('.cb-locations').value = filters.locations.join(', ');
    this.$('.cb-days').value = String(filters.days);
    this.$('.cb-depth').value = String(filters.limit);
    this.$('.cb-undated').checked = filters.include_undated;
    this.querySelectorAll('.cb-ats input').forEach(el => el.checked = filters.ats.includes(el.value));
  }
  networkFilters() {
    const list = selector => this.$(selector).value.split(/[,;\n]+/).map(s => s.trim()).filter(Boolean);
    return {provider:this.$('.cb-client').value, interests:this.$('#cb-search-query').value.trim(), positive:list('.cb-roles'), negative:list('.cb-exclude'), locations:list('.cb-locations'),
      ats:[...this.querySelectorAll('.cb-ats input:checked')].map(el => el.value), days:Number(this.$('.cb-days').value),
      include_undated:this.$('.cb-undated').checked, limit:Number(this.$('.cb-depth').value)};
  }
  paintNetwork() {
    const running = this.data.running && this.data.active_search?.kind === 'network';
    const last = this.data.last_network, filters = this.networkFilters();
    this.$('.cb-network-start').disabled = this.busy || this.data.running || !filters.ats.length;
    this.$('.cb-network-start').textContent = running ? 'Scanning job boards…' : 'Search jobs';
    const more = this.$('.cb-network-continue');
    more.hidden = !last || last.complete || this.data.running;
    more.disabled = this.busy || !this.data.network_filters || Object.keys(filters).some(k => JSON.stringify(filters[k]) !== JSON.stringify(this.data.network_filters[k]));
    more.title = more.disabled ? 'Restore the previous filters, or start a new scan with these filters.' : 'Scan the next companies using the same filters';
    this.$('.cb-network-stop').hidden = !running;
    const coverage = running ? this.data.active_search.coverage : last?.coverage;
    const box = this.$('.cb-coverage');
    box.hidden = this.mode !== 'network' || !coverage || !Object.keys(coverage).length;
    if (box.hidden) return;
    const items = Object.entries(coverage), checked = items.reduce((n,[,c]) => n + c.next, 0), total = items.reduce((n,[,c]) => n + c.total, 0);
    box.innerHTML = `<div><strong>${checked.toLocaleString()} / ${total.toLocaleString()} company boards checked</strong><span>${running ? `${this.data.active_search.found || 0} matches this batch` : `${last?.undated || 0} matches excluded for missing dates`}</span></div><progress max="${total || 1}" value="${checked}" aria-label="Company directory coverage"></progress><p>${items.map(([name,c]) => `${esc(name)} ${c.next.toLocaleString()}/${c.total.toLocaleString()}${c.status !== 'ok' ? ` (${esc(c.status)})` : ''}`).join(' · ')}</p><details class="cb-coverage-details"><summary>About coverage</summary><p class="cb-note">Counts include unreachable boards. Provider page limits can also make results partial${last?.capped ? ` · ${last.capped} boards reported a page limit` : ''}.</p></details>`;
  }
  async network(resume) {
    if (this.busy || !this.data || this.data.running) return;
    this.busy = true; this.paintStatus(); this.message('Starting network scan…');
    try {
      const filters = this.networkFilters();
      const result = await this.request('/network', {...filters, resume});
      this.data.running = true; this.data.active_search = {kind:result.kind || 'network'};
      this.data.progress = null; this.$('.cb-state').value = 'network';
      this.page = 1; this.selected.clear();
      this.dispatchEvent(new CustomEvent('career-show-all', {bubbles:true}));
    } catch (error) { this.message(error.message, true); }
    finally { this.busy = false; if (this.data.running) await this.refresh(); else this.paintNetwork(); }
  }
  async save() {
    if (this.busy) return;
    this.busy = true; const button = this.$('form button'); button.disabled = true;
    try {
      const settings = await this.request('/settings', {companies: [...this.querySelectorAll('[name=company]:checked')].map(c => c.value), scheduled:this.$('[name=scheduled]').checked, auto_prepare:this.$('[name=auto_prepare]').checked, interests:this.$('#cb-search-query').value.trim(), scheduled_interests:this.$('[name=scheduled_interests]').checked, search_provider:this.$('.cb-client').value});
      this.data.settings = settings; this.$('.cb-saved').textContent = 'Saved';
    } catch (error) { this.$('.cb-saved').textContent = error.message; }
    finally { this.busy = false; button.disabled = false; }
  }
  async scan() {
    if (this.busy || !this.data || this.data.running) return;
    this.busy = true; this.paintStatus(); this.$('.cb-scan').textContent = 'Starting…';
    try {
      const result = await this.request('/scan', {company:this.getAttribute('company') || ''});
      this.data.running = true; this.data.active_search = {kind:result.kind === 'web' ? 'company_web' : 'feeds'}; this.data.progress = null;
      this.$('.cb-state').value = result.kind === 'web' ? 'interest' : 'new';
    } catch (error) { this.message(error.message, true); }
    finally { this.busy = false; if (this.data?.running) { this.paintStatus(); this.refresh(); } else { this.$('.cb-scan').disabled = false; this.$('.cb-scan').textContent = this.getAttribute('company') ? `Search ${this.getAttribute('company')}` : 'Search selected sources'; } }
  }
  async act(action) {
    if (this.busy || !this.selected.size || this.selected.size > 50) return;
    this.busy = true; this.paintSelection();
    const ids = [...this.selected];
    if (action === 'apply') {
      const requested_at = new Date().toISOString();
      for (const r of this.data.jobs.filter(r => this.selected.has(r.id))) this.pendingApplications.set(r.id, {...r, application:{phase:'starting', label:'Sending request…', requested_at}});
      this.paintApplications(); this.showSide('apps');
      this.paintApplications();
      this.$('.cb-apply').textContent = 'Starting…';
      this.$('.cb-applications').scrollIntoView({block:'nearest', behavior:'smooth'});
    }
    try {
      const companies = [...new Set(this.data.jobs.filter(r => this.selected.has(r.id)).map(r => r.company))];
      this.preparedCompany = companies.length === 1 ? companies[0] : '';
      const result = await this.request('/' + action, {ids:[...this.selected]});
      if (action === 'apply') for (const id of ids) {
        const row = this.pendingApplications.get(id);
        if (row) row.application = {...row.application, phase:'queued', label:'Request accepted', detail:'Waiting for the pipeline to report progress.'};
      }
      this.selected.clear(); this.busy = false; await this.refresh();
      if (action === 'apply' && !this.applicationRefreshFailed) for (const id of ids) {
        const pending = this.pendingApplications.get(id), tracked = this.data.jobs.find(r => r.id === id);
        if (pending && tracked?.pk && result.pks && !result.pks.includes(tracked.pk)) pending.application = {...pending.application, phase:'unknown', label:'Already in pipeline', detail:'This role was already handled. Open its pipeline to see the current status.'};
      }
      this.message(action === 'apply' ? `${result.started} selected roles started: scoring → tailoring → application. Follow them in View pipeline. Low scores or missing answers will stop for attention.` : action === 'prepare' ? `${result.prepared} sent for preparation${result.duplicates ? ` · ${result.duplicates} already handled` : ''}. Use View pipeline to follow preparation and review the tailored résumés.${this.data.paused ? ' Unpause the pipeline to start preparation.' : ''}` : 'Selected jobs dismissed.');
    } catch (error) {
      if (action === 'apply') for (const id of ids) {
        const row = this.pendingApplications.get(id);
        if (row) row.application = {...row.application, phase:'unknown', label:'Request not confirmed', detail:'Check the pipeline before retrying. ' + error.message};
      }
      this.message(error.message, true);
    }
    finally { this.busy = false; this.$('.cb-apply').textContent = 'Apply selected'; this.paintApplications(); this.paintSelection(); }
  }
}
customElements.define('career-board', CareerBoard);
