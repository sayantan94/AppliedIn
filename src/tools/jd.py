"""Read a posting: its title, and the text the tailor works from."""

from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx
import tldextract

from core.logging import get_logger
from tools import posting_guard as pg

log = get_logger(__name__)

_OFFLINE_TLD = tldextract.TLDExtract(suffix_list_urls=())


class PostingReadUnavailable(RuntimeError):
    """The reader failed before it could establish what is on the page."""


# A shared CLI outage must not launch another session for every row in a sweep.
# HTTP/ATS reads still run; only the browser reader backs off for five minutes.
_browser_retry: tuple[float, str] = (0, "")


def _check_browser_reader() -> None:
    if _browser_retry[0] > time.monotonic():
        raise PostingReadUnavailable(_browser_retry[1])


def _note_browser_problem(problem: str) -> None:
    from tools.browser_runtime import is_infrastructure

    global _browser_retry
    if is_infrastructure(problem):
        _browser_retry = (time.monotonic() + 300, problem)

_TAG_RX = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.S | re.I)
_TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1_RX = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_SPLIT_RX = re.compile(r"\s[|\-–—]\s|\s+at\s+")
_ATS_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_WORKABLE_LOCK = threading.Lock()
_WORKABLE_NEXT_START = 0.0
_WORKABLE_CLOCK = time.monotonic
_WORKABLE_SLEEP = time.sleep
_WORKABLE_WALL_CLOCK = time.time


def _api_url(url: str):
    """Return only a fixed, vendor-derived API destination for known posting shapes."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if (p.scheme != "https" or p.username is not None or p.password is not None
                or p.port not in (None, 443) or p.query or p.fragment
                or "%" in p.path or "\\" in p.path or "//" in p.path):
            return None
        parts = p.path.strip("/").split("/")
        if any(not token or token in {".", ".."} for token in parts):
            return None
        if host.endswith(".myworkdayjobs.com"):
            tenant, dot, region = host.partition(".")
            if not dot or not re.fullmatch(r"wd\d+", region.removesuffix(".myworkdayjobs.com")):
                return None
            region = host[len(tenant) + 1:].removesuffix(".myworkdayjobs.com")
            locale = None
            if len(parts) in (4, 5) and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
                locale, parts = parts[0], parts[1:]
            if (len(parts) not in (3, 4) or parts[1] != "job" or
                    not all(_ATS_TOKEN.fullmatch(x) for x in [tenant, *parts])):
                return None
            site = parts[0]
            external = "/" + "/".join(parts)
            return ("workday", url, external,
                    (f"https://{tenant}.{region}.myworkdayjobs.com/"
                     f"wday/cxs/{tenant}/{site}/job/" + "/".join(parts[2:])),
                    {"tenant": tenant, "site": site, "external": external, "locale": locale})
        if host == "apply.workable.com":
            if (len(parts) != 3 or parts[1] != "j"
                    or not all(_ATS_TOKEN.fullmatch(x) for x in parts)):
                return None
            account, _, code = parts
            return ("workable", url, "", "https://apply.workable.com/api/v1/widget/accounts/"
                    f"{account}?details=true", {"account": account, "code": code})
        if host.endswith(".eightfold.ai"):
            tenant = host.removesuffix(".eightfold.ai")
            if (not _ATS_TOKEN.fullmatch(tenant) or len(parts) != 3
                    or parts[:2] != ["careers", "job"] or not _ATS_TOKEN.fullmatch(parts[2])):
                return None
            return ("eightfold", url, "", f"https://{host}/api/apply/v2/jobs/{parts[2]}",
                    {"tenant": tenant, "id": parts[2]})
        for suffix in (".jobs.personio.de", ".jobs.personio.com"):
            if host.endswith(suffix):
                org = host[:-len(suffix)]
                if (not _ATS_TOKEN.fullmatch(org) or len(parts) != 2 or parts[0] != "job"
                        or not parts[1].isdigit()):
                    return None
                return ("personio", url, "", f"https://{host}/xml",
                        {"org": org, "id": parts[1]})
        if host == "jobs.smartrecruiters.com":
            if len(parts) != 2 or not _ATS_TOKEN.fullmatch(parts[0]):
                return None
            posting_id = parts[1].split("-", 1)[0]
            if not _ATS_TOKEN.fullmatch(posting_id):
                return None
            company = parts[0]
            return ("smartrecruiters", url, "",
                    f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}",
                    {"company": company, "id": posting_id})
    except (ValueError, IndexError):
        return None
    return None


def _workable_start(deadline: float) -> bool:
    """Serialize only Workable start reservations; queued calls share the caller budget."""
    global _WORKABLE_NEXT_START
    remaining = deadline - _WORKABLE_CLOCK()
    if remaining <= 0 or not _WORKABLE_LOCK.acquire(timeout=remaining):
        return False
    try:
        now = _WORKABLE_CLOCK()
        start = max(now, _WORKABLE_NEXT_START)
        delay = start - now
        if start >= deadline:
            return False
        if delay:
            _WORKABLE_SLEEP(delay)
        now = _WORKABLE_CLOCK()
        if now >= deadline:
            return False
        _WORKABLE_NEXT_START = now + 1.5
        return True
    finally:
        _WORKABLE_LOCK.release()


def _retry_after(value: str | None, now: float) -> float:
    if value is None:
        return 2.0
    if re.fullmatch(r"\d+(?:\.\d+)?", value.strip()):
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return 2.0
        return max(0.0, (date - datetime.fromtimestamp(now, UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 2.0


def _workable_request(endpoint: str):
    deadline = _WORKABLE_CLOCK() + 15.0
    for attempt in range(2):
        if not _workable_start(deadline):
            return None
        remaining = deadline - _WORKABLE_CLOCK()
        if remaining <= 0:
            return None
        try:
            with httpx.Client(timeout=min(8.0, remaining), follow_redirects=False) as client:
                response = client.get(endpoint)
        except (httpx.HTTPError, TimeoutError):
            return None
        if response.status_code == 429 and attempt == 0:
            delay = _retry_after(response.headers.get("Retry-After"), _WORKABLE_WALL_CLOCK())
            if delay > 4 or _WORKABLE_CLOCK() + delay >= deadline:
                return None
            _WORKABLE_SLEEP(delay)
            continue
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except (ValueError, TypeError):
            return None
    return None


def _html_piece(value: object) -> bool:
    return isinstance(value, str) and bool(re.search(r"<[a-z][^>]*>", value, re.I))


def _api_incomplete(value: object) -> bool:
    """An API's own partial-content evidence cannot become a complete HTML witness."""
    if isinstance(value, list):
        return any(_api_incomplete(item) for item in value)
    if not isinstance(value, dict):
        return False
    for raw_key, field in value.items():
        if not isinstance(raw_key, str):
            continue
        key = raw_key.replace("_", "").replace("-", "").lower()
        if key in {"truncated", "istruncated", "partial", "ispartial",
                   "incomplete", "isincomplete", "hasmore", "hasnext",
                   "hasnextpage", "hasmorepages", "hasmoresections"} and field:
            return True
        if key in {"complete", "iscomplete", "responsecomplete", "sectionscomplete",
                   "descriptioncomplete", "contentcomplete"} and field is False:
            return True
        if key in {"pagination", "paging", "pageinfo", "nextpage", "nextcursor",
                   "nextpagetoken", "continuationtoken", "missingsections",
                   "missingrequiredsections", "omittedsections"} and field:
            return True
    required = value.get("requiredSections", value.get("required_sections"))
    if isinstance(required, list) and required:
        sections = value.get("sections")
        if not isinstance(sections, dict):
            return True
        for name in required:
            section = sections.get(name) if isinstance(name, str) else None
            content = section.get("text") if isinstance(section, dict) else section
            if not _html_piece(content):
                return True
    return any(_api_incomplete(item) for item in value.values())


