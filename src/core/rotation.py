"""Rotating profiles — one address per N applications at a company.

An employer caps applications per identity, and it enforces that cap on the email
address: OpenAI takes five in a hundred and eighty days, Ramp two in sixty. Once
an address is spent at a company every further application there is refused at
submit time, after a tailored résumé, a browser session and an approval have
already been spent to be told no.

A rotating profile is a template rather than an identity: a base address, the
owner's real phone, and a limit. Bound to a company, it hands every application
to that company an ALIAS of the base address — `owner+io@gmail.com` — and
mints the next one when the current alias reaches its limit. The name, the phone,
the work history and the résumé are unchanged and true. The only thing that moves
is the string in the email field, and every alias delivers to the same inbox.

Two things make this safe rather than merely clever, and both are enforced here:

  A word is burned the moment it is minted, across every company at once, so no
  two applications anywhere can land on the same address and make the count lie.

  The address is settled at DISPATCH, not at tailoring, and the résumé's contact
  line is re-rendered to match before the form is filled — so the PDF and the
  form can never disagree. Deciding at tailoring looked equivalent and was not:
  it spent addresses on résumés that never went anywhere, eight of them in one
  OpenAI backlog before a single application existed.

What this does NOT do is retry. An employer that refuses under its own cap stays
refused — `application_limit` remains terminal in the apply queue. Rotation is
driven by a count this module keeps, never by a board's refusal; a refusal means
the configured limit was too high for that company, and the fix is to lower it.

The ledger lives in `<local_dir>/rotation.yaml`, beside profiles.yaml and
git-ignored: it holds real addresses, and `config/watchlist.yaml` is tracked.
"""

from __future__ import annotations

import os
import random
import re
import threading
from pathlib import Path

from .config import get_settings
from .logging import get_logger

log = get_logger(__name__)

# Minting reads-modifies-writes one file while several applications run
# concurrently in the same daemon process. Without this two of them mint at the
# same moment and one entry is lost — which means one word burned twice.
_lock = threading.RLock()

DEFAULT_LIMIT = 5

# Only Gmail ignores dots in the local part. Anywhere else `ow.ner@…` is a
# DIFFERENT mailbox, i.e. an address that silently drops every recruiter reply.
GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})

# plus — owner+io@gmail.com. Works anywhere sub-addressing does.
# dot  — ow.ner@gmail.com. Gmail only, for boards that reject a "+".
STYLES = ("plus", "dot")

# What actually spends one of an address's five: an application that went out, or
# may have. Nothing else does.
#
# This was the other way round at first — everything held a slot unless it was
# clearly dead — and it was wrong in the way that matters. Five OpenAI jobs were
# dispatched, gated on a missing résumé, and came back to the board having
# submitted nothing; the address read 5/5 and was retired without a single
# application. A dispatch is not a submission. `submitting` counts because it is
# set immediately before submit, and a failure with fail_kind `uncertain` counts
# because the page may already have taken it — "may have applied" reads as "has".
_SPENT = frozenset({"applied", "applied_manual", "submitting"})

# Letters only. Every tag in the pool is a domain extension — `+io`, `+dev`,
# `+us` — which is short by construction and says nothing about the person, the
# employer, or the fact that anything is being rotated.
_WORD_RX = re.compile(r"^[a-z]{2,14}$")


def norm(company: str) -> str:
    """The key a binding is stored under — same shape the apply queue uses, so
    'OpenAI', 'openai' and ' OpenAI ' are one company rather than three."""
    return (company or "").strip().lower()


def _path() -> Path:
    return Path(get_settings().local_dir) / "rotation.yaml"


def _words_path() -> Path:
    return Path(get_settings().config_dir) / "alias_words.yaml"


_EMPTY: dict = {"companies": {}, "aliases": [], "used_words": [], "used_emails": []}


def _load() -> dict:
    """The ledger, or an empty one. A file nobody can parse must never be able to
    stop an application, so a broken ledger reads as 'no rotation configured' —
    the same way `profiles.load()` already behaves."""
    import yaml

    path = _path()
    if not path.exists():
        return {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in _EMPTY.items()}
    try:
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise TypeError("rotation.yaml is not a mapping")
        out = {
            "companies": dict(data.get("companies") or {}),
            "aliases": list(data.get("aliases") or []),
            "used_words": list(data.get("used_words") or []),
            "used_emails": list(data.get("used_emails") or []),
        }
        if not all(isinstance(v, dict) for v in out["companies"].values()):
            raise TypeError("a company binding is not a mapping")
        return out
    except Exception:  # noqa: BLE001 — see the docstring: never block an apply
        log.warning("could not read %s — rotation is off until it is fixed", path,
                    exc_info=True)
        return {}


