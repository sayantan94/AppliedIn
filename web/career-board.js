// This element stays mounted outside #pane so routine pipeline polling cannot
// close source settings, lose selections or interrupt a button press.
import { auth } from './auth.js';
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = value => {
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value || '') ? value + 'T12:00:00' : value);
  return value && Number.isFinite(+d) ? d.toLocaleDateString(undefined, {month:'short', day:'numeric'}) : 'Date unavailable';
};
const plainWhy = value => String(value || '').replace(/\s*\(\[[^\]]+\]\(https?:\/\/[^)]+\)\)/g, '').replace(/\[([^\]]+)\]\(https?:\/\/[^)]+\)/g, '$1').trim();
const labels = {new:'New', pipeline:'In pipeline', handled:'Already handled', dismissed:'Dismissed'};
class CareerBoard extends HTMLElement {
  static observedAttributes = ['company', 'query', 'hidden'];
  connectedCallback() {
    this.selected = new Set(); this.data = null; this.busy = false; this.page = 1;
    this.innerHTML = `<section class="cb-panel cb-page"><header class="cb-heading"><div><h2>Find jobs</h2><p>Discover companies that fit your interests. Send the roles you choose to your application pipeline.</p></div><button type="button" class="btn cb-pipeline">View pipeline →</button></header>
      <div class="cb-body">
        <div class="cb-interest-box"><label for="cb-interests">Interests <small>(optional)</small></label><div class="cb-toolbar"><input id="cb-interests" type="search" maxlength="600" placeholder="e.g. AI infrastructure or developer tools"><button type="button" class="btn cb-find" disabled>Search jobs</button></div><p class="cb-prefs"></p><p class="cb-note">Leave this blank to use your saved job preferences. Web searches use your Claude subscription.</p></div>
        <span class="cb-count" hidden></span><p class="cb-message" role="status" aria-live="polite"></p>
        <details class="cb-progress" hidden><summary>Search activity</summary><ol aria-label="Search progress"></ol></details>
        <details class="cb-settings"><summary>Advanced settings</summary>
        <div class="cb-toolbar"><p class="cb-context">Search company feeds, then prepare roles for your approval queue.</p><button type="button" class="btn cb-scan">Search now</button></div>

          <form><p>Search by interests to discover employers, or choose company feeds to monitor.</p><label><input name="scheduled_interests" type="checkbox"> Search the web for my interests every 6 hours (uses your Claude subscription)</label>
            <label><input name="scheduled" type="checkbox"> Search every 6 hours while AppliedIn is running and unpaused</label>
            <label><input name="auto_prepare" type="checkbox"> Automatically prepare matches using my job preferences</label>
            <p class="cb-note">Preparation uses your AI model and per-company limit, up to 50 roles per scan. Every new application waits for your approval.</p>
            <input type="search" class="cb-source-search" placeholder="Find a company source…" aria-label="Find a company source">
            <div class="cb-sources"></div><button type="submit" class="btn">Save sources & automation</button><span class="cb-saved" role="status"></span>
          </form>
        </details>
        <div class="cb-toolbar cb-filters"><label>Show <select class="cb-state"><option value="interest">Matches from last search</option><option value="new">All new jobs</option><option value="pipeline">In pipeline</option><option value="dismissed">Dismissed</option><option value="all">All results</option></select></label>
          <label>Sort <select class="cb-sort"><option value="found">Recently found</option><option value="posted">Recently posted</option><option value="company">Company</option></select></label>
          <span class="cb-result-count"></span>
        </div>
        <div class="cb-toolbar cb-actions"><label><input class="cb-all" type="checkbox"> Select visible</label><span class="cb-selection">0 selected</span><button type="button" class="btn cb-prepare" disabled>Prepare selected</button><button type="button" class="btn cb-dismiss" disabled>Dismiss</button></div>
        <div class="cb-list"></div><div class="cb-pagination"><button type="button" class="btn cb-prev">Previous</button><span></span><button type="button" class="btn cb-next">Next</button></div>
      </div></section>`;
    this.$('.cb-scan').onclick = () => this.scan();
    this.$('.cb-find').onclick = () => this.find();
    this.$('#cb-interests').onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); this.find(); } };
    this.$('.cb-pipeline').onclick = () => this.dispatchEvent(new CustomEvent('career-pipeline', {bubbles:true, detail:{company:this.preparedCompany || this.getAttribute('company') || ''}}));
    this.$('form').onsubmit = event => { event.preventDefault(); this.save(); };
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
    this.$('.cb-prepare').onclick = () => this.act('prepare');
    this.$('.cb-dismiss').onclick = () => this.act('dismiss');
    this.$('.cb-prev').onclick = () => { this.page--; this.renderRows(); };
    this.$('.cb-next').onclick = () => { this.page++; this.renderRows(); };
    this.refresh();
    this.timer = setInterval(() => { if (!this.hidden) this.refresh(); }, 6000);
  }
  disconnectedCallback() { clearInterval(this.timer); this.progressController?.abort(); }
  attributeChangedCallback(name, oldValue, newValue) {
    if (oldValue === newValue || !this.data) return;
    if (name === 'hidden') {
      if (this.hidden) this.progressController?.abort(); else this.refresh();
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
        this.$('[name=scheduled]').checked = data.settings.scheduled;
        this.$('[name=scheduled_interests]').checked = !!data.settings.scheduled_interests;
        this.$('#cb-interests').value = data.settings.interests || '';
        this.$('[name=auto_prepare]').checked = data.settings.auto_prepare;
        this.$('.cb-sources').innerHTML = data.sources.map(c => `<label class="cb-source"><input type="checkbox" name="company" value="${esc(c.name)}" ${data.settings.companies.includes(c.name) ? 'checked' : ''}>${esc(c.name)}</label>`).join('');
      }
      const changed = JSON.stringify(this.data?.jobs) !== JSON.stringify(data.jobs);
      const searchChanged = this.data?.last_search?.started_at !== data.last_search?.started_at;
      this.data = data;
      const prefs = data.preferences || {};
      this.$('.cb-prefs').textContent = `Using: ${(prefs.titles || []).join(', ') || 'your target roles'} · ${(prefs.locations || []).join(', ') || 'your preferred locations'}`;
      this.dispatchEvent(new CustomEvent('career-sources', {bubbles:true, detail:{companies: [...new Set([...data.settings.companies, ...data.jobs.map(j => j.company)])]}}));
      this.paintStatus();
      this.streamProgress();
      // Updating only when rows change keeps native dropdowns and selection stable.
      if (changed || searchChanged) this.renderRows();
    } catch (error) { this.message(error.message, true); }
    finally { this.loading = false; }
  }
  message(text, error = false) {
    this.$('.cb-message').textContent = text; this.$('.cb-message').classList.toggle('cb-error', error);
  }
  paintStatus() {
    if (!this.data) return;
    const company = this.getAttribute('company') || '';
    const supported = !company || this.data.sources.some(s => s.name === company);
    const scan = this.$('.cb-scan'); scan.disabled = this.busy || this.data.running || (!company && !!this.data.error);
    scan.textContent = this.data.running ? 'Searching…' : company ? `Search ${company}` : 'Search selected sources';
    this.$('.cb-context').textContent = company ? `More jobs at ${company}. Prepared roles join this company’s application queue.` : 'Search company feeds, then prepare roles for your approval queue.';
    this.$('.cb-find').disabled = this.busy || this.data.running;
    this.$('.cb-find').textContent = this.data.running && this.data.active_search?.kind === 'interests' ? 'Finding matching roles…' : 'Search jobs';
    const elapsed = this.data.active_search?.started_at ? Math.max(0, Math.floor((Date.now() - Date.parse(this.data.active_search.started_at)) / 1000)) : 0;
    this.$('.cb-list').setAttribute('aria-busy', String(!!this.data.running));
    const latest = [this.data.last_scan, this.data.last_search].filter(Boolean).sort((a,b) => (b.started_at || '').localeCompare(a.started_at || ''))[0];
    const receipt = this.$('.cb-state').value === 'interest' ? this.data.last_search : latest;
    let status = this.data.running ? (this.data.active_search?.kind === 'feeds' ? 'Searching public feeds…' : `Searching for matching roles and companies${elapsed ? ` · ${elapsed}s` : ''}. This can take 1–2 minutes…`) : receipt ? `${receipt.found} postings read · ${receipt.added} added to board · ${receipt.companies} source${receipt.companies === 1 ? '' : 's'} · ${date(receipt.finished_at)}` : 'Choose your sources, then run your first search. Scheduled searches repeat every 6 hours after that.';
    if (this.data.running && this.data.progress?.events?.length) status = `${this.data.progress.events.at(-1).message} · ${elapsed}s`;
    this.paintProgress();
    if (this.data.paused) status += ' Automation is paused; manual search is available.';
    if (!supported && !this.data.running) status += ` ${company} uses web search because no direct feed is connected.`;
    if (receipt?.kind === 'web' && !this.data.running) status = `${receipt.found} match${receipt.found === 1 ? '' : 'es'} · ${receipt.companies} compan${receipt.companies === 1 ? 'y' : 'ies'}. ${receipt.summary || ''}`;
    const errors = this.data.running ? [] : receipt?.errors || [];
    if (!this.data.running && receipt?.kind !== 'web' && receipt?.sources?.length) status += ' ' + receipt.sources.map(s => `${s.company}: ${s.found} postings`).join(' · ') + '.';
    if (errors.length) status += ' ' + errors.map(e => `${e.company}: ${e.error}`).join(' ');
    this.message(status, !!errors.length);
    this.$('.cb-count').textContent = `${this.rows().filter(r => r.state === 'new').length} new${company ? ` · ${company}` : ''}`;
  }
  paintProgress() {
    const latest = [this.data.last_search, this.data.last_scan].filter(Boolean).sort((a,b) => (b.started_at || '').localeCompare(a.started_at || ''))[0];
    const progress = this.data.running ? this.data.progress : latest?.progress;
    const box = this.$('.cb-progress'), events = progress?.events || [];
    box.hidden = !events.length;
    if (!events.length) return;
    if (this.progressRun !== progress.run_id) {
      this.progressRun = progress.run_id; box.open = !!this.data.running;
    }
    const list = box.querySelector('ol');
    const signature = progress.run_id + ':' + events.at(-1).seq;
    if (signature === this.progressSignature) return;
    this.progressSignature = signature;
    const follow = list.scrollHeight - list.scrollTop - list.clientHeight < 35;
    list.innerHTML = events.map(event => {
      const seconds = Math.max(0, Math.floor((Date.parse(event.at) - Date.parse(progress.run_id)) / 1000)) || 0;
      return `<li><time>${seconds}s</time><span>${esc(event.message)}</span></li>`;
    }).join('');
    if (follow) list.scrollTop = list.scrollHeight;
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
    return (this.data?.jobs || []).filter(r => (!company || r.company === company) && (!query || `${r.title} ${r.company} ${r.location}`.toLowerCase().includes(query)));
  }
  renderRows() {
    if (!this.data) return;
    const kind = this.$('.cb-state').value, sort = this.$('.cb-sort').value;
    const rows = this.rows().filter(r => kind === 'all' || (kind === 'interest' ? !!this.data.last_search && r.search_id === this.data.last_search.started_at : r.state === kind)).sort((a,b) => sort === 'company' ? a.company.localeCompare(b.company) || a.title.localeCompare(b.title) : (Date.parse(b[sort === 'posted' ? 'posted_at' : 'first_seen']) || 0) - (Date.parse(a[sort === 'posted' ? 'posted_at' : 'first_seen']) || 0));
    const eligible = new Set(rows.filter(r => r.state === 'new').map(r => r.id));
    this.selected = new Set([...this.selected].filter(id => eligible.has(id)));
    const pages = Math.max(1, Math.ceil(rows.length / 25)); this.page = Math.min(Math.max(this.page, 1), pages);
    this.visibleRows = rows.slice((this.page-1)*25, this.page*25);
    this.$('.cb-result-count').textContent = `${rows.length} role${rows.length === 1 ? '' : 's'}`;
    this.$('.cb-list').innerHTML = this.visibleRows.map(r => `<article class="cb-job"><input type="checkbox" data-pick="${esc(r.id)}" aria-label="Select ${esc(r.title)} at ${esc(r.company)}" ${r.state !== 'new' ? 'disabled' : ''} ${this.selected.has(r.id) ? 'checked' : ''}>
      <div><a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a><p>${esc(r.company)} · ${esc(r.location || 'Location not listed')}</p><small>${r.posted_at ? `Posted ${esc(date(r.posted_at))}` : 'Posting date unavailable'} · Found ${esc(date(r.first_seen))}${r.last_seen ? ` · Last seen ${esc(date(r.last_seen))}` : ''}</small>${r.why ? `<p class="cb-why">${esc(plainWhy(r.why))}</p>` : ""}${r.verification === "needs_posting_read" ? `<small>Posting will be checked before tailoring</small>` : ""}</div><span class="cb-state-label">${esc(r.state === "new" && r.verification === "verified" ? "Posting checked" : labels[r.state] || r.state)}</span></article>`).join('') || '<p class="cb-empty">Your search results will appear here. Add an interest or leave it blank, then choose Search jobs.</p>';
    this.$('.cb-pagination span').textContent = `${this.page} / ${pages}`;
    this.$('.cb-prev').disabled = this.page <= 1; this.$('.cb-next').disabled = this.page >= pages;
    this.paintSelection();
  }
  paintSelection() {
    const size = this.selected.size;
    this.$('.cb-selection').textContent = `${size} selected`;
    this.$('.cb-prepare').disabled = this.busy || !size || size > 50;
    this.$('.cb-dismiss').disabled = this.busy || !size || size > 50;
    const eligible = (this.visibleRows || []).filter(r => r.state === 'new');
    const checked = eligible.filter(r => this.selected.has(r.id)).length;
    this.$('.cb-all').checked = !!eligible.length && checked === eligible.length;
    this.$('.cb-all').indeterminate = checked > 0 && checked < eligible.length;
    this.$('.cb-all').disabled = !eligible.length || this.busy;
  }
  async save() {
    if (this.busy) return;
    this.busy = true; const button = this.$('form button'); button.disabled = true;
    try {
      const settings = await this.request('/settings', {companies: [...this.querySelectorAll('[name=company]:checked')].map(c => c.value), scheduled:this.$('[name=scheduled]').checked, auto_prepare:this.$('[name=auto_prepare]').checked, interests:this.$('#cb-interests').value.trim(), scheduled_interests:this.$('[name=scheduled_interests]').checked});
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
  async find() {
    if (this.busy || !this.data || this.data.running) return;
    this.busy = true; this.paintStatus(); this.$('.cb-find').textContent = 'Starting search…';
    this.message('Starting your search…');
    try {
      await this.request('/search', {interests:this.$('#cb-interests').value.trim(), company:''});
      this.data.running = true; this.data.active_search = {kind:'interests'}; this.data.progress = null;
      this.$('.cb-state').value = 'interest'; this.page = 1; this.selected.clear();
      this.dispatchEvent(new CustomEvent('career-show-all', {bubbles:true}));
    } catch (error) { this.message(error.message, true); }
    finally { this.busy = false; if (this.data?.running) { this.paintStatus(); this.refresh(); } else { this.$('.cb-find').disabled = false; this.$('.cb-find').textContent = 'Search jobs'; } }
  }
  async act(action) {
    if (this.busy || !this.selected.size || this.selected.size > 50) return;
    this.busy = true; this.paintSelection();
    try {
      const companies = [...new Set(this.data.jobs.filter(r => this.selected.has(r.id)).map(r => r.company))];
      this.preparedCompany = companies.length === 1 ? companies[0] : '';
      const result = await this.request('/' + action, {ids:[...this.selected]});
      this.selected.clear(); this.busy = false; await this.refresh();
      this.message(action === 'prepare' ? `${result.prepared} sent for preparation${result.duplicates ? ` · ${result.duplicates} already handled` : ''}. Use View pipeline to follow preparation and review the tailored résumés.${this.data.paused ? ' Unpause the pipeline to start preparation.' : ''}` : 'Selected jobs dismissed.');
    } catch (error) { this.message(error.message, true); }
    finally { this.busy = false; this.paintSelection(); }
  }
}
customElements.define('career-board', CareerBoard);