def _api_meta(url: str, title: str, fragments: list[str], *, company: object = None) -> dict | None:
    """Accept only complete native HTML; the normal guard owns all safety decisions."""
    if not title or not fragments or not all(_html_piece(x) for x in fragments):
        return None
    text = "\n".join(_text(fragment) for fragment in fragments if fragment.strip())
    if not text.strip():
        return None
    result = _guarded(title, text, html_variants=fragments, source_required=True,
                      source_complete=True)
    result.update(source_kind="ats_html", source_url=url)
    if employer := _employer_name(company):
        result["company"] = employer
    if (not pg.valid(result["text"], result["posting_guard"])
            or result["posting_guard"].get("source_verified") is not True
            or "source_html_unavailable" in result["posting_guard"].get("reasons", [])
            or not matched_source_witness({"jd_url": url}, result)):
        return None
    return result


def _workable_candidate_matches(candidate: object, account: str, code: str) -> bool:
    """A supplied link may be the account form or Workable's tenantless shortlink."""
    try:
        p = urlsplit(str(candidate))
        if p.scheme != "https" or p.hostname != "apply.workable.com":
            return False
        path = p.path.rstrip("/")
        return path in {f"/{account}/j/{code}", f"/j/{code}"}
    except ValueError:
        return False


def _from_new_ats(url: str) -> dict | None:
    parsed = _api_url(url)
    if parsed is None:
        return None
    vendor, original, external, endpoint, identity = parsed
    if vendor == "workable":
        payload = _workable_request(endpoint)
    else:
        try:
            with httpx.Client(timeout=8, follow_redirects=False) as client:
                response = client.get(endpoint)
            if response.status_code != 200:
                return None
            payload = response.json() if vendor != "personio" else response.text
        except (httpx.HTTPError, ValueError, TypeError):
            return None
    try:
        # The native detail/widget must not declare missing, truncated, or paginated
        # role material before its HTML can be marked as verified source.
        if vendor != "personio" and _api_incomplete(payload):
            return None
        if vendor == "workday":
            info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
            if not isinstance(info, dict) or info.get("externalPath") != external:
                return None
            body = info.get("jobDescription")
            fragments = [body] if isinstance(body, str) else []
            return _api_meta(original, str(info.get("title") or ""), fragments,
                             company=info.get("hiringOrganization") or info.get("companyName"))
        if vendor == "workable":
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            matches = [j for j in jobs or [] if isinstance(j, dict)
                       and str(j.get("shortcode") or "") == identity["code"]]
            if len(matches) != 1:
                return None
            job = matches[0]
            for candidate in (job.get("url"), job.get("application_url"), job.get("shortlink")):
                if candidate and not _workable_candidate_matches(
                        candidate, identity["account"], identity["code"]):
                    return None
            description = job.get("description")
            return _api_meta(original, str(job.get("title") or ""),
                             [description] if isinstance(description, str) else [],
                             company=job.get("company"))
        if vendor == "eightfold":
            if not isinstance(payload, dict) or str(payload.get("id") or "") != identity["id"]:
                return None
            canonical = payload.get("canonicalPositionUrl")
            if canonical:
                p = urlsplit(str(canonical))
                expected_host = f"{identity['tenant']}.eightfold.ai"
                expected_path = f"/careers/job/{identity['id']}"
                if (p.scheme != "https" or p.hostname != expected_host
                        or p.path.rstrip("/") != expected_path):
                    return None
            description = payload.get("job_description")
            return _api_meta(original, str(payload.get("title") or ""),
                             [description] if isinstance(description, str) else [],
                             company=payload.get("hiringOrganization"))
        if vendor == "personio":
            root = ET.fromstring(payload or "")
            if root.tag != "workzag-jobs":
                return None
            positions = root.findall("position")
            matches = [p for p in positions
                       if (p.get("id") or p.findtext("id") or "").strip() == identity["id"]]
            if len(matches) != 1:
                return None
            position = matches[0]
            # A flat <description> may be a shared intro; without the declared
            # jobDescriptions list its completeness is unobservable, so miss.
            sections = position.findall(".//jobDescriptions/*")
            fragments = [x.text if x.text and x.text.strip() else
                         next((child.text for child in x if child.text), "")
                         for x in sections]
            return _api_meta(original, position.findtext("name") or "", fragments)
        if vendor == "smartrecruiters":
            if (not isinstance(payload, dict) or str(payload.get("id") or "") != identity["id"]):
                return None
            company = payload.get("company")
            if (isinstance(company, dict)
                    and company.get("identifier") not in (None, identity["company"])):
                return None
            canonical = payload.get("postingUrl")
            if not canonical:
                return None
            p = urlsplit(str(canonical))
            if (p.scheme != "https" or p.hostname != "jobs.smartrecruiters.com"
                    or p.path.strip("/").split("/")[0] != identity["company"]
                    or p.path.strip("/").split("/")[1].split("-")[0] != identity["id"]):
                return None
            sections = payload.get("jobAd", {}).get("sections", {})
            required = sections.get("jobDescription", {}).get("text")
            if not _html_piece(required):
                return None
            fragments = [required]
            for key in ("qualifications", "additionalInformation"):
                value = sections.get(key, {}).get("text")
                if value:
                    fragments.append(value)
            return _api_meta(original, str(payload.get("name") or ""), fragments,
                             company=company if isinstance(company, dict) else None)
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, ET.ParseError):
        return None
    return None


