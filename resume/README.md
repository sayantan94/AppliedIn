# Résumé — baseline seed

This folder holds your **one truthful baseline résumé**. Everything the pipeline
sends is a re-emphasized copy of this, rendered through the same template — so
every application looks like your real résumé, just tuned to the JD.

## How to share your LaTeX résumé (one time)

1. **Get the source.**
   - **Overleaf:** open the project → *Menu → Download → Source*. That gives a
     zip with `main.tex` plus any `.cls`/style/font/image files it uses.
   - **Local:** your `.tex` and anything it `\input`s / `\usepackage`s from local
     files (custom `.cls`, logos, fonts).

2. **Drop it in `resume/source/`.** Put the whole thing there — the main `.tex`
   and every file it depends on. Don't flatten it; keep the structure so it
   compiles as-is.

3. **Run the seed workflow** (local, one time):
   ```bash
   uv run python scripts/seed_resume.py resume/source/main.tex
   ```
   It produces three things:
   - `resume/base.yaml` — your résumé **facts** (employers, titles, date ranges,
     degrees, certs, bullets), extracted from the `.tex`. This is the source of
     truth the truthfulness validator checks every tailored résumé against.
   - `resume/template.tex` — your layout turned into a **parameterized** LaTeX
     template (Jinja-for-LaTeX: `\VAR{...}` / `\BLOCK{...}` delimiters, chosen so
     they don't collide with LaTeX's own braces). Your visual design is preserved;
     only the content becomes slots.
   - `resume/preview.pdf` — `base.yaml` rendered through `template.tex` with
     **Tectonic** (a single self-contained LaTeX binary — LaTeX quality, no full
     TeX Live install).

4. **Eyeball `preview.pdf`.** It should match your real résumé. If a fact was
   mis-extracted or the layout slipped, fix `base.yaml`/`template.tex` and
   re-render. This is the only manual résumé step — after this, every tailored
   render is automatic and truth-guarded.

5. **Commit** `resume/base.yaml` + `resume/template.tex`. (You can keep or delete
   `resume/source/` — it's git-ignored by default so your raw résumé isn't
   committed; keep a copy somewhere safe.)

## Files

| Path | Committed? | What |
| --- | --- | --- |
| `resume/source/` | no (git-ignored) | your raw `.tex` + assets — the input |
| `resume/base.yaml` | yes | extracted facts — validator's source of truth |
| `resume/template.tex` | yes | parameterized LaTeX layout, rendered by Tectonic |
| `resume/preview.pdf` | no | local render to eyeball |

> Later, if you'd rather not touch the repo, the dashboard can grow an "upload
> résumé" screen that runs the same seed workflow server-side. For now the repo
> drop is the simplest path and keeps your résumé off any server.
