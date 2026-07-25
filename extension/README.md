# AppliedIn — assisted apply (browser extension)

The server is the product. This extension is a **driver**: it reads the form on
the page, asks AppliedIn what belongs in it, types the answers, and stops. Every
decision — which fact answers which field, what an open question should say,
which sites have quirks — is made server-side, by the same code the autonomous
pipeline uses. No API key ever reaches the page, and nothing is invented here.

## Why drive from your own browser

The pipeline drives a headless browser, and employers increasingly answer that
with a challenge. Databricks, for example, only reveals a reCAPTCHA and a
"security code — confirm you're a human" field *after* Submit is clicked, so a
perfectly filled run ends with no confirmation and no application.

Running in your own Chrome — your session, your history, your profile — looks
like what it is: a person applying. Two consequences:

- Challenges are far less likely to appear at all.
- When one does, you are already there to clear it in a click.

**This never touches a CAPTCHA and never presses Submit.** It fills; you review
and send. That is the point of the mode, not a limitation of it.

## Install

1. Start the app: `./appliedin start` (the extension talks to `127.0.0.1:8787`).
2. Chrome → `chrome://extensions` → enable **Developer mode**.
3. **Load unpacked** → select this `extension/` folder.

## Use

1. Open a job you've tailored (the board's **open job posting ↗** link).
2. Click the AppliedIn icon → **Fill this application**.
3. Review every answer. Fields still needing you are listed in the popup.
4. Submit it yourself, clearing any security check.
5. **I submitted it — mark applied** puts it on your board.

Set the app to **assisted** mode (Settings → Apply mode) so the pipeline stops at
Tailored and leaves the applying to you, rather than also driving a browser.

## What it does on the page

- Finds the application even when it's inside an **iframe** (embedded Greenhouse
  boards leave the top-level document with no form at all).
- Ignores cookie banners — their checkboxes look like form fields, and answering
  them corrupts the application and changes your privacy settings.
- Types with real key events rather than assigning values, because React
  ignores assigned values and spam filters notice input that arrives with no
  keystrokes.
- Drives comboboxes properly: click, wait for the option list, pick the match.
- Answers any **sanctions / export-control** question negatively — enforced here
  as well as on the server, because a wrong tick there is a self-reported
  disqualifier filed under your name.
- Attaches the tailored résumé to the résumé field only, never to Cover Letter.

## Endpoints it uses

| Endpoint | Purpose |
|---|---|
| `GET /extension/context` | company, title, tailored résumé, site rules |
| `POST /extension/plan` | LLM maps fields → approved answers, drafts essays |
| `POST /extension/applied` | records the application on your board |