def _text(html: str) -> str:
    """Visible text, near enough for a language model to read."""
    html = _TAG_RX.sub(" ", html)
    html = re.sub(r"<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
                .replace("&quot;", '"'))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _guarded(title: str, text: str, *, html_variants=(),
             source_required: bool = False, normalize_terminal: bool = False,
             source_complete: bool = True) -> dict:
    # Source-aware ATS/HTTP metadata has always been normalized. Keep the
    # normalization inside the analyzer so its generation binds exact bytes.
    inspected = pg.analyze(text, html_variants=html_variants,
                           source_required=source_required,
                           strip_trailing=source_required or normalize_terminal,
                           source_complete=source_complete)
    cleaned_title = pg.analyze(title)["text"][:90]
    return {"title": cleaned_title, **inspected}


def _valid_reader_url(value: object) -> bool:
    """Only complete, non-ambiguous HTTP(S) addresses may reach a reader."""
    if (not isinstance(value, str) or not value
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127
                   or ch == "\\" for ch in value)):
        return False
    try:
        p = urlsplit(value)
        return (p.scheme in {"http", "https"} and bool(p.netloc)
                and bool(p.hostname) and p.username is None
                and p.password is None and p.port != 0
                and not p.hostname.endswith(".")
                and not any(ch in p.hostname for ch in "%<>^`{|}"))
    except ValueError:
        return False


