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
    except Exception:  # noqa: BLE001 — keep the good PDF rather than break the job
        log.warning("retarget render failed for %s — keeping the previous PDF", pk,
                    exc_info=True)
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
