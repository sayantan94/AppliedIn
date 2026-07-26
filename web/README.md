# AppliedIn — Control (web dashboard)

Static single-page dashboard for the AppliedIn pipeline.

## Views

- **Pipeline** — kanban lanes: Discovered → Tailoring & applying → Waiting for
  you → Applied → Closed, with a KPI strip (today's cap, needs-you, pipeline).
- **Applications** — filterable/searchable table of every posting.
- **Needs you** — the review queue (gated items with reason + one-tap actions).
- **Companies** — watchlist with per-company burn-in progress and mode.
- **Answer bank** — global facts + per-company answers.
- **Detail drawer** (click any card/row) — resume version and **all metadata
  used to apply**: every form field, checkbox, and answer (with auto/gated
  confidence), JD snapshot, timeline, confirmation, and artifact links.

## Local preview

```bash
cd web
python3 -m http.server 8791
# http://localhost:8791/            the landing page
# http://localhost:8791/dashboard.html   the dashboard, on sample data
```

Demo mode is the committed default (`config.js` → `demo: true`), and the login
gate is currently disabled (`LOGIN_DISABLED` in `app.js`) so every view renders
without a backend.

## Deploy (Vercel)

This folder is a self-contained static site.

```bash
cd web
vercel            # or connect the repo and set the root directory to web/
```

On deploy, replace `config.js` with live values (see `config.example.js`):
the API base URL, Cognito domain + client id, and the CloudFront/Vercel
`redirectUri`. Re-enable auth by flipping `LOGIN_DISABLED = false` in `app.js`.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | the public landing page (appliedin.dev) |
| `dashboard.html` | the app shell: nav, topbar, views, detail drawer |
| `landing.css`, `landing.js` | landing page styles and docs behaviour |
| `styles.css` | wireframe design system (Inter, monochrome, light/dark) |
| `app.js` | router, five views, detail drawer, live refresh |
| `auth.js` | Cognito Hosted-UI PKCE (kept ready; bypassed for now) |
| `demo-data.js` | sample data for demo mode |
| `config.js` | runtime config (CDK/Vercel overwrites on deploy) |