class _JobHTMLIdentity(HTMLParser):
    """Capture only HTML-authored canonical and JobPosting JSON-LD witnesses."""

    def __init__(self) -> None:
        super().__init__()
        self.canonicals: list[str] = []
        self.structured: list[str] = []
        self._ld: list[str] | None = None
        self.meta: dict[str, str] = {}
        self.headings: list[tuple[str, str, str]] = []
        self._heading: list[str] | None = None
        self._heading_tag = ""
        self._heading_hint = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "link" and "canonical" in (fields.get("rel") or "").lower().split():
            self.canonicals.append(fields.get("href") or "")
        if tag == "script" and (fields.get("type") or "").lower() == "application/ld+json":
            self._ld = []
        if tag == "meta":
            key = (fields.get("property") or fields.get("name") or "").lower()
            if key in {"og:title", "og:site_name", "og:type"}:
                self.meta[key] = fields.get("content") or ""
        if tag in {"h1", "h2", "h3"} and self._heading is None:
            self._heading = []
            self._heading_tag = tag
            self._heading_hint = (fields.get("class") or "") + " " + (fields.get("id") or "")

    def handle_data(self, data: str) -> None:
        if self._ld is not None:
            self._ld.append(data)
        if self._heading is not None:
            self._heading.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld is not None:
            self.structured.append("".join(self._ld))
            self._ld = None
        if tag == self._heading_tag and self._heading is not None:
            self.headings.append((tag, unescape("".join(self._heading)).strip(),
                                  self._heading_hint))
            self._heading = None
            self._heading_tag = ""
            self._heading_hint = ""



# These are hosting/generic labels, not source-evidenced employers.
_GENERIC_EMPLOYERS = {
    "company", "unknown", "jobs", "job", "careers", "apply", "recruiting",
    "greenhouse", "ashby", "ashbyhq", "lever", "linkedin", "workday",
    "myworkdayjobs", "workable", "smartrecruiters", "personio", "breezy",
    "homerun", "breezy hr", "homerun jobs", "phenom", "phenom people",
    "eightfold", "teamtailor", "jobvite",
}
_GENERIC_HEADING = re.compile(
    r"^(?:welcome|join (?:our|the) team|build your future|"
    r"discover (?:your|our)|explore (?:our|your)|"
    r"careers(?: at .*)?|jobs(?: at .*)?|work with us|"
    r"about (?:us|our team)|our (?:culture|mission)|job openings)(?:\b|$)", re.I,
)
_ROLE_HINT = re.compile(
    r"(?:^|[\s_-])(?:job|posting|position|role|vacancy)[\s_-]*title"
    r"|(?:^|[\s_-])title[\s_-]*(?:job|posting|position|role)(?:$|[\s_-])",
    re.I,
)


def _employer_name(value: object) -> str:
    """Sanitize a human-readable company, excluding common provider labels."""
    if isinstance(value, dict):
        value = value.get("name")
    if not isinstance(value, str):
        return ""
    name = " ".join(unescape(re.sub(r"<[^>]*>", "", value)).split()).strip(" #")
    if (not name or len(name) > 120 or "#" in name
            or name.casefold() in _GENERIC_EMPLOYERS
            or re.search(r"(?:^|[\s_-])(?:careers|jobs|recruiting)$", name, re.I)):
        return ""
    return pg.analyze(name)["text"][:120].strip()


def _posting_page_metadata(html: str, url: str) -> dict:
    """JobPosting, then job-specific OG, then role heading on this HTTP page."""
    parser = _JobHTMLIdentity()
    parser.feed(html)
    parser.close()
    entries: list[dict] = []
    for raw in parser.structured:
        try:
            queue = [json.loads(raw)]
        except (TypeError, ValueError):
            continue
        while queue:
            item = queue.pop()
            if isinstance(item, list):
                queue.extend(item)
            elif isinstance(item, dict):
                graph = item.get("@graph")
                if isinstance(graph, list):
                    queue.extend(graph)
                kind = item.get("@type", "")
                if "JobPosting" in (kind if isinstance(kind, list) else [kind]):
                    entries.append(item)

    headings = [(tag, " ".join(label.split()), hint)
                for tag, label, hint in parser.headings
                if label and len(label) <= 120 and not _GENERIC_HEADING.match(label)]
    # A hero or site banner is not a role heading even when its slogan is new.
    banner = re.compile(r"(?:^|[\s_-])(?:hero|banner|masthead|site-header)(?:$|[\s_-])", re.I)
    marked = [label for _, label, hint in headings if _ROLE_HINT.search(hint)]
    candidates = [(tag, label) for tag, label, hint in headings
                  if not banner.search(hint)]
    heading = (marked[0] if marked else
               next((label for tag, label in candidates if tag == "h1"), "")
               or next((label for tag, label in candidates if tag == "h2"), "")
               or (candidates[0][1] if candidates else ""))

    matching = [item for item in entries if isinstance(item.get("url"), str)
                and _same_posting_url(url, item["url"])]
    # An unbound recommendation widget can be the page's sole JobPosting.
    # Corroborate its role identity with the visible posting heading first.
    unbound = [item for item in entries if not item.get("url")]
    candidate = unbound[0] if len(entries) == 1 and unbound else {}
    json_candidate = candidate.get("title") if isinstance(candidate.get("title"), str) else ""
    normalized_heading = re.sub(r"[\W_]+", " ", heading.casefold()).strip()
    normalized_json = re.sub(r"[\W_]+", " ", json_candidate.casefold()).strip()
    same_role = bool(normalized_heading and normalized_json
                     and (normalized_json == normalized_heading
                          or (normalized_json.endswith(" " + normalized_heading)
                              and all(word in {"senior", "junior", "staff", "principal",
                                               "lead", "sr", "jr"}
                                      for word in normalized_json[:-len(normalized_heading)].split()))))
    selected = matching if matching else ([candidate] if same_role else [])
    posting = selected[0] if len(selected) == 1 else {}
    json_title = posting.get("title") if isinstance(posting.get("title"), str) else ""
    company = _employer_name(posting.get("hiringOrganization"))

    og_title = " ".join(unescape(parser.meta.get("og:title", "")).split())
    og_kind = parser.meta.get("og:type", "").lower()
    # OG site branding and hero slogans alone don't prove a posting.
    title_match = bool(heading and heading.casefold() in og_title.casefold())
    og_specific = bool(og_title and not _GENERIC_HEADING.match(og_title)
                       and (title_match or og_kind in {"job", "jobposting"}))
    # A shared ATS host's site_name brands the provider, not the hiring employer.
    # Reuse the existing generic-provider vocabulary, not a site-specific blocklist.
    host = urlsplit(url).hostname or ""
    host_brand = _OFFLINE_TLD(host).domain.casefold()
    if not company and og_specific and host_brand not in _GENERIC_EMPLOYERS:
        company = _employer_name(parser.meta.get("og:site_name"))
    # A vendor/site suffix in OG title should not be stored as the role name.
    role_og = og_title if og_specific and og_title.casefold() != heading.casefold() else ""
    if role_og and heading and role_og.casefold().startswith(heading.casefold()):
        suffix = role_og[len(heading):]
        if re.match(r"^\s*[|–—-]\s*", suffix):
            role_og = heading
    return {"title": json_title or role_og or heading, "company": company}