def _save(data: dict) -> None:
    """Write through a temporary file so a crash mid-write cannot leave a ledger
    that parses as 'no aliases' — which would re-mint words already in use."""
    import yaml

    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    os.replace(tmp, path)


# --- bindings --------------------------------------------------------------

def binding(company: str) -> dict | None:
    """{'profile', 'limit', 'style'} for a company, or None when it is unbound.
    An unbound company behaves exactly as it did before this module existed."""
    data = _load()
    row = (data.get("companies") or {}).get(norm(company))
    return dict(row) if row else None


def bindings() -> dict:
    return dict(_load().get("companies") or {})


def bind(company: str, profile_id: str, limit: int = 0, style: str = "") -> dict:
    """Point a company at a rotating profile. Raises ValueError on a combination
    that could not work, rather than accepting it and failing at submit time."""
    from . import profiles as _profiles

    prof = _profiles.get(profile_id)
    if not prof:
        raise ValueError(f"no profile {profile_id!r}")
    if prof.kind != "rotating":
        raise ValueError(f"{prof.label!r} is not a rotating profile")
    chosen = (style or prof.style or "plus").lower()
    if chosen not in STYLES:
        raise ValueError(f"unknown alias style {chosen!r}")
    if chosen == "dot" and prof.email.split("@")[-1].lower() not in GMAIL_DOMAINS:
        raise ValueError("dot aliases only work on Gmail — every other provider "
                         "treats a dotted address as a different mailbox")
    row = {"profile": prof.id, "limit": int(limit or prof.limit or DEFAULT_LIMIT),
           "style": chosen}
    with _lock:
        data = _load() or dict(_EMPTY)
        data.setdefault("companies", {})[norm(company)] = row
        _save(data)
    log.info("rotation bound: %s → %s (limit %s, %s)", company, prof.id,
             row["limit"], row["style"])
    return row


def unbind(company: str) -> bool:
    """Stop minting for a company. Aliases already minted are RETIRED, never
    deleted: the ledger is the record of which address received which employer's
    mail, and a job already stamped with one keeps applying under it."""
    with _lock:
        data = _load() or dict(_EMPTY)
        gone = (data.get("companies") or {}).pop(norm(company), None)
        for alias in data.get("aliases") or []:
            if alias.get("company") == norm(company):
                alias["retired"] = True
        if gone is not None:
            _save(data)
    return gone is not None


# --- the word pool ---------------------------------------------------------

def _pool() -> list[str]:
    import yaml

    try:
        data = yaml.safe_load(_words_path().read_text()) or {}
        return [w for w in (data.get("words") or []) if _WORD_RX.match(str(w))]
    except Exception:  # noqa: BLE001
        log.warning("could not read %s — falling back to a numbered suffix",
                    _words_path(), exc_info=True)
        return []


def _in_use_by_a_profile() -> tuple[set[str], set[str]]:
    """The tags and addresses the owner's own profiles already occupy.

    Someone who has been rotating by hand already has `…+app@gmail.com` in
    profiles.yaml, and minting `app` again would put a second company's
    applications on an address the first one is already counting. The ledger
    cannot know about those, so they are read straight off the profiles.
    """
    from . import profiles as _profiles

    words, emails = set(), set()
    try:
        rows, _ = _profiles.load()
    except Exception:  # noqa: BLE001 — an unreadable profile list is not fatal here
        return words, emails
    for p in rows:
        local, _, _ = p.email.partition("@")
        emails.add(p.email.lower())
        if "+" in local:
            words.add(local.split("+", 1)[1].lower())
    return words, emails


def _next_word(data: dict) -> str:
    """An unused word, at random. Random rather than sequential so two addresses
    do not read as a series; burned on use so it can never come back."""
    used = set(data.get("used_words") or []) | _in_use_by_a_profile()[0]
    pool = _pool()
    free = [w for w in pool if w not in used]
    if free:
        return random.choice(free)
    # Every word spent — a few hundred addresses at five applications each. Pair
    # two of them rather than start numbering: `+jobsreachme` is still a phrase
    # somebody could have chosen, and `+alias417` is visibly a machine.
    for a in pool:
        for b in pool:
            if (a + b) not in used and len(a + b) <= 16:
                return a + b
    raise ValueError("the alias word pool is exhausted")


