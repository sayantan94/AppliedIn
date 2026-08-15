# Rotating profiles — one address per five applications

## The problem

An employer caps applications per identity: OpenAI takes 5 in 180 days, Waymo 5
in 60, Ramp 2 in 60, Coinbase 3 in 180. The cap is enforced on the email address,
not on the person. Once an address is spent at a company, every further
application to that company is refused at submit time — a browser session, a
tailored résumé and an approval, all burned to be told no.

AppliedIn already has profiles: an application goes out under a chosen email and
phone, and the résumé's contact line is rewritten to match. But a profile is a
thing the owner types in by hand, so staying under a cap means creating a new
profile manually, remembering which company has seen which address, and counting
applications in your head.

A rotating profile does that counting. It is a template — a base email, a base
phone, a limit — bound to a company. Every application to that company goes out
under a generated alias of the base address; when an alias reaches the limit, the
next one is minted. The person, the phone number and the résumé are the same
throughout. Only the string in the email field changes.

### What this is and is not

The name on the application, the phone, the work history and the résumé are
unchanged and true. What rotates is the address, and every alias delivers to the
same inbox the base address does. This is a workaround of an employer's own
limit, and it is the owner's decision to make; the design's job is to make it
deliberate, recorded and reversible rather than silent. Every alias is written to
a ledger with the company and date, so "which address did I use at OpenAI, and
when" always has an answer.

## Scope

**In:** a rotating profile kind, per-company binding and limit, two alias styles
(plus-addressing and Gmail dot), a word pool, alias minting with a live count,
the ledger, the dashboard controls, and the OpenAI proof of concept.

**Out:** retrying a job that a board refused under its cap. That outcome stays
terminal exactly as it is today (`apply_queue.TERMINAL`). Rotation is driven by a
counter the pipeline keeps, never by an employer's refusal — a refusal means the
configured limit was too high, and the fix is to lower it, not to submit again.

## Data

### The rotating profile

`.local/profiles.yaml` gains a `kind` field. Absent or `fixed` means today's
behaviour, so every existing file loads unchanged.

```yaml
default: personal
profiles:
  - id: personal
    label: Personal
    email: owner@example.com
    phone: "+1 555 010 0123"
  - id: rotating
    label: Rotating
    kind: rotating
    email: owner@example.com     # the BASE — never submitted as-is
    phone: "+1 555 010 0123"          # same on every alias
    limit: 5                          # default, per alias
    style: plus                       # plus | dot
```

A rotating profile is never submitted itself. `resolve()` returns it only so the
rotation module can read the base off it; if one is ever stamped on a job row
directly, assignment replaces it with a real alias before anything is rendered.

### The ledger

All rotation state lives in one git-ignored file, `.local/rotation.yaml`, beside
`profiles.yaml`. It holds personal addresses, so it must not go near
`config/watchlist.yaml`, which is tracked and public — and a public file recording
that the owner rotates addresses to get past OpenAI's cap is exactly the wrong
thing to commit.

```yaml
companies:
  openai: {profile: rotating, limit: 5, style: plus}
aliases:
  - id: rot-app
    profile: rotating
    company: openai
    email: owner+app@gmail.com
    word: app
    style: plus
    created: 2026-08-14
    retired: false
used_words: [app]
used_emails: [owner+app@gmail.com]
```

`companies` keys are normalised company names (lowercased, same normalisation the
apply queue uses), so "OpenAI" and "openai" are one binding. A company absent
from `companies` has no rotation and behaves exactly as it does today.

`used_words` and `used_emails` are append-only. A word is burned the moment it is
minted and is never offered again for any company, so an address is unique across
the whole system, not merely within one employer. An alias is retired rather than
deleted, because the ledger is the record of which address received which
employer's mail.

### The word pool

`config/alias_words.yaml` is tracked. It holds roughly a thousand short, neutral,
lowercase tokens — `app`, `me`, `job`, `hire`, `apply`, `inbox`, … — matching
`^[a-z0-9]{2,8}$`. Nothing in it is personal, which is why it can be public.

Minting picks uniformly at random from the words not in `used_words`. Random
rather than sequential so a recipient cannot read a sequence off two addresses;
short and generic so an alias never looks like it was built to describe the
employer. If the pool is ever exhausted — 1000 aliases is 5000 applications —
minting falls back to appending a counter (`app2`).

### Alias styles

Both are built. The style is a property of the company binding, defaulting to the
profile's.

**`plus`** — `owner+app@gmail.com`. Standard sub-addressing, supported by
Gmail and most modern providers.

**`dot`** — `ow.ner@gmail.com`. Gmail (and only Gmail) ignores dots in the
local part, so every dot placement delivers to the same inbox while being a
distinct string to any system that compares addresses literally.

Two styles exist because plus-addressing has two failure modes and the dot has
neither: some ATS validators reject `+` in an email field outright, and some
normalise a plus-address back to its base before checking their own cap. The dot
form passes both, at the cost of only working for Gmail.

So `dot` is offered only when the base domain is `gmail.com` or `googlemail.com`;
for any other domain the style control is disabled with that reason shown. Dot
placements are enumerated deterministically (one dot, then two, never adjacent,
never leading or trailing) and checked against `used_emails`, which is what makes
a dot alias unique rather than a coin flip.

**No automatic style switching.** A board that rejects `+` reports it as a field
validation problem or a gate question, and the shapes those take are not
distinguishable from an ordinary form problem with any confidence. Guessing wrong
would silently change the address an application goes out under, which is the one
thing this system must not do quietly. Instead the failure card offers a one-click
"switch OpenAI to dot aliases", and the next application uses the new style. This
is a case where the human's judgement is cheap and a wrong inference is expensive.

## Assignment