def _html_source_identity(html: str) -> dict:
    """Independent job ID and posting URL from fetched HTML, never a model echo."""
    try:
        parser = _JobHTMLIdentity()
        parser.feed(html)
        parser.close()
        canonical = set(parser.canonicals)
        ids: set[str] = set()
        posting_urls: set[str] = set()
        for raw in parser.structured:
            document = json.loads(raw)
            queue = [document]
            while queue:
                item = queue.pop()
                if isinstance(item, list):
                    queue.extend(item)
                    continue
                if not isinstance(item, dict):
                    continue
                queue.extend(item.get("@graph", []) if isinstance(item.get("@graph"), list) else [])
                kind = item.get("@type", "")
                kinds = kind if isinstance(kind, list) else [kind]
                if "JobPosting" not in kinds:
                    continue
                identifier = item.get("identifier")
                if isinstance(identifier, dict):
                    identifier = identifier.get("value")
                if isinstance(identifier, (str, int)) and str(identifier):
                    ids.add(str(identifier))
                if isinstance(item.get("url"), str):
                    posting_urls.add(item["url"])
        if (len(canonical) != 1 or len(ids) != 1 or len(posting_urls) != 1
                or canonical != posting_urls):
            return {}
        url = next(iter(canonical))
        return {"canonical_url": url, "job_id": next(iter(ids))} if _valid_reader_url(url) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _same_posting_url(expected: str, effective: str) -> bool:
    """A redirect proves original HTML only for the same posting identity."""
    try:
        a, b = urlsplit(expected), urlsplit(effective)
        if (a.scheme not in {"http", "https"} or b.scheme not in {"http", "https"}
                or not a.hostname or not b.hostname or a.hostname != b.hostname
                or a.username or b.username or a.password or b.password):
            return False
        same_scheme = a.scheme == b.scheme and a.port == b.port
        upgraded = (a.scheme == "http" and b.scheme == "https"
                    and a.port in (None, 80) and b.port in (None, 443))
        return (same_scheme or upgraded) and (
            (a.path.rstrip("/") or "/") == (b.path.rstrip("/") or "/")
            and a.query == b.query)
    except ValueError:
        return False

def matched_source_witness(row: dict, meta: dict) -> bool:
    """Only the same posting's inspected source can clear missing-HTML evidence.

    Network-directory Career Ops rows need the original ATS API HTML, not a
    flattened posting webpage; ordinary feed/crawler HTTP pages are valid
    witnesses when their fetched HTML belongs to the exact tracked URL.
    """
    guard = meta.get("posting_guard")
    kind = meta.get("source_kind")
    if not isinstance(guard, dict) or guard.get("source_verified") is not True:
        return False
    original = row.get("jd_url") or ""
    if not _valid_reader_url(original):
        # The operational URL is not a historical source witness. Require a
        # *separate* canonical and job ID parsed from actual HTTP HTML, plus
        # exact effective-response identity and the original persisted job ID.
        target = meta.get("reader_effective_url") or ""
        identity = meta.get("source_identity")
        return (kind == "http_html"
                and row.get("discovery_source") != "career_ops"
                and isinstance(identity, dict)
                and bool(row.get("job_id"))
                and identity.get("job_id") == str(row["job_id"])
                and _valid_reader_url(target)
                and _same_posting_url(target, identity.get("canonical_url") or "")
                and _same_posting_url(target, meta.get("source_url") or ""))
    return (_same_posting_url(original, meta.get("source_url") or "")
            and (kind == "ats_html"
                 or (kind == "http_html"
                     and row.get("discovery_source") != "career_ops")))