def _dot_addresses(local_part: str):
    """Every Gmail-equivalent spelling of a local part, shortest first.

    One dot, then two, never adjacent, never leading or trailing — the placements
    Gmail accepts and treats as the same mailbox, and which no ATS normalises.
    """
    from itertools import combinations

    slots = range(1, len(local_part))
    for count in (1, 2, 3):
        for spots in combinations(slots, count):
            if any(b - a == 1 for a, b in zip(spots, spots[1:], strict=False)):
                continue
            out, last = [], 0
            for i in spots:
                out.append(local_part[last:i])
                last = i
            out.append(local_part[last:])
            yield ".".join(out)


# --- aliases ---------------------------------------------------------------

def _alias_email(base: str, style: str, word: str, taken: set[str]) -> str:
    local, _, domain = base.partition("@")
    if style == "dot":
        for candidate in _dot_addresses(local):
            addr = f"{candidate}@{domain}"
            if addr.lower() not in taken:
                return addr
        raise ValueError(f"no unused dot spelling left for {base}")
    return f"{local}+{word}@{domain}"


def _alias_id(email: str) -> str:
    """A stable id for a row's `profile_id`. Derived from the address so the
    ledger, the tracking row and the dashboard all name the same thing."""
    local = email.split("@")[0]
    return "rot-" + re.sub(r"[^a-z0-9]+", "-", local.lower()).strip("-")


def _mint(data: dict, company: str, prof, style: str) -> dict:  # noqa: ANN001
    """Add the next alias for a company and burn what it consumed."""
    taken = {e.lower() for e in (data.get("used_emails") or [])}
    taken |= _in_use_by_a_profile()[1]   # addresses the owner set up by hand
    word = _next_word(data) if style != "dot" else ""
    email = _alias_email(prof.email, style, word, taken)
    from datetime import date

    alias = {"id": _alias_id(email), "profile": prof.id, "company": norm(company),
             "email": email, "word": word, "style": style,
             "created": date.today().isoformat(), "retired": False}
    for old in data.get("aliases") or []:
        if old.get("company") == norm(company) and not old.get("retired"):
            old["retired"] = True
    data.setdefault("aliases", []).append(alias)
    if word:
        data.setdefault("used_words", []).append(word)
    data.setdefault("used_emails", []).append(email)
    log.info("minted alias %s for %s", email, company)
    return alias


def aliases(company: str = "") -> list[dict]:
    """Every alias ever minted, newest last. The history is the point."""
    rows = _load().get("aliases") or []
    return [dict(a) for a in rows
            if not company or a.get("company") == norm(company)]


def alias(alias_id: str) -> dict | None:
    return next((a for a in aliases() if a.get("id") == alias_id), None)


def _live(data: dict, company: str) -> dict | None:
    return next((a for a in reversed(data.get("aliases") or [])
                 if a.get("company") == norm(company) and not a.get("retired")), None)


def used(alias_id: str, stores) -> int:  # noqa: ANN001
    """How many applications this address has actually sent.

    Counted off the tracking rows rather than kept as a number, so it cannot
    drift from what happened. ONLY a submission spends a slot. This was the other
    way round at first — every row holding the alias counted, in-flight ones
    included — and it was wrong in the way that matters: five OpenAI jobs were
    dispatched, gated on a missing résumé and came back having submitted nothing,
    and the address read 5 of 5 and was retired without ever being used.

    `uncertain` counts, because it means the page may already have taken the
    application, and the safe reading of "may have applied" is "has applied".
    """
    n = 0
    for row in stores.tracking.all():
        if row.get("profile_id") != alias_id:
            continue
        if str(row.get("status") or "") in _SPENT or str(row.get("fail_kind") or "") == "uncertain":
            n += 1
    # The board lives in Redis; the ledger is a file. Each dispatch also writes
    # the pk into the alias, so the count survives a flushed Redis, a rebuilt
    # board or a restore — losing it would mean handing a spent address five more
    # applications. Both sides count the same events, so the larger is the true
    # one and neither can undercount the other.
    return max(n, len((alias(alias_id) or {}).get("used_pks") or []))


