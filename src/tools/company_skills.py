"""Per-site custom instructions, loaded from the `site-quirks` agent skill.

Most sites apply fine with the generic logic. The few that don't each get a
markdown file describing their quirks, and those notes are injected into the
browser agent's task for that site only. See that skill's SKILL.md for the format.

Files are read on every apply (they're tiny, and a cache would mean restarting
the daemon to fix a bad note mid-run). Nothing here ever raises: a malformed
skill file must degrade to "no extra instructions", never break an application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from core.logging import get_logger

log = get_logger(__name__)


@dataclass
class SiteSkill:
    """Merged instructions for one site (company file + its ATS file)."""

    names: list[str] = field(default_factory=list)
    notes: str = ""
    allow_domains: list[str] = field(default_factory=list)
    success_phrases: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.notes or self.allow_domains or self.success_phrases)


def _skills_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "agent" / "skills" / "site-quirks"


def _parse(path: Path) -> tuple[dict, str]:
    """Split a skill file into (frontmatter, body). Tolerates a missing header."""
    import yaml

    text = path.read_text()
    if not text.startswith("---"):
        return {}, text.strip()
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text.strip()
    meta = yaml.safe_load(parts[1]) or {}
    return (meta if isinstance(meta, dict) else {}), parts[2].strip()


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _matches(meta: dict, host: str, company: str) -> bool:
    hosts = [h.lower() for h in _as_list(meta.get("match_hosts"))]
    if host and any(h in host for h in hosts):
        return True
    names = [c.lower().strip() for c in _as_list(meta.get("match_companies"))]
    return bool(company) and company.lower().strip() in names


def load_skill(url: str, company: str = "") -> SiteSkill:
    """Every skill matching this job, merged. Company files come LAST so their
    notes read as the final word on a site the ATS file also describes."""
    skill = SiteSkill()
    root = _skills_dir()
    if not root.is_dir():
        return skill
    host = (urlparse(url or "").hostname or "").lower()

    # ATS-level files first, then company-level (companies/ override/extend).
    files = sorted(root.glob("*.md")) + sorted((root / "companies").glob("*.md"))
    for path in files:
        if path.name.lower() in ("skill.md", "readme.md"):
            continue  # the skill's own index, not a site's rules
        try:
            meta, body = _parse(path)
        except Exception:  # noqa: BLE001 — a broken note must not break an apply
            log.warning("could not read company skill %s — ignoring", path.name,
                        exc_info=True)
            continue
        # A company file may also be selected by filename (companies/uber.md),
        # so a note can be added without writing any frontmatter at all.
        by_filename = (path.parent.name == "companies"
                       and company and path.stem.lower() == company.lower().strip())
        if not (by_filename or _matches(meta, host, company)):
            continue
        name = str(meta.get("name") or path.stem)
        skill.names.append(name)
        if body:
            skill.notes += (f"\n\n### {name}\n{body}" if skill.notes else f"### {name}\n{body}")
        skill.allow_domains += _as_list(meta.get("allow_domains"))
        skill.success_phrases += _as_list(meta.get("success_phrases"))
    if skill.names:
        log.info("company skills for %s: %s", company or host, ", ".join(skill.names))
    return skill


def instructions_for(url: str, company: str = "") -> str:
    """The notes block to inject into the apply agent's task ('' when none)."""
    skill = load_skill(url, company)
    if not skill.notes:
        return ""
    return ("\n\nSITE-SPECIFIC RULES (learned from earlier applies to this site — "
            "they override your general habits):\n" + skill.notes)