def _from_ats(url: str) -> dict | None:
    """Match an exact ATS posting and inspect all exposed HTML variants."""
    from urllib.parse import urlparse

    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if (u.scheme != "https" or u.username or u.password
                or u.port not in (None, 443)):
            return None
    except ValueError:
        return None
    ashby = host == "jobs.ashbyhq.com"
    greenhouse = host in {"boards.greenhouse.io", "job-boards.greenhouse.io",
                          "job-boards.eu.greenhouse.io"}
    lever = host in {"jobs.lever.co", "jobs.eu.lever.co"}
    if not (ashby or greenhouse or lever):
        return None

    seg = [part for part in u.path.split("/") if part]
    if ((greenhouse and (len(seg) != 3 or seg[1] != "jobs"))
            or (not greenhouse and len(seg) != 2)):
        return None
    org, job_id = seg[0], seg[-1]
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            if ashby:
                from discovery.adapters.ashby import ashby_jd_text, ashby_location_metadata

                r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}")
                r.raise_for_status()
                for j in r.json().get("jobs", []):
                    target_raw = str(j.get("jobUrl") or "")
                    target = urlparse(target_raw)
                    target_seg = [part for part in target.path.split("/") if part]
                    if target_raw and (target.scheme != "https"
                                       or target.hostname != host
                                       or len(target_seg) != 2
                                       or target_seg[0] != org):
                        continue
                    if (job_id == str(j.get("id", "")) or
                            (target_seg and target_seg[-1] == job_id)):
                        html = j.get("descriptionHtml")
                        description = str(j.get("descriptionPlain") or html or "")
                        metadata = ashby_location_metadata(j)
                        result = _guarded(
                            str(j.get("title", "")),
                            ashby_jd_text(j, description),
                            html_variants=[html] if html else [],
                            source_required=True)
                        # Internal reconciliation fields are removed before
                        # any fetch_jd_meta result reaches its caller.
                        result.update(source_kind="ats_html", source_url=url,
                                      _ashby_metadata=metadata,
                                      _ashby_body_length=len(pg.analyze(description)["text"]))
                        return result
            elif greenhouse:
                r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{org}/jobs/{job_id}",
                          params={"content": "true"})
                r.raise_for_status()
                j = r.json()
                if not isinstance(j, dict) or (
                        j.get("id") is not None and str(j["id"]) != job_id):
                    return None
                html = j.get("content")
                result = _guarded(str(j.get("title", "")), str(html or ""),
                                  html_variants=[html] if html else [],
                                  source_required=True)
                result.update(source_kind="ats_html", source_url=url)
                return result
            elif lever:
                api_host = "api.eu.lever.co" if host == "jobs.eu.lever.co" else "api.lever.co"
                r = c.get(f"https://{api_host}/v0/postings/{org}/{job_id}")
                r.raise_for_status()
                j = r.json()
                hosted = str(j.get("hostedUrl") or "")
                hosted_parts = urlparse(hosted)
                if ((j.get("id") is not None and str(j["id"]) != job_id)
                        or (hosted and (
                            hosted_parts.scheme != "https"
                            or hosted_parts.hostname != host
                            or hosted_parts.path.rstrip("/") != f"/{org}/{job_id}"))):
                    return None
                html = j.get("description")
                variants = [html] if html else []
                # List fragments remain diagnostic, but cannot substitute for
                # Lever's primary description HTML as source completeness proof.
                for section in j.get("lists") or []:
                    if isinstance(section, dict) and section.get("content"):
                        variants.append(section["content"])
                result = _guarded(str(j.get("text", "")),
                                  str(j.get("descriptionPlain") or html or ""),
                                  html_variants=variants, source_required=True,
                                  source_complete=bool(html))
                result.update(source_kind="ats_html", source_url=url)
                return result
    except Exception as exc:  # noqa: BLE001 — fail closed to other readers
        log.debug("board API miss for %s: %s", url, exc)
    return None


def _ashby_selected_text(text: str, ats: dict | None, url: str) -> str:
    """Reconcile metadata only from the URL-matched Ashby API posting."""
    if (not ats or ats.get("source_kind") != "ats_html" or
            ats.get("source_url") != url or
            not pg.valid(ats.get("text", ""), ats.get("posting_guard"))):
        return text
    metadata = ats.get("_ashby_metadata")
    if not isinstance(metadata, str) or not metadata:
        return text
    return metadata + "\n" + text


def _public_ats(meta: dict | None) -> dict | None:
    if meta is not None:
        meta.pop("_ashby_metadata", None)
        meta.pop("_ashby_body_length", None)
    return meta