def state(company: str, stores) -> dict | None:  # noqa: ANN001
    """What the dashboard shows: the address in use, and how much of it is left."""
    b = binding(company)
    if not b:
        return None
    live = _live(_load(), company)
    return {"company": norm(company), "profile": b["profile"], "limit": b["limit"],
            "style": b["style"], "alias": (live or {}).get("id", ""),
            "email": (live or {}).get("email", ""),
            "used": used(live["id"], stores) if live else 0,
            "minted": len(aliases(company))}


def retire(company: str) -> bool:
    """Retire the address in use so the next application starts on a fresh one.

    The button offered when a board refuses under its own cap while our count was
    still below the limit: the alias is spent whatever we counted.
    """
    with _lock:
        data = _load() or dict(_EMPTY)
        live = _live(data, company)
        if not live:
            return False
        live["retired"] = True
        _save(data)
    log.info("retired alias %s for %s", live.get("email"), company)
    return True


# --- assignment ------------------------------------------------------------

# Work that has not been sent yet and still could be: turning rotation on should
# reach these, or every job already on the board keeps the address the whole
# point was to stop using. `applied` and `uncertain` are absent deliberately —
# those have been sent, and rewriting what they went out under would be a lie.
#
# Ordered by how close each is to actually going out. The list is bounded by what
# the address has room for — five, usually — and the board holds hundreds, so
# whichever rows are seen first take those slots. Unordered, five untailored
# postings could take them while thirty finished résumés wait, and each of those
# five costs a tailoring run before anything is sent.
_READINESS = ("needs_human", "tailored", "failed", "tailoring", "found")
_IN_FLIGHT = frozenset(_READINESS)


def _by_readiness(rows, company: str):  # noqa: ANN001, ANN201
    """This company's un-sent rows, closest to being applied first."""
    mine = [r for r in rows
            if norm(r.get("company", "")) == norm(company)
            and str(r.get("status") or "") in _IN_FLIGHT]
    return sorted(mine, key=lambda r: _READINESS.index(str(r.get("status"))))


def backfill(company: str, stores) -> dict:  # noqa: ANN001
    """Take this company's un-sent jobs off whatever identity they carry.

    Two different things happen, and the split is the point. As many as the
    current address has room for are STAMPED with it now and their résumés
    re-rendered from the saved .tex, so the board shows exactly what the next few
    applications will go out under. Every other un-sent job simply has its old
    identity CLEARED: it must not keep the address being retired, but stamping it
    would mint one address per five jobs across a board of hundreds, spending the
    pool on applications that may never be sent. Those get their address from
    `ensure()` at dispatch, which is the only moment the count can be right.

    No model is called either way — a re-render rewrites the contact line only.
    """
    from . import profiles as _profiles

    b = binding(company)
    if not b:
        return {"ok": False, "error": f"{company} does not rotate"}
    with _lock:
        data = _load()
        if not data:
            return {"ok": False, "error": "the rotation ledger could not be read"}
        live = _live(data, company)
        if live is None:
            parent = _profiles.get(b["profile"])
            if not parent:
                return {"ok": False, "error": "the rotating profile is gone"}
            live = _mint(data, company, parent, b["style"])
            _save(data)
    alias = profile_for(live["id"])
    room = max(0, int(b["limit"] or DEFAULT_LIMIT) - used(live["id"], stores))

    moved, left, cleared, pks = 0, 0, 0, []
    for row in _by_readiness(stores.tracking.all(), company):
        # `failed` covers two very different things. Most never reached a form.
        # One did: an UNCERTAIN submit means the page may already have taken it,
        # under whatever address it carried at the time. Re-pointing that row
        # would rewrite what an application that may exist went out under.
        if str(row.get("fail_kind") or "") == "uncertain":
            continue
        current = str(row.get("profile_id") or "")
        if current == alias.id or alias_of_company(current, company):
            continue                      # already on a rotating address here
        pk = row.get("pk", "")
        if not room:
            # Past what this address can carry. It still must not keep the old
            # identity — that is the address being retired — but it must not be
            # given a new one either: stamping every row would mint one address
            # per five jobs across the whole board, spending the pool on
            # applications that may never be sent. So the stale identity is
            # cleared and `ensure()` hands this job an address as it is
            # dispatched, which is the only moment the count can be right.
            if current:
                stores.tracking.set_status(pk, row.get("status"), profile_id="")
                cleared += 1
            left += 1
            continue
        stores.tracking.set_status(pk, row.get("status"), profile_id=alias.id)
        _profiles.retarget(pk, alias, stores)   # contact line only; no model call
        moved += 1
        room -= 1
        pks.append(pk)
    log.info("backfill %s: %s on %s, %s cleared for an address at dispatch, %s waiting",
             company, moved, alias.email, cleared, left)
    return {"ok": True, "company": norm(company), "alias": alias.id,
            "email": alias.email, "moved": moved, "cleared": cleared,
            "left": left, "pks": pks}