A new module, `src/core/rotation.py`. One entry point matters:

```python
def assign(pk: str, company: str, stores) -> Profile | None:
    """Stamp this job with the alias it will go out under. None when the
    company has no rotating profile bound."""
```

It is called once, at the top of `run_job` in `src/agent/run.py`, before the
job is scored or tailored. That placement is the important part: the résumé's
contact line is rewritten at render time from the row's `profile_id`
(`graph.py:87`), and the form is filled from the same field (`run.py:469`). Assign
before tailoring and both read the alias, so the address on the PDF and the
address in the form are the same by construction — the existing invariant, still
enforced in the same place.

An alias is an ordinary `Profile` (`id`, `label`, `email`, `phone`), so
`profiles.resolve()` is extended to look in the ledger as well as
`profiles.yaml`. Nothing downstream changes: `override()` fills the form,
`apply_to_latex()` rewrites the résumé, `retarget()` re-renders on a change, the
drawer's "Apply as" dropdown displays it, and the tracking row records forever
which address the application went out under.

If a job already carries a non-rotating `profile_id` the owner chose by hand,
assignment leaves it alone. An explicit choice outranks a rule.

### Counting

The count for an alias is derived from the tracking store rather than kept as a
separate number, so it cannot drift from what actually happened:

> rows whose `profile_id` is this alias, whose status is not `skipped` or
> `job_gone`.

`failed` keeps its slot. An attempt that reached a form and could not confirm is
not evidence that nothing arrived, and a wasted slot costs nothing next to an
address quietly carrying a sixth application.

Both submitted and in-flight applications hold a slot. That is deliberate:
approving six OpenAI jobs at once assigns all six before any of them submits, and
counting only confirmed submissions would put all six under one address and
overshoot the limit by one. A slot is released only when a job ends without
having reached a form.

`uncertain` counts as used. It means a submission may have gone through, and the
safe reading of "may have applied" is "has applied".

### Minting

When the live count for a company's current alias has reached its limit, mint the
next: pick an unused word (or dot placement), build the address, append to
`aliases`, burn the word and the address, retire the previous alias, write the
file.

Minting is serialized behind a module-level lock and writes through a temporary
file with an atomic replace, because several applications run concurrently in one
daemon process and two of them minting at once would either lose a ledger entry or
hand the same word to two companies.

A ledger that cannot be read or parsed logs a warning and yields "no rotation" —
the job then applies under the ordinary default profile. A broken file must never
be able to stop an application, which is how `profiles.load()` already behaves.

## Cap refusals

Unchanged. `application_limit` stays in `apply_queue.TERMINAL`, the job stays
failed, and nothing is resubmitted. `_BOARD_SAID_NO` gains a sentence for the
rotating case: the employer refused under its cap while this alias was below its
configured limit, which means the limit is set too high for this company — with a
button that lowers it and retires the current alias so the next application to
that company starts on a fresh one.

## Interface

**Profiles panel** (`web/app.js`, the existing `#profpicker`). Adding a profile
offers "rotating" alongside the ordinary kind: base email, base phone, default
limit, style. A rotating profile renders with its bound companies beneath it —
each with its current alias, a live "3 of 5" count, an editable limit and a style
control.

**Company binding.** A company is bound from the same panel, or from the company
pane's identity dropdown, which gains the rotating profile as a choice. Both write
through one new endpoint, `POST /actions/rotation`, taking `{company, profile_id,
limit, style}`; an empty `profile_id` unbinds.

**Job drawer.** "Apply as" shows the alias actually assigned, its parent profile
and its count — `owner+app@gmail.com · rotating · 3 of 5` — rather than a
profile label, because the address is the thing worth checking before approving a
submission.

**Nothing is silent.** Minting emits an event onto the job's timeline: the new
address, the company, and why it was minted.

## Tests

Offline, per AGENTS.md — no network, no browser.

- A word, once minted, is never offered again — for the same company or any other.
- Rotation fires at the limit and not before: five assignments share one alias,
  the sixth mints.
- A per-company limit overrides the profile default (Ramp at 2 rotates on the
  third).
- A skipped job releases its slot; an `uncertain` one does not.
- Assignment stamps the row before tailoring, and the alias address reaches both
  `Profile.override()` and `apply_to_latex()` — the form and the PDF agree.
- A hand-chosen profile on a row survives assignment.
- Dot placements are unique, never adjacent, never leading or trailing, and the
  style is refused for a non-Gmail base.
- A corrupt or unreadable `rotation.yaml` yields no rotation rather than an
  exception, and the application proceeds under the default profile.
- Concurrent minting for two companies produces two distinct words.

## Proof of concept

OpenAI, whose cap of 5 per 180 days is already spent on the base address. Bind it
to a rotating profile with `style: plus`, apply to one role, and read the outcome:

- the form accepted the address and the submission confirmed → plus works, and
  the design is proven end to end;
- the form rejected `+` → switch that company to `dot` and repeat;
- the board refused under its cap despite a fresh address → the ATS normalises
  sub-addresses, and `dot` is the only style with a chance.

No other company is bound until OpenAI answers that question. Nothing changes for
an unbound company: no ledger entry, no alias, no behaviour change of any kind.

## Risks

**An ATS rejects the alias.** Handled by having both styles; the POC is
specifically designed to find out which one this ATS takes.

**An employer notices.** Two applications from one name at two addresses is
visible to a recruiter looking for it, and may read as gaming the cap. Nothing in
the design hides it: the name, phone and résumé are constant and the ledger
records every alias. This is the owner's call, taken deliberately, one company at
a time.

**Mail routing.** Every alias delivers to the same inbox, so no recruiter reply
can be lost. Worth a Gmail filter on the alias to keep threads sorted, which is a
manual step outside this system.