def fetch_jd_meta(url: str, kind: str = "jd") -> dict:
    """Guarded title, text, posting_guard and provenance for a posting."""
    if not _valid_reader_url(url):
        return _guarded("", "", source_required=True)
    api_ats = _from_new_ats(url)
    if api_ats is not None:
        return api_ats
    ats = _from_ats(url)
    if ats and ats.get("_ashby_body_length", len(ats["text"])) > 200:
        return _public_ats(ats)
    from discovery.resolver import BROWSER_HEADERS

    html_read = _get(url, BROWSER_HEADERS)
    if html_read is None:
        html_read = _get(url, {})
    if html_read is None:
        try:
            seen = _from_chrome(url, kind)
        except PostingReadUnavailable:
            # A newly flagged short ATS read must not be dropped just
            # because the slower fallback failed to fetch a longer JD.
            if ats:
                return _public_ats(ats)
            raise
        if seen is None:
            return _public_ats(ats) or _guarded("", "", source_required=True)
        if ats:
            selected = _ashby_selected_text(seen["text"], ats, url)
            merged = pg.merge(selected, seen["posting_guard"], ats["posting_guard"])
            seen.update(merged)
        return seen

    # _get supplies the effective URL after redirects. Legacy synthetic
    # fetch stubs returning only HTML represent an exact URL response.
    html, effective_url = (html_read if isinstance(html_read, tuple)
                           else (html_read, url))
    same_source = _same_posting_url(url, effective_url)
    posting = _posting_page_metadata(html, url) if same_source else {}
    hit = _guarded(posting.get("title", ""), html, html_variants=[html], source_required=True,
                   source_complete=same_source)
    # A redirect to a different posting is not original-HTML evidence even
    # when that page returns HTTP 200 and its visible text looks clean.
    hit.update(source_kind="http_html", source_url=effective_url,
               source_identity=_html_source_identity(html) if same_source else {})
    if posting.get("company"):
        hit["company"] = posting["company"]
    if len(hit["text"]) < 400:
        try:
            seen = _from_chrome(url, kind)
        except PostingReadUnavailable:
            # Workday and Workable answer HTTP with a near-empty script shell.
            # Without an ATS read, a browser outage here is an outage, exactly
            # as when HTTP fails above, not an unreadable posting (no_jd).
            if not ats:
                raise
            seen = None
        if seen and len(seen.get("text", "")) > len(hit["text"]):
            combined = pg.merge(seen["text"], seen["posting_guard"],
                                hit["posting_guard"])
            seen.update(combined)
            # Chrome supplies longer text, not independent original-job identity.
            # Retain only the canonical/JobPosting proof parsed from the HTTP HTML.
            hit = {**seen, "source_kind": "http_html",
                   "source_url": effective_url,
                   "source_identity": hit["source_identity"] if same_source else {},
                   "title": hit["title"] or seen.get("title", "")}
            if same_source and posting.get("company"):
                hit["company"] = posting["company"]
    if ats:
        complete_ats = bool(ats["posting_guard"].get("source_verified"))
        # Never substitute another redirected page's JD for an ATS posting.
        selected_text = (_ashby_selected_text(hit["text"], ats, url)
                         if same_source else ats["text"])
        combined = pg.merge(selected_text, hit["posting_guard"],
                            ats["posting_guard"], source_witness=complete_ats)
        hit.update(combined)
        if complete_ats and ats.get("source_url") == url:
            hit.update(source_kind="ats_html", source_url=url)
            if not same_source:
                hit.pop("company", None)
            if ats.get("company"):
                hit["company"] = ats["company"]
    return hit

def _get(url: str, headers: dict) -> tuple[str, str] | None:
    """The page, or None when the server refused or the request failed.

    A non-2xx response used to be returned as if it were the posting. Meta's
    400 page — "Sorry, something went wrong" — came back as 126 characters of
    job description, nothing downstream objected, and the tailor would have
    written a résumé against it. A refusal is not a page.
    """
    if not _valid_reader_url(url):
        return None
    try:
        with httpx.Client(headers=headers, follow_redirects=True, timeout=25) as client:
            r = client.get(url)
    except httpx.HTTPError as exc:
        log.error("JD fetch failed for %s: %s", url, exc)
        return None
    if not 200 <= r.status_code < 300:
        log.warning("JD fetch refused for %s: HTTP %s", url, r.status_code)
        return None
    return r.text, str(r.url)