def ensure(pk: str, company: str, stores) -> object | None:  # noqa: ANN001
    """The address for an application about to go out, decided now.

    `assign()` runs during tailoring, which covers everything discovered after
    rotation was turned on. It cannot cover the job tailored last week and
    approved today: that row was stamped before the company rotated, and without
    this it would apply under exactly the address rotation exists to retire.

    So the apply path calls this last. If the row is not already on one of this
    company's addresses, it gets one and its résumé is re-rendered from the saved
    .tex to match — the contact line only, no model call, so the PDF and the form
    still agree.
    """
    from . import profiles as _profiles

    if not binding(company):
        return None
    row = stores.tracking.get(pk) or {}
    current = str(row.get("profile_id") or "")
    if alias_of_company(current, company):
        return profile_for(current)
    # A hand-picked identity is not honoured here. Rotation is a decision about
    # this employer and it was made after that pick; leaving it would send the
    # application from an address the count knows nothing about.
    #
    # It is cleared only for as long as the assignment takes, and PUT BACK if that
    # fails. Clearing first and raising second destroyed the owner's explicit
    # choice on the way out: the application stopped, correctly, and the row came
    # back with no identity on it at all, so the next person to look could not see
    # what it had been meant to go out under.
    previous = current
    stores.tracking.set_status(pk, row.get("status"), profile_id="")
    try:
        alias = assign(pk, company, stores)
    except Exception:
        stores.tracking.set_status(pk, row.get("status"), profile_id=previous)
        raise
    if alias is None:
        stores.tracking.set_status(pk, row.get("status"), profile_id=previous)
        # The company rotates, and we could not produce the address it should go
        # out under — an unreadable ledger, a deleted profile. Falling through
        # would send this application from the base address, which is the exact
        # thing rotation exists to prevent and cannot be taken back. Stopping
        # costs one application; not stopping costs the address.
        raise RuntimeError(
            f"{company} rotates its address but none could be assigned — "
            f"nothing was submitted. Check the rotating profile and the ledger.")
    _profiles.retarget(pk, alias, stores)
    return alias


def record_submission(pk: str, profile_id: str) -> None:
    """An application went out (or may have) under this address — write it to the
    file, so the count outlives the board.

    Called where a submit is recorded, NOT where one is dispatched. Recording at
    dispatch counted five gated attempts that submitted nothing and retired an
    address that had never been used.
    """
    if str(profile_id or "").startswith("rot-"):
        _record_use(profile_id, pk)


def _record_use(alias_id: str, pk: str) -> None:
    """Write this submission into the ledger against the address that carried it."""
    with _lock:
        data = _load()
        if not data:
            return
        for row in data.get("aliases") or []:
            if row.get("id") != alias_id:
                continue
            used_pks = row.setdefault("used_pks", [])
            if pk not in used_pks:
                used_pks.append(pk)
                _save(data)
            return


