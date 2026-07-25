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
# Phone shapes a résumé actually uses: +1 (413) 275-8723, 413-275-8723, 4132758723
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
        email = str(row.get("email") or "").strip()
        if not email:
            continue  # a profile without an address cannot answer anything
        pid = str(row.get("id") or email.split("@")[0]).strip()
        profiles.append(Profile(id=pid, label=str(row.get("label") or pid),
                                email=email, phone=str(row.get("phone") or "").strip()))
    default = str(data.get("default") or (profiles[0].id if profiles else ""))
    return profiles, default


def save(profiles: list[dict], default: str = "") -> tuple[list[Profile], str]:
    import yaml

    rows = []
    for p in profiles:
        email = str(p.get("email") or "").strip()
        if not email:
            continue
        pid = str(p.get("id") or email.split("@")[0]).strip()
        rows.append({"id": pid, "label": str(p.get("label") or pid).strip(),
                     "email": email, "phone": str(p.get("phone") or "").strip()})
    ids = {r["id"] for r in rows}
    chosen = default if default in ids else (rows[0]["id"] if rows else "")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"default": chosen, "profiles": rows},
                                   sort_keys=False, allow_unicode=True))
    return load()


def get(profile_id: str) -> Profile | None:
    profiles, _ = load()
    return next((p for p in profiles if p.id == profile_id), None)


def resolve(profile_id: str = "") -> Profile | None:
    """The profile to use: the one asked for, else the default, else none."""
    profiles, default = load()
    if not profiles:
        return None
    if profile_id:
        hit = next((p for p in profiles if p.id == profile_id), None)
        if hit:
            return hit
    return next((p for p in profiles if p.id == default), profiles[0])


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