def read_postings(urls: list[str], *, batch: int = 6, kind: str = "jd_sweep",
                  with_gone: bool = False, with_meta: bool = False):
    """Read several browser-only postings per session. {url: description}.

    Meta's postings cannot be read over HTTP at all, so each needs a real browser.
    Read one per session that cost ~5 minutes × 55 rows, serialised, each session
    waiting on any application in flight. The cost is session start-up, not page
    reads, so one session reads a handful.

    Two rules keep a batch honest. An entry is accepted only if its URL is one we
    asked for: a session that attributes one posting's text to another's URL is
    worse than one that fails, and it is the failure mode a batch invites. And a
    batch that comes back malformed loses only itself — the sweep carries on.
    """
    from tools.browser_runtime import TAB_HYGIENE

    urls = [url for url in urls if _valid_reader_url(url)]
    if not urls:
        return ({}, set()) if with_gone else {}
    _check_browser_reader()
    ok, why = available()
    if not ok:
        raise PostingReadUnavailable(why)

    out: dict = {}
    gone: set[str] = set()
    for i in range(0, len(urls), batch):
        chunk = urls[i:i + batch]
        listing = "\n".join(f"- {u}" for u in chunk)
        task = (f"Read these {len(chunk)} job postings, one at a time. For each: open the "
                f"URL, wait for it to load, read the whole posting — responsibilities, "
                f"requirements, everything — then close its tab.\n\n{listing}\n\n"
                f"{TAB_HYGIENE}\n"
                "Write this JSON to the file you are told about and repeat it in your "
                "reply, with one entry per URL EXACTLY as given above. If a posting has "
                "been removed — the page says the job is no longer available, or "
                "redirects to a not-available page — set \"gone\": true for it and "
                "leave its description empty:\n"
                '{"postings": [{"url": "<the url, verbatim>", "title": "<role title>", '
                '"gone": false, "description": "<the full posting text, verbatim>"}]}')
        try:
            report, problem = _run(run_task(task, report_key="postings",
                                            timeout_s=120 * len(chunk), kind=kind, urls=chunk))
        except Exception as exc:  # noqa: BLE001 — one bad batch must not stop the sweep
            log.warning("posting batch failed: %s", exc)
            continue
        if problem or not isinstance(report.get("postings"), list):
            log.warning("posting batch returned nothing usable: %s", problem or "no list")
            if problem:
                _note_browser_problem(problem)
                if _browser_retry[0] > time.monotonic():
                    break  # Keep earlier results, and defer unread rows below.
            continue
        wanted = set(chunk)
        for entry in report["postings"]:
            if not isinstance(entry, dict):
                continue
            u = str(entry.get("url") or "").strip()
            text = str(entry.get("description") or "").strip()
            if u not in wanted:
                log.warning("posting batch returned a URL that was not asked for: %s", u[:80])
                continue
            if entry.get("gone") is True or _GONE_RX.search(text[:400]):
                gone.add(u)
                continue
            if len(text) < 400:
                continue
            guarded = _guarded(str(entry.get("title") or ""), text)
            out[u] = guarded if with_meta else guarded["text"]
    return (out, gone) if with_gone else out


_GONE_RX = re.compile(r"no longer available|position[- ]not[- ]available|has been removed|"
                      r"job (?:posting )?(?:is )?closed", re.I)


def run_task(*args, **kwargs):
    from tools.browser_runtime import run_task as _rt

    return _rt(*args, **kwargs)


def available():
    from tools.browser_runtime import available as _av

    return _av()


def _run(coro):
    import asyncio

    try:
        return asyncio.run(coro)
    except RuntimeError:  # already inside a loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro)).result()


def _from_chrome(url: str, kind: str = "jd") -> dict | None:
    """Read the posting in the owner's own browser. Last resort, and slow."""
    import asyncio

    from tools.browser_runtime import available, configuration, run_task

    if not _valid_reader_url(url):
        return None
    _check_browser_reader()
    ok, why = available()
    if not ok:
        raise PostingReadUnavailable(why)
    from tools.browser_runtime import TAB_HYGIENE

    task = (f"Open {url}, wait for it to load, and read the job posting.\n\n"
            f"{TAB_HYGIENE}\n"
            "Close the tab, then write this JSON to the file you are told about "
            "and repeat it in your reply:\n"
            '{"title": "<the role title>", "description": "<the full posting text, '
            'verbatim: responsibilities, requirements, everything>"}')
    try:
        report, problem = asyncio.run(
            run_task(task, report_key="description", timeout_s=300, kind=kind, urls=[url]))
    except RuntimeError:  # already inside a loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            report, problem = pool.submit(
                lambda: asyncio.run(run_task(task, report_key="description",
                                             timeout_s=300, kind=kind, urls=[url]))).result()
    if problem or not report:
        log.warning("could not read %s in the browser: %s", url, problem)
        why = problem or "The browser reader returned no posting report. Try reading it again."
        _note_browser_problem(why)
        raise PostingReadUnavailable(why)
    # BrowserSkill's ordinary reader is normalized; the older Claude Chrome
    # string-return reader preserves harmless terminal spaces. Both are guarded
    # before their distinct text contracts are returned.
    return _guarded(str(report.get("title", "")), str(report.get("description", "")),
                    normalize_terminal=configuration()["engine"] == "browser_skill")


def fetch_jd(url: str, kind: str = "jd") -> str:
    """Just the text. `kind` says whether a browser read may wait for a live
    application ("jd_sweep") or is part of one ("jd")."""
    return fetch_jd_meta(url, kind=kind).get("text", "")