def rotate_and_approve(company: str, stores, queue) -> dict:  # noqa: ANN001
    """Re-point this company's un-sent work at its rotating address, then approve
    and queue it. Returns what happened, plus the pks that still need tailoring.

    The three steps have to happen in this order or they fight each other. Jobs
    already sitting in the apply queue were queued under the OLD address, and the
    queue item is what gets dispatched — so they come out first and go back in
    after the re-point, or the very applications this exists to fix go out under
    the address being retired.

    It cannot run away. The re-point stops at whatever the current address has
    room for, so this approves at most that many however big the board is. A row
    gated on a REAL question is left alone: "no questions asked" means not asking
    again for an approval this press already gave, not answering something nobody
    has answered.
    """
    b = binding(company)
    if not b:
        return {"ok": False, "error": f"{company} does not rotate"}

    key = norm(company)
    dequeued = 0
    for item in queue.pending():
        if norm(item.get("company", "")) == key and queue.remove(item.get("pk", "")):
            dequeued += 1

    out = backfill(company, stores)
    if not out.get("ok"):
        return out

    # EVERYTHING with a résumé goes in the queue, not just the few the current
    # address has room for. The limit is not a limit on how much work may be
    # queued — it is a limit on how many applications one ADDRESS may carry, and
    # that is enforced where it belongs: `ensure()` runs as each application is
    # dispatched and hands it the current address, or mints the next one if that
    # is full. So an address is spent only when an application is actually sent,
    # and the queue is free to hold the whole board.
    picked, blocked, untailored = [], 0, []
    for row in _by_readiness(stores.tracking.all(), company):
        status = str(row.get("status") or "")
        pk = row.get("pk", "")
        if status in ("found", "tailoring"):
            # No résumé to send. Tailoring is the expensive stage, so only the
            # ones already stamped are done here — the rest are what "Process
            # applications" is for, which is its own decision about spend.
            if pk in out["pks"]:
                untailored.append(pk)
            continue
        if status not in ("tailored", "needs_human"):
            continue
        question = (row.get("gate_pending") or {}).get("question", "")
        if (status == "tailored" or row.get("gate_reason") == "approval"
                or question.startswith("Ready to apply")):
            picked.append((pk, row.get("company") or company))
        else:
            blocked += 1

    queued = sum(1 for pk, co in picked if queue.put(pk, co))
    log.info("rotate-and-approve %s: %s stamped with %s, %s queued, %s to tailor, "
             "%s left gated", company, out["moved"], out["email"], queued,
             len(untailored), blocked)
    return {**out, "dequeued": dequeued, "queued": queued, "blocked": blocked,
            "untailored": untailored, "tailoring": len(untailored)}


def alias_of_company(alias_id: str, company: str) -> bool:
    """Whether this id is one of the addresses minted for this company."""
    if not alias_id:
        return False
    return any(a.get("id") == alias_id for a in aliases(company))


def profile_for(alias_id: str):  # noqa: ANN201
    """The alias as an ordinary Profile, so every existing reader works unchanged
    — the form filler, the résumé rewrite, the drawer's 'Apply as'."""
    from . import profiles as _profiles

    row = alias(alias_id)
    if not row:
        return None
    parent = _profiles.get(row.get("profile", ""))
    return _profiles.Profile(
        id=row["id"], label=f"{parent.label if parent else 'Rotating'} · {row['email']}",
        email=row["email"], phone=parent.phone if parent else "", kind="alias")


def assign(pk: str, company: str, stores) -> object | None:  # noqa: ANN001
    """Stamp this job with the address it will go out under.

    Returns the alias Profile, or None when the company has no rotating profile
    bound or the owner already chose an identity for this job by hand. Called
    once, at the top of `run_job`, before the job is scored or tailored.
    """
    b = binding(company)
    if not b:
        return None
    row = stores.tracking.get(pk) or {}
    chosen = str(row.get("profile_id") or "").strip()
    if chosen:
        # Either the owner picked an identity for this job — an explicit choice
        # outranks the rule — or it is a rotation alias from an earlier attempt,
        # and a retry must keep the address it already reserved rather than burn
        # a second slot for one application.
        return profile_for(chosen)

    from . import profiles as _profiles

    parent = _profiles.get(b["profile"])
    if not parent or parent.kind != "rotating":
        log.warning("%s is bound to %r, which is not a rotating profile — skipping",
                    company, b.get("profile"))
        return None

    with _lock:
        data = _load()
        if not data:                      # unreadable ledger: no rotation, no harm
            return None
        live = _live(data, company)
        if live is None or used(live["id"], stores) >= int(b["limit"] or DEFAULT_LIMIT):
            live = _mint(data, company, parent, b["style"])
            _save(data)

    prof = profile_for(live["id"])
    stores.tracking.set_status(pk, row.get("status") or "found", profile_id=prof.id)
    try:
        from .events import emit

        emit("running", pk=pk, agent="workflow",
             detail=f"applying as {prof.email} · rotating profile for {company}")
    except Exception:  # noqa: BLE001 — a feed that is down must not stop an apply
        pass
    return prof
