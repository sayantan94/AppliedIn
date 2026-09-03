"""Application profiles — which identity a given application goes out under.

One person often applies from more than one contact: a personal address and a
dedicated job-hunting one, two numbers, an alias a particular employer already
knows. Portals also cap applications per identity, and the pipeline already had a
single hard-coded "fallback profile" for exactly that. This generalises it: any
number of profiles, one chosen per application.

A profile is only ever an email and a phone the owner entered themselves. Nothing
here invents an identity, and the résumé is re-rendered to match whichever profile
an application uses, so the contact details on the PDF are never a different
person from the ones typed into the form.

Profiles live in `<local_dir>/profiles.yaml` — beside facts.md and secrets.json,
git-ignored, because they are personal data rather than configuration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import get_settings
from .logging import get_logger

log = get_logger(__name__)

_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Phone shapes a résumé actually uses: +1 (555) 010-0123, 555-010-0123, 5550100123
# Keys a form might use for the same two facts. Deliberately broad: a missed key
# means one field keeps the old identity.
_EMAIL_KEY_RX = re.compile(r"e-?mail", re.I)
_PHONE_KEY_RX = re.compile(r"\bphone\b|mobile|cell\b|telephone", re.I)

_PHONE_RX = re.compile(r"(?:\+?\d{1,2}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}")


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    email: str
    phone: str = ""
    # "fixed"    — an address the owner typed in, used exactly as written
    # "rotating" — a TEMPLATE: never submitted itself, it mints one alias per
    #              company and rotates at `limit` (see core/rotation.py)
    # "alias"    — one address minted from a rotating template
    kind: str = "fixed"
    limit: int = 0        # rotating only: applications one alias may carry
    style: str = "plus"   # rotating only: plus-addressing or the Gmail dot

    def as_facts(self) -> dict[str, str]:
        """The canonical keys, for a bank that has none of its own yet."""
        out = {"Email": self.email, "Email address": self.email}
        if self.phone:
            out.update({"Phone": self.phone, "Phone number": self.phone})
        return out

    def override(self, facts: dict) -> dict:
        """Every contact answer in `facts`, replaced with this profile's.

        Matching by key PATTERN rather than a fixed list, because forms ask in
        wording nobody can enumerate — "Email", "Email address", "Preferred email",
        "Contact e-mail", "Mobile", "Cell phone". A profile that only covered the
        two obvious spellings would leave the old address in the third field, and
        an application going out under two different addresses is worse than one
        going out under the wrong one.
        """
        out = dict(facts)
        for key in list(out):
            if _EMAIL_KEY_RX.search(key):
                out[key] = self.email
            elif self.phone and _PHONE_KEY_RX.search(key):
                out[key] = self.phone
        for k, v in self.as_facts().items():
            out.setdefault(k, v)
        return out


_INVISIBLE_RX = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def clean(value: str) -> str:
    """Strip what a paste drags in: zero-width and bidi format characters.

    A phone copied from a contacts app often carries U+202C; it renders as
    nothing, so the profile looks right in the UI and the employer receives a
    number with a control character embedded in it.
    """
    return _INVISIBLE_RX.sub("", str(value or "")).strip()


def _path() -> Path:
    return Path(get_settings().local_dir) / "profiles.yaml"


def load() -> tuple[list[Profile], str]:
    """Every profile, plus the id of the default. ([], "") when none are set up —
    the pipeline then behaves exactly as it did before profiles existed."""
    import yaml

    path = _path()
    if not path.exists():
        return [], ""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except Exception:  # noqa: BLE001 — a broken file must not stop an apply
        log.warning("could not read %s — ignoring profiles", path, exc_info=True)
        return [], ""
    profiles = []
    for row in data.get("profiles") or []:
        email = clean(row.get("email"))
        if not email:
            continue  # a profile without an address cannot answer anything
        pid = str(row.get("id") or email.split("@")[0]).strip()
        profiles.append(Profile(id=pid, label=str(row.get("label") or pid),
                                email=email, phone=clean(row.get("phone")),
                                kind=str(row.get("kind") or "fixed"),
                                limit=int(row.get("limit") or 0),
                                style=str(row.get("style") or "plus")))
    default = str(data.get("default") or (profiles[0].id if profiles else ""))
    return profiles, default


def save(profiles: list[dict], default: str = "") -> tuple[list[Profile], str]:
    import yaml

    rows = []
    for p in profiles:
        email = clean(p.get("email"))
        if not email:
            continue
        pid = str(p.get("id") or email.split("@")[0]).strip()
        row = {"id": pid, "label": clean(p.get("label")) or pid,
               "email": email, "phone": clean(p.get("phone"))}
        if str(p.get("kind") or "fixed") == "rotating":
            row.update(kind="rotating", limit=int(p.get("limit") or 5),
                       style=str(p.get("style") or "plus"))
        rows.append(row)
    # A rotating profile is a template whose base address must never reach a
    # form, so it can never be the default. Falling back to it would send every
    # unassigned job out under the one address rotation exists to protect.
    usable = [r["id"] for r in rows if r.get("kind") != "rotating"]
    chosen = default if default in usable else (usable[0] if usable else "")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"default": chosen, "profiles": rows},
                                   sort_keys=False, allow_unicode=True))
    return load()


def bindings() -> dict[str, str]:
    """Every company with a standing profile: {company (lower) → profile id}.

    Stored as the company's `profile_id` preference — the same value the company
    page's "Apply as" writes and discovery has always read. One value, one place:
    it used to reach only discovery, so choosing it on the company page stamped
    postings found later and did nothing for the rows already on the board.
    """
    from . import flags

    return {co: str(pref["profile_id"]) for co, pref in flags.company_prefs().items()
            if pref.get("profile_id")}


def binding(company: str) -> str:
    """The profile id every application at this company goes out under, or ""."""
    from . import flags

    return str(flags.company_pref(company).get("profile_id") or "")


def bind(company: str, profile_id: str) -> None:
    """Make a profile the standing rule for a company.

    Not the rows that exist right now, but every application at that company
    from here on — including postings discovered next week that nobody has
    looked at. Rotation works the same way; this is that shape for a profile
    the owner chose.
    """
    from . import flags

    key = (company or "").strip().lower()
    if not key:
        raise ValueError("no company")
    p = get(profile_id)
    if not p:
        raise ValueError(f"no profile {profile_id!r}")
    if p.kind == "rotating":
        raise ValueError("a rotating template cannot be a company's profile")
    flags.set_company_pref(company, {"profile_id": p.id})


def unbind(company: str) -> None:
    from . import flags

    flags.set_company_pref(company, {"profile_id": None})


def resolve_for(row: dict) -> Profile | None:
    """The profile THIS row goes out under: the row's own choice, else its
    company's standing rule, else the default. Every place that turns a row
    into an identity goes through here, so the form and the PDF cannot disagree
    about who is applying."""
    own = str((row or {}).get("profile_id") or "")
    if own:
        return resolve(own)
    company = str((row or {}).get("company") or "")
    bound = binding(company)
    if bound:
        hit = resolve(bound)
        if hit:
            return hit
        # The rule names a profile that has since been removed. Falling through
        # to the default is the honest outcome; returning nothing would send the
        # bank's raw contact details, which is the default by another name but
        # without the résumé being rendered to match.
        log.warning("standing profile %r for %s no longer exists — using the default",
                    bound, company)
    return resolve("")


def get(profile_id: str) -> Profile | None:
    profiles, _ = load()
    return next((p for p in profiles if p.id == profile_id), None)


def resolve(profile_id: str = "") -> Profile | None:
    """The profile an application actually goes out under.

    Two rules beyond "the one asked for, else the default". A minted alias lives
    in the rotation ledger rather than profiles.yaml, and a row stamped with one
    must resolve to it — that stamp is what the résumé's contact line and the
    form's answers are both read from. And a ROTATING profile is a template whose
    base address is the very one being protected, so it is never returned: asking
    for one, or falling back to one, yields nothing rather than the address that
    every alias exists to keep off a form.
    """
    profiles, default = load()
    if profile_id:
        hit = next((p for p in profiles if p.id == profile_id), None)
        if hit:
            return None if hit.kind == "rotating" else hit
        from . import rotation

        return rotation.profile_for(profile_id)
    usable = [p for p in profiles if p.kind != "rotating"]
    if not usable:
        return None
    return next((p for p in usable if p.id == default), usable[0])


def apply_to_latex(tex: str, profile: Profile | None) -> str:
    """Rewrite the résumé's contact details to match the profile it is sent under.

    An application whose form says one address while the attached PDF says another
    looks careless at best. The contact line is the only place a résumé carries
    these, so every address (and phone, when the profile has one) is swapped —
    including inside a \\href{mailto:…} — and nothing else is touched.
    """
    if not profile or not tex:
        return tex
    out = _EMAIL_RX.sub(lambda _: profile.email, tex)
    if profile.phone:
        out = _PHONE_RX.sub(lambda _: profile.phone, out)
    if out != tex:
        log.info("résumé contact rewritten for profile %r", profile.id)
    return out


def retarget(pk: str, profile: Profile | None, stores=None) -> bool:  # noqa: ANN001
    """Point an already-tailored résumé at a different profile.

    The tailoring is the expensive part and it does not change: only the contact
    line does. So this rewrites the saved .tex and re-renders the PDF, rather than
    sending the job back through the tailor — switching identity on a whole board
    should not cost a single token.

    Returns True when a new PDF was written.
    """
    from tools.render import render_pdf

    if stores is None:
        from .stores import make_stores
        stores = make_stores()
    row = stores.tracking.get(pk) or {}
    tex_key = row.get("resume_tex_key")
    if not tex_key or not profile:
        return False
    try:
        tex = stores.artifacts.get(tex_key).decode()
    except Exception:  # noqa: BLE001
        log.warning("no saved .tex for %s — cannot retarget", pk)
        return False
    updated = apply_to_latex(tex, profile)
    if updated == tex:
        return False
    try:
        pdf = render_pdf(updated)
    except Exception:  # noqa: BLE001
        # Repair and retry ONCE, exactly as the tailoring path does. Without
        # this, rotation re-rendered through a plainer path than the one that
        # wrote the document: a résumé the tailor had already repaired past a
        # bare `&` would fail here, keep its OLD address on the PDF, and go out
        # against a form carrying the new one.
        from tools.render import sanitize_latex

        pdf = None
        repaired = sanitize_latex(updated)
        if repaired != updated:
            try:
                pdf = render_pdf(repaired)
                updated = repaired
            except Exception:  # noqa: BLE001
                pdf = None
        if pdf is None:
            log.warning("retarget render failed for %s — the PDF still carries the "
                        "previous address", pk, exc_info=True)
            return False
    stores.artifacts.put("resumes", f"{pk}.tex", updated.encode(), "text/x-tex")
    key = stores.artifacts.put("resumes", f"{pk}.pdf", pdf, "application/pdf")
    stores.tracking.set_status(pk, row.get("status", "tailored"),
                               resume_s3_key=key, profile_id=profile.id)
    log.info("retargeted %s to profile %r", pk, profile.id)
    return True


# A start date written down today is wrong next month, so an answer may carry a
# relative token — "{date:+6w}" — which is resolved when a form is actually
# filled. Weeks (w), days (d) and months (m) are supported.
_DATE_TOKEN_RX = re.compile(r"\{date:\+(\d+)([dwm])\}", re.I)


def expand_dates(value: str, today=None) -> str:  # noqa: ANN001
    """Turn any relative-date token in an answer into a real date."""
    from datetime import date, timedelta

    if not value or "{date:" not in str(value):
        return value
    base = today or date.today()

    def _sub(m: re.Match) -> str:
        n, unit = int(m.group(1)), m.group(2).lower()
        days = n * (7 if unit == "w" else 30 if unit == "m" else 1)
        return (base + timedelta(days=days)).isoformat()

    return _DATE_TOKEN_RX.sub(_sub, str(value))


def expand_all(facts: dict) -> dict:
    """Resolve relative dates across a whole answer set."""
    return {k: expand_dates(v) for k, v in facts.items()}


_SENT = ("applied", "applied_manual")
_IN_FLIGHT = ("submitting",)


def usage(stores) -> dict[str, dict]:  # noqa: ANN001
    """What each profile has actually been used for, derived from the rows.

    The profile panel showed an email and nothing else, so choosing "a new one
    for Netflix" meant guessing which had already been spent there. Counted from
    the rows rather than remembered, so it is what happened.
    """
    from .ids import is_internal_pk

    out: dict[str, dict] = {}
    for row in stores.tracking.all():
        pid = str(row.get("profile_id") or "")
        if not pid or is_internal_pk(row.get("pk", "")):
            continue
        st = str(row.get("status") or "")
        bucket = ("applied" if st in _SENT else "in_flight" if st in _IN_FLIGHT else "unsent")
        u = out.setdefault(pid, {"applied": 0, "unsent": 0, "in_flight": 0, "companies": {}})
        u[bucket] += 1
        co = str(row.get("company") or "?")
        c = u["companies"].setdefault(co, {"applied": 0, "unsent": 0, "in_flight": 0})
        c[bucket] += 1
    return out


def assign_company(company: str, profile: Profile, stores, queue) -> dict:  # noqa: ANN001
    """Send everything un-sent at one company under this profile.

    The same press rotation offers, for a profile the owner chose. The rules are
    rotation's rules: anything already sent keeps its identity and is counted
    so the owner sees it was left alone; a job sitting in the apply queue was
    queued under the OLD profile and the queue item is what dispatches, so it
    comes out and goes back in; a failed or errored job is revived under the
    new profile — that is "apply again" — if it has a résumé, and goes back to
    `found` for tailoring if it does not.
    """
    from . import rotation as _rotation
    from .ids import is_internal_pk
    from .models import Status

    key = (company or "").strip().lower()
    if _rotation.binding(company):
        return {"ok": False, "company": company,
                "error": f"{company} rotates its address — retire rotation first, "
                         f"or use Rotate & queue"}
    bind(company, profile.id)
    dequeued = 0
    for item in queue.pending():
        if (item.get("company") or "").strip().lower() == key and queue.remove(item.get("pk", "")):
            dequeued += 1

    repointed = left_alone = queued = revived = 0
    for row in stores.tracking.all():
        pk = str(row.get("pk") or "")
        if (row.get("company") or "").strip().lower() != key or is_internal_pk(pk):
            continue
        st = str(row.get("status") or "")
        if st in _SENT or st in _IN_FLIGHT:
            left_alone += 1
            continue
        has_resume = bool(row.get("resume_tex_key"))
        new_status = st
        if st in ("failed", "error"):
            new_status = Status.TAILORED.value if has_resume else Status.FOUND.value
            revived += 1
        stores.tracking.set_status(pk, new_status, profile_id=profile.id,
                                   fail_reason="", fail_kind="")
        repointed += 1
        if has_resume:
            retarget(pk, profile, stores)
        question = (row.get("gate_pending") or {}).get("question", "")
        ready = new_status == Status.TAILORED.value or (
            st == "needs_human" and (row.get("gate_reason") == "approval"
                                     or question.startswith("Ready to apply")))
        if ready and queue.put(pk, row.get("company") or company):
            queued += 1

    # Rows with no résumé yet cannot be queued; the caller tailors them under
    # the new profile and queues each as it finishes, so "apply as X" ends with
    # something to press Apply on rather than a list of found postings.
    untailored = [str(r.get("pk")) for r in stores.tracking.all()
                  if (r.get("company") or "").strip().lower() == key
                  and not is_internal_pk(r.get("pk", ""))
                  and str(r.get("status") or "") in ("found", "tailoring")]
    log.info("assign %s -> %s: %d repointed, %d queued, %d revived, %d dequeued, "
             "%d left alone, %d to tailor", company, profile.id, repointed, queued,
             revived, dequeued, left_alone, len(untailored))
    return {"ok": True, "company": company, "profile": profile.id, "email": profile.email,
            "repointed": repointed, "queued": queued, "revived": revived,
            "dequeued": dequeued, "left_alone": left_alone, "untailored": untailored}


def reapply(pk: str, profile: Profile, stores) -> dict:  # noqa: ANN001
    """Apply again to a posting already applied to, under a different identity.

    The applied row is history — it holds the confirmation the employer sent
    back — and is never rewritten. A re-application is a NEW row for the same
    posting, keyed ``<pk>~2`` (then ``~3``…), stamped with the new profile,
    linked to the original both ways, and started as `found` so it goes through
    tailoring and the ordinary gate like any other job.

    The one rule that stays in code: the identity must differ from every one
    already used on this posting. The same address twice is precisely the
    duplicate the guard exists to refuse; a different profile is the only thing
    that makes "again" mean anything.
    """
    from .ids import is_internal_pk
    from .models import JobRecord, Status

    orig = stores.tracking.get(pk)
    if not orig or is_internal_pk(pk):
        return {"ok": False, "error": "no such job"}
    if str(orig.get("status") or "") not in ("applied", "applied_manual"):
        return {"ok": False, "error": "only a job already applied to can be applied to again"}
    if profile.kind == "rotating":
        return {"ok": False, "error": "a rotating template is not an identity — pick a profile"}

    root = pk.split("~", 1)[0]
    family = [r for r in stores.tracking.all()
              if str(r.get("pk") or "").split("~", 1)[0] == root]
    used = set()
    for r in family:
        p = resolve_for(r)
        if p:
            used.add(p.id)
    if profile.id in used:
        return {"ok": False,
                "error": f"{profile.label} has already applied to this posting — pick a "
                         "different identity, or this is the same application twice"}

    n = 2
    while stores.tracking.get(f"{root}~{n}"):
        n += 1
    job = JobRecord(company=orig.get("company", ""),
                    job_id=f"{root.split('#', 1)[1]}~{n}",
                    title=orig.get("title", ""), jd_url=orig.get("jd_url", ""),
                    jd_text=orig.get("jd_text") or orig.get("title", ""),
                    location=orig.get("location", ""), ats=orig.get("ats", ""))
    if not stores.tracking.put_new(job):
        return {"ok": False, "error": f"{job.pk} already exists"}
    stores.tracking.set_status(job.pk, Status.FOUND, profile_id=profile.id,
                               reapplied_from=root, reapply_n=n)
    prior = list((stores.tracking.get(root) or {}).get("reapplied_as") or [])
    stores.tracking.set_status(root, orig.get("status"), reapplied_as=prior + [job.pk])
    log.info("re-application %s of %s as %s", job.pk, root, profile.id)
    return {"ok": True, "pk": job.pk, "original": root, "profile": profile.id,
            "email": profile.email, "n": n}
