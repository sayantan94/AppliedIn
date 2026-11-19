"""Default-off, local-only funding lead ingestion.

Nothing in this module submits, tracks or enqueues an application. Untrusted news,
SEC and board content is kept out of the effective watchlist unless the
independently established employer->native board->current entry-guard chain
passes. The local YAML is the only durable funding state.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx
import yaml

from core.config import get_settings

_ALLOWED_STATE = frozenset({"version", "feeds", "articles", "deals", "active", "pending",
                            "unresolved", "budgets", "report", "next_poll"})
_ALLOWED_AUTO = frozenset({"name", "careers_url", "ats", "board", "mode", "discovery",
                           "employer_domain", "aliases", "homepage", "source_id",
                           "admitted_at", "last_relevant_at"})
_BOARD_HOST = {"ashby": "jobs.ashbyhq.com", "lever": "jobs.lever.co"}
_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")
_FUNDING = re.compile(r"\b(?:funding|financing|fundraise|raised|raises|secured|pre.seed|seed|series a)\b", re.I)
_STAGE = {"pre-seed": re.compile(r"\bpre[- ]seed\b", re.I),
          "seed": re.compile(r"(?<!pre[- ])\bseed\b", re.I),
          "series a": re.compile(r"\bseries\s+a\b", re.I)}
_SECTOR = re.compile(r"\b(?:fintech|payments?|financial technology|artificial intelligence|"
                     r"generative ai|machine learning|llms?|ai[- /]powered|ai startup)\b", re.I)
_BLOCKED_SECTOR = re.compile(r"\b(?:venture fund|investment fund|fund manager|real estate fund)\b", re.I)
_REGIONS = {"eu", "uk", "usa", "uae", "gcc", "india", "singapore", "australia"}
_REGION_PHRASES = {
    "eu": ("europe", "european union", "france", "germany", "netherlands", "ireland",
           "spain", "italy", "sweden", "finland", "denmark", "portugal", "poland"),
    "uk": ("united kingdom", "britain", "london", "uk-based", "england"),
    "usa": ("united states", "u.s.", "usa", "american", "san francisco", "new york"),
    "uae": ("united arab emirates", "dubai", "abu dhabi", "uae"),
    "gcc": ("saudi arabia", "qatar", "kuwait", "bahrain", "oman", "gcc", "riyadh"),
    "india": ("india", "bengaluru", "mumbai", "delhi"),
    "singapore": ("singapore",),
    "australia": ("australia", "sydney", "melbourne"),
}
_REMOTE = re.compile(r"\bremote[- ]first\b", re.I)
_MAX_STATE_BYTES = 3_000_000


class FundingRefusal(ValueError):
    """Fail closed; caller may count the reason, never turn it into authority."""


def _utc(now=None) -> datetime:
    v = now or datetime.now(UTC)
    return v if v.tzinfo else v.replace(tzinfo=UTC)


def _day(now) -> str:
    return now.date().isoformat()


def _week(now) -> str:
    y, w, _ = now.isocalendar()
    return f"{y:04d}-W{w:02d}"


def _key(*values: object) -> str:
    return hashlib.sha256("|".join(str(v) for v in values).encode("utf-8")).hexdigest()[:32]


def _new_state() -> dict:
    return {"version": 1, "feeds": {}, "articles": {}, "deals": {}, "active": {},
            "pending": {}, "unresolved": {}, "budgets": {}, "report": {}, "next_poll": ""}


def _strict_native(name: str, url: str, ats: str, board: str) -> bool:
    if (not isinstance(name, str) or not name.strip() or len(name) > 150
            or not isinstance(ats, str) or ats not in _BOARD_HOST or not isinstance(board, str)
            or not _TENANT.fullmatch(board) or not isinstance(url, str)):
        return False
    return url.rstrip("/") == f"https://{_BOARD_HOST[ats]}/{board}"


def _auto_record(item: object) -> dict:
    if not isinstance(item, dict) or set(item) - _ALLOWED_AUTO:
        raise FundingRefusal("auto_record_unknown_fields")
    if (item.get("mode") != "gated" or item.get("discovery") != "feed"
            or not _strict_native(item.get("name"), item.get("careers_url"),
                                  item.get("ats"), item.get("board"))):
        raise FundingRefusal("auto_record_authority")
    aliases = item.get("aliases", [])
    if (not isinstance(aliases, list) or len(aliases) > 8
            or any(not isinstance(x, str) or not x.strip() or len(x) > 150
                   for x in aliases)):
        raise FundingRefusal("auto_record_aliases")
    if not isinstance(item.get("homepage"), str) or not item["homepage"].startswith("https://"):
        raise FundingRefusal("auto_record_homepage")
    domain = registrable(item["homepage"])
    if not domain or domain != item.get("employer_domain"):
        raise FundingRefusal("auto_record_domain")
    for field in ("source_id", "admitted_at", "last_relevant_at"):
        if not isinstance(item.get(field), str) or len(item[field]) > 160:
            raise FundingRefusal("auto_record_metadata")
    return item


def registrable(homepage: str) -> str | None:
    """PSL from the wheel's bundled snapshot; never fetch or guess a suffix."""
    try:
        from tldextract import TLDExtract
        p = urlsplit(homepage)
        if (p.scheme != "https" or not p.hostname or p.username or p.password
                or p.port not in (None, 443) or not p.hostname.isascii()):
            return None
        ext = TLDExtract(suffix_list_urls=(), cache_dir=None,
                         include_psl_private_domains=True)(p.hostname)
        if not ext.suffix or not ext.domain:
            return None
        return (ext.domain + "." + ext.suffix).casefold()
    except (ImportError, ValueError, TypeError, AttributeError):
        return None


def _validate_state(data: object) -> dict:
    if not isinstance(data, dict) or set(data) != _ALLOWED_STATE or data["version"] != 1:
        raise FundingRefusal("corrupt_funding_state")
    for field in ("feeds", "articles", "deals", "active", "pending", "unresolved",
                  "budgets", "report"):
        if not isinstance(data[field], dict):
            raise FundingRefusal("corrupt_funding_state")
    if not isinstance(data["next_poll"], str):
        raise FundingRefusal("corrupt_funding_state")
    for field in ("feeds", "articles", "deals", "pending", "unresolved"):
        if any(not isinstance(k, str) or len(k) > 200 or not isinstance(v, dict)
               for k, v in data[field].items()):
            raise FundingRefusal("corrupt_funding_state")
    for feed in data["feeds"].values():
        if any(not isinstance(feed.get(k, ""), str)
               for k in ("etag", "last_modified", "last_fetch")):
            raise FundingRefusal("corrupt_funding_state")
    for period in ("day", "week"):
        if not isinstance(data["budgets"].get(period, ""), str):
            raise FundingRefusal("corrupt_budget")
    if len(data["active"]) > 1200 or len(data["deals"]) > 5000:
        raise FundingRefusal("state_capacity")
    for entry in data["active"].values():
        _auto_record(entry)
    for k in ("day_calls", "day_http", "day_relevance", "day_added", "week_tokens"):
        v = data["budgets"].get(k, 0)
        if type(v) is not int or v < 0:
            raise FundingRefusal("corrupt_budget")
    return data


class State:
    """One validated private YAML, atomic replacement, and durable pre-call spend."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            if self.path.stat().st_size > _MAX_STATE_BYTES:
                raise FundingRefusal("corrupt_funding_state")
            try:
                self.data = _validate_state(yaml.safe_load(self.path.read_text("utf-8")))
            except (OSError, yaml.YAMLError, UnicodeError, ValueError) as exc:
                raise FundingRefusal("corrupt_funding_state") from exc
        else:
            self.data = _new_state()

    def save(self) -> None:
        _validate_state(self.data)
        payload = yaml.safe_dump(self.data, sort_keys=True, allow_unicode=False)
        if len(payload.encode("utf-8")) > _MAX_STATE_BYTES:
            raise FundingRefusal("state_capacity")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                              dir=self.path.parent, prefix=".funded-",
                                              delete=False) as f:
                tmp = Path(f.name)
                os.chmod(f.name, 0o600)
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if tmp and tmp.exists():
                tmp.unlink()

    def _period(self, now) -> None:
        b = self.data["budgets"]
        day, week = _day(now), _week(now)
        # Clock rollback cannot reset spent budgets.
        if b.get("day", "") > day or b.get("week", "") > week:
            raise FundingRefusal("clock_rollback")
        if b.get("day") != day:
            for name in ("day_calls", "day_http", "day_relevance", "day_added"):
                b[name] = 0
            b["day"] = day
        if b.get("week") != week:
            b["week_tokens"] = 0
            b["week"] = week

    def reserve(self, now, field: str, amount: int, limit: int) -> bool:
        self._period(now)
        if amount < 0 or limit < 0 or field not in {
            "day_calls", "day_http", "day_relevance", "day_added", "week_tokens"
        }:
            raise FundingRefusal("invalid_budget")
        b = self.data["budgets"]
        if b.get(field, 0) + amount > limit:
            self.save()
            return False
        b[field] = b.get(field, 0) + amount
        # Unknown usage, failed calls and crash consume their reservation.
        self.save()
        return True

    def count(self, source: str, reason: str, now) -> None:
        report = self.data["report"]
        if report.get("date") != _day(now):
            report.clear()
            report.update(date=_day(now), sources={}, counts={})
        report["counts"][reason] = report["counts"].get(reason, 0) + 1
        src = report["sources"].setdefault(source, {})
        src[reason] = src.get(reason, 0) + 1
        self.save()

    def compact(self, now, days: int = 60) -> None:
        cutoff = now - timedelta(days=max(30, min(90, days)))
        for field in ("deals", "articles", "unresolved"):
            for key, item in list(self.data[field].items()):
                if not isinstance(item, dict):
                    raise FundingRefusal("corrupt_funding_state")
                t = item.get("seen_at", "")
                if t:
                    try:
                        seen = datetime.fromisoformat(t)
                        # Pending companies and open budgets live elsewhere.
                        # Stale retryable/deferred tombstones cannot remain forever.
                        if _utc(seen) < cutoff:
                            del self.data[field][key]
                    except ValueError:
                        raise FundingRefusal("corrupt_funding_state")
        self.save()


def active_watchlist(settings=None) -> list[dict]:
    settings = settings or get_settings()
    if settings.mode != "local" or not settings.funding_enabled:
        return []
    state = State(Path(settings.local_dir) / "funded-watchlist.yaml")
    return list(state.data["active"].values())


def _safe_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 2048 or not url.isascii():
        raise FundingRefusal("unsafe_url")
    if any(ord(ch) <= 32 or ch == "\\" or ord(ch) == 127 for ch in url):
        raise FundingRefusal("unsafe_url")
    try:
        p = urlsplit(url)
        if (p.scheme != "https" or not p.hostname or not p.netloc
                or p.username is not None or p.password is not None or p.fragment
                or p.port not in (None, 443) or p.hostname.endswith(".")
                or not re.fullmatch(r"[A-Za-z0-9.-]+", p.hostname)
                or ".." in p.hostname or "." not in p.hostname):
            raise FundingRefusal("unsafe_url")
        return p.hostname.casefold(), p.path or "/"
    except ValueError as exc:
        raise FundingRefusal("unsafe_url") from exc


def _public_addresses(host: str, port: int) -> list[tuple]:
    """Resolve at CONNECT time; never trust preflight DNS on a different socket."""
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FundingRefusal("dns_failed") from exc
    if not addresses:
        raise FundingRefusal("dns_failed")
    for family, _, _, _, sockaddr in addresses:
        try:
            if family not in (socket.AF_INET, socket.AF_INET6) or not ipaddress.ip_address(
                    sockaddr[0]).is_global:
                raise FundingRefusal("nonpublic_connect_address")
        except ValueError as exc:
            raise FundingRefusal("invalid_connect_address") from exc
    return addresses[:8]


class _SafeTLS(http.client.HTTPSConnection):
    def connect(self):
        addresses = _public_addresses(self.host, self.port)
        last = None
        for family, socktype, proto, _, sockaddr in addresses:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(self.timeout)
                sock.connect(sockaddr)  # numeric approved address, no second DNS lookup
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except (OSError, ssl.SSLError) as exc:
                last = exc
                sock.close()
        raise FundingRefusal("tls_or_connect_failed") from last


class GuardedClient:
    """Minimal httpx-compatible GET surface for unchanged resolver and ATS adapters.

    No proxy, cookies, auth forwarding or alternate unguarded client. One
    guarded socket verifies every DNS result at the actual connection boundary
    and TLS continues to verify the original DNS hostname.
    """

    def __init__(self, *, state: State, settings, now=None, max_bytes=256_000):
        self.state, self.settings, self.now = state, settings, now
        self.max_bytes = min(max_bytes, 512_000)
        self.requests = 0
        self._sec_last = 0.0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, *, params=None, headers=None, timeout=12,
            follow_redirects=False):
        if params:
            p = urlsplit(url)
            query = p.query + ("&" if p.query else "") + urlencode(params)
            url = urlunsplit((p.scheme, p.netloc, p.path, query, ""))
        original = url
        allowed_headers = {}
        for name, val in (headers or {}).items():
            if name.lower() in {"if-none-match", "if-modified-since", "accept"}:
                if isinstance(val, str) and len(val) < 512 and "\n" not in val and "\r" not in val:
                    allowed_headers[name] = val
        for hop in range(4):
            host, path = _safe_url(url)
            self.requests += 1
            if self.requests > self.settings.funding_max_http_per_pass:
                raise FundingRefusal("http_pass_budget")
            now = self.now or _utc()
            if not self.state.reserve(now, "day_http", 1,
                                      self.settings.funding_daily_http):
                raise FundingRefusal("http_day_budget")
            if host == "www.sec.gov":
                agent = self.settings.funding_sec_user_agent
                if (not agent or len(agent) > 200 or
                        not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", agent)):
                    raise FundingRefusal("sec_user_agent_missing")
                # Conservative per-process rate, with no retry storm.
                delay = .5 - (time.monotonic() - self._sec_last)
                if delay > 0:
                    time.sleep(delay)
                self._sec_last = time.monotonic()
                agent_header = agent
            else:
                agent_header = "AppliedIn-Funding/1.0"
            p = urlsplit(url)
            conn = _SafeTLS(host, 443, timeout=min(15, max(1, float(timeout))),
                            context=ssl.create_default_context())
            try:
                h = {"User-Agent": agent_header, "Accept": "application/xml, application/json, text/html;q=0.8",
                     **allowed_headers}
                conn.request("GET", path + (("?" + p.query) if p.query else ""),
                             headers=h)
                response = conn.getresponse()
                status = response.status
                data = response.read(self.max_bytes + 1)
                if len(data) > self.max_bytes:
                    raise FundingRefusal("response_too_large")
                out_headers = {k: v for k, v in response.getheaders()
                               if k.lower() in {"location", "etag", "last-modified", "content-type"}}
            finally:
                conn.close()
            if 300 <= status < 400 and "location" in out_headers:
                if not follow_redirects or hop == 3:
                    raise FundingRefusal("redirect_not_authorized")
                url = urljoin(url, out_headers["location"])
                _safe_url(url)
                # Conditional headers, SEC identity, and all auth are host-scoped.
                if urlsplit(url).hostname != host:
                    allowed_headers.clear()
                continue
            return httpx.Response(status, content=data, headers=out_headers,
                                  request=httpx.Request("GET", url))
        raise FundingRefusal("too_many_redirects")


class _ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links = [], []
        self.href = ""
        self.anchor_text = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.ignored += 1
        if tag == "a" and not self.ignored:
            self.href = dict(attrs).get("href", "")
            self.anchor_text = []

    def handle_data(self, value):
        if self.ignored:
            return
        self.parts.append(value)
        if self.href:
            self.anchor_text.append(value)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.ignored = max(0, self.ignored - 1)
        if tag == "a" and self.href:
            self.links.append((" ".join(self.anchor_text).strip(), self.href))
            self.href = ""
            self.anchor_text = []

    @property
    def text(self):
        return " ".join(self.parts)


def _article(body: str) -> _ArticleHTML:
    p = _ArticleHTML()
    p.feed(body[:120_000])
    return p


def _xml(payload: bytes) -> ET.Element:
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise FundingRefusal("unsafe_xml")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FundingRefusal("invalid_xml") from exc


def _node_text(element, local: str) -> str:
    for node in element.iter():
        if node.tag.rsplit("}", 1)[-1] == local:
            return (node.text or "").strip()[:512]
    return ""


def _rss_items(payload: bytes, kind: str) -> list[dict]:
    root = _xml(payload)
    records = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] in
               ({"entry"} if kind == "atom" else {"item"})]
    out = []
    for item in records[:40]:
        title = _node_text(item, "title")
        date_raw = _node_text(item, "pubDate") or _node_text(item, "published") or _node_text(item, "updated")
        link = _node_text(item, "link")
        if not link:
            for node in item:
                if node.tag.rsplit("}", 1)[-1] == "link" and node.attrib.get("href"):
                    link = node.attrib["href"]
                    break
        guid = _node_text(item, "guid") or _node_text(item, "id") or link
        out.append({"title": title, "date": date_raw, "url": link, "key": _key(kind, guid)})
    return out


def _date(raw: str) -> datetime | None:
    if not isinstance(raw, str) or len(raw) > 100:
        return None
    try:
        from email.utils import parsedate_to_datetime
        result = (parsedate_to_datetime(raw) if "," in raw
                  else datetime.fromisoformat(raw.replace("Z", "+00:00")))
        return _utc(result)
    except (TypeError, ValueError, OverflowError):
        return None


def _fresh(raw: str, now: datetime, window_days: int) -> bool:
    d = _date(raw)
    return bool(d and now - timedelta(days=window_days) <= d <= now + timedelta(days=1))


def _regions(text: str, allowed: set[str]) -> set[str]:
    t = text.casefold()
    result = {r for r, phrases in _REGION_PHRASES.items()
              if r in allowed and any(re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", t)
                                      for v in phrases)}
    if not result and "remote-first-anywhere" in allowed and _REMOTE.search(t):
        result.add("remote-first-anywhere")
    return result


def _manifest(settings) -> dict:
    path = Path(settings.config_dir) / "funding-feeds.yaml"
    try:
        v = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FundingRefusal("invalid_source_configuration") from exc
    if not isinstance(v, dict) or set(v) != {"regions", "sources", "version"} or v["version"] != 1:
        raise FundingRefusal("invalid_source_configuration")
    regions = v["regions"]
    if (not isinstance(regions, list) or not regions or len(regions) > 12
            or any(not isinstance(x, str) or x not in _REGIONS | {"remote-first-anywhere"}
                   for x in regions) or len(set(regions)) != len(regions)):
        raise FundingRefusal("invalid_source_regions")
    ids = set()
    if not isinstance(v["sources"], list) or len(v["sources"]) > 40:
        raise FundingRefusal("invalid_source_configuration")
    for item in v["sources"]:
        if (not isinstance(item, dict) or set(item) != {"id", "kind", "region", "url", "enabled"}
                or not isinstance(item["kind"], str)
                or item["kind"] not in {"rss", "atom", "yc_json", "sec_atom"}
                or not isinstance(item["region"], str)
                or not isinstance(item["enabled"], bool) or not isinstance(item["id"], str)
                or not re.fullmatch(r"[a-z0-9_-]{2,40}", item["id"])
                or item["id"] in ids or item["region"] not in set(regions)):
            raise FundingRefusal("invalid_source_configuration")
        _safe_url(item["url"])
        ids.add(item["id"])
    return v


def ready(settings=None) -> bool:
    settings = settings or get_settings()
    if (settings.mode != "local" or not settings.funding_enabled
            or not 4 <= settings.funding_poll_hours <= 6
            or not 1 <= settings.funding_eligible_days <= 30
            or not 1 <= settings.funding_daily_extraction_calls <= 48
            or not 1000 <= settings.funding_weekly_extraction_tokens <= 2_000_000
            or not 100 <= settings.funding_extract_max_output_tokens <= 4096
            or not 0 <= settings.funding_daily_new_companies <= 20
            or not 1 <= settings.funding_daily_http <= 1000
            or not 1 <= settings.funding_max_http_per_pass <= 250
            or not 1 <= settings.funding_max_candidates_per_pass <= 100
            or not 1 <= settings.funding_max_postings_per_pass <= 50
            or not 1 <= settings.funding_daily_relevance_calls <= 100
            or not 1 <= settings.funding_recheck_days <= 14
            or not 7 <= settings.funding_pending_days <= 60
            or not 30 <= settings.funding_active_days <= 120
            or os.getenv("APPLIEDIN_DISCOVERY", "on").lower() in {"off", "false", "no", "0"}):
        return False
    try:
        return any(s["enabled"] for s in _manifest(settings)["sources"])
    except FundingRefusal:
        return False


def _source_payload(source: dict, state: State, client: GuardedClient, now) -> tuple[list[dict], str]:
    sid = source["id"]
    meta = state.data["feeds"].get(sid, {})
    # When a previous pass stopped mid-article, a 304 must not hide that
    # unfinished work on the next pass. Fetch an ordinary body for replay.
    needs_replay = any(isinstance(v, dict) and v.get("source") == sid
                       and v.get("status") in ("queued", "retryable", "processing")
                       for v in state.data["articles"].values())
    headers = {} if needs_replay else {
        key: value for key, value in (
            ("If-None-Match", meta.get("etag", "")),
            ("If-Modified-Since", meta.get("last_modified", ""))
        ) if value
    }
    if source["kind"] == "sec_atom" and not client.settings.funding_sec_user_agent:
        raise FundingRefusal("sec_user_agent_missing")
    resp = client.get(source["url"], headers=headers, follow_redirects=True)
    if resp.status_code == 304:
        return [], "not_modified"
    resp.raise_for_status()
    next_cursor = {
        "etag": resp.headers.get("etag", "")[:256],
        "last_modified": resp.headers.get("last-modified", "")[:128],
        "last_fetch": now.isoformat(),
    }
    if source["kind"] == "yc_json":
        # YC cohort membership, recent changes and hiring are enrichment only.
        try:
            v = resp.json()
            if not isinstance(v, (dict, list)):
                raise ValueError("wrong shape")
        except ValueError as exc:
            raise FundingRefusal("invalid_yc_json") from exc
        state.data["feeds"][sid] = next_cursor
        state.save()
        return [], "yc_not_round_evidence"
    kind = "atom" if source["kind"] in {"atom", "sec_atom"} else "rss"
    items = _rss_items(resp.content, kind)
    for item in items:
        item["key"] = _key(sid, item["key"])
        if item["key"] not in state.data["articles"]:
            state.data["articles"][item["key"]] = {
                "status": "queued", "source": sid, "seen_at": now.isoformat()}
    # Cursor and unacknowledged article keys must become durable in ONE
    # replacement. A crash cannot advance an ETag while leaving no replay
    # witness for an article not yet evaluated.
    state.data["feeds"][sid] = next_cursor
    state.save()
    return items, "fetched"


def _sec_primary_doc(article_url: str, client: GuardedClient) -> str:
    """Choose a filing-supplied document, never guess an issuer site or person."""
    host, path = _safe_url(article_url)
    if (host != "www.sec.gov" or not path.startswith("/Archives/edgar/data/")
            or not path.endswith((".xml", ".html", ".htm"))):
        raise FundingRefusal("sec_filing_link_unverified")
    if path.endswith(".xml"):
        return article_url
    index = client.get(article_url, follow_redirects=True)
    index.raise_for_status()
    if urlsplit(str(index.request.url)).hostname != "www.sec.gov":
        raise FundingRefusal("sec_filing_redirect")
    candidates = set()
    parent = path.rsplit("/", 1)[0] + "/"
    for _, href in _article(index.text).links[:200]:
        full = urljoin(article_url, href)
        try:
            h, p = _safe_url(full)
            if h == "www.sec.gov" and p.startswith(parent) and p.endswith("/primary_doc.xml"):
                candidates.add(full)
        except FundingRefusal:
            continue
    if len(candidates) != 1:
        raise FundingRefusal("sec_primary_doc_unresolved")
    return next(iter(candidates))


def _sec_proxy(source: dict, article: dict, state: State, client: GuardedClient, now) -> None:
    # The Atom list has no issuer site and no round label. Only the filing
    # metadata relevant to sector/amount is touched, never officer/contact nodes.
    key = _key(source["id"], article["key"])
    previous = state.data["deals"].get(key, {})
    if previous.get("status") in ("unresolved", "rejected"):
        state.data["articles"][article["key"]] = {
            "status": "complete", "source": source["id"],
            "seen_at": now.isoformat()}
        state.save()
        state.count(source["id"], "duplicate", now)
        return
    # A previous SEC document fetch may have failed transiently. A durable
    # retryable child is not a terminal duplicate and must be attempted again.
    try:
        # SEC Atom links commonly point to a filing index. Select the exact
        # primary_doc.xml link in that filing's own document listing.
        url = _sec_primary_doc(article["url"], client)
        xml = _xml(client.get(url, follow_redirects=True).content)
        industry = _node_text(xml, "industryGroupType")
        fund = any(e.tag.rsplit("}", 1)[-1] == "investmentFundInfo" for e in xml.iter())
        # Only the primary issuer can supply the company identity. Never
        # accept a similarly named related-person or third-party field.
        primary = next((e for e in xml.iter()
                        if e.tag.rsplit("}", 1)[-1] == "primaryIssuer"), None)
        issuer = _node_text(primary, "entityName") if primary is not None else ""
        amount = _node_text(xml, "totalOfferingAmount")
        if fund or industry.casefold() == "pooled investment fund" or "real estate" in industry.casefold():
            reason = "sec_excluded_fund_or_real_estate"
        elif not issuer or not industry:
            reason = "sec_issuer_or_sector_missing"
        elif industry not in {"Computers", "Telecommunications", "Other Technology",
                              "Business Services", "Other Banking and Financial Services", "Other"}:
            reason = "sec_other_industry"
        else:
            reason = "sec_unverified_offering_proxy"  # amount is NOT money raised
        state.data["deals"][key] = {"status": "unresolved" if reason == "sec_unverified_offering_proxy"
                                    else "rejected", "reason": reason, "seen_at": now.isoformat(),
                                    "source": source["id"],
                                    "issuer": issuer[:150], "offering_amount_proxy": amount[:40]}
    except (FundingRefusal, httpx.HTTPError):
        state.data["deals"][key] = {
            "status": "retryable", "reason": "sec_document_unavailable",
            "seen_at": now.isoformat(), "source": source["id"]}
    state.data["articles"][article["key"]] = {
        "status": ("retryable" if state.data["deals"][key]["status"] == "retryable"
                   else "complete"), "source": source["id"],
        "seen_at": now.isoformat()}
    state.save()
    state.count(source["id"], state.data["deals"][key]["reason"], now)
    state.count(source["id"], "deal_" + state.data["deals"][key]["status"], now)


def _extract_batch(articles: list[dict], state: State, settings, now, *, extractor=None) -> dict:
    inputs = [{"id": a["key"], "headline": a["title"][:220],
               "text": a["text"][:4500]} for a in articles[:4]]
    prompt = (
        "Extract only independently announced financing deals. Never infer an employer "
        "domain, finance stage, location, industry, or remote-first status. "
        "Return JSON {\"articles\":[{\"id\":\"article id\",\"deals\":["
        "{\"issuer\":\"company name\",\"stage\":\"pre-seed|seed|series a|unknown\","
        "\"date\":\"ISO date or empty\",\"sector\":\"ai|fintech|unknown\","
        "\"region\":\"eu|uk|usa|uae|gcc|india|singapore|australia|unknown\","
        "\"remote_first\":false,\"amount\":\"reported amount or empty\","
        "\"aliases\":[]}]}]}. Each item may contain multiple distinct deals. "
        "Use empty deal lists when announcement is unconfirmed.\n"
        + json.dumps(inputs, ensure_ascii=True, separators=(",", ":"))
    )
    worst = len(prompt.encode("utf-8")) + settings.funding_extract_max_output_tokens
    if not state.reserve(now, "day_calls", 1, settings.funding_daily_extraction_calls):
        raise FundingRefusal("daily_extraction_calls")
    if not state.reserve(now, "week_tokens", worst, settings.funding_weekly_extraction_tokens):
        raise FundingRefusal("weekly_extraction_tokens")
    if extractor is None:
        from litellm import completion
        resp = completion(
            model=settings.agent_model("relevance"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=settings.funding_extract_max_output_tokens,
            temperature=0,
        )
        raw = resp["choices"][0]["message"]["content"]
        parsed = json.loads(raw)
    else:
        parsed = extractor(prompt, max_tokens=settings.funding_extract_max_output_tokens)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("articles"), list):
        raise FundingRefusal("invalid_model_result")
    out = {}
    for article in parsed["articles"][:len(inputs)]:
        if (not isinstance(article, dict) or article.get("id") not in
                {a["id"] for a in inputs} or article["id"] in out):
            raise FundingRefusal("invalid_model_result")
        deals = article.get("deals")
        if not isinstance(deals, list) or len(deals) > 8:
            raise FundingRefusal("invalid_model_result")
        out[article["id"]] = deals
    return out


def _employer_link(issuer: str, a: _ArticleHTML, article_url: str) -> str | None:
    publisher, _ = _safe_url(article_url)
    match = []
    target = re.sub(r"[^a-z0-9]", "", issuer.casefold())
    for label, href in a.links[:200]:
        label_norm = re.sub(r"[^a-z0-9]", "", label.casefold())
        if label_norm != target or not target:
            continue
        full = urljoin(article_url, href)
        try:
            host, _ = _safe_url(full)
            if host == publisher or host.endswith("." + publisher) or host in _BOARD_HOST.values():
                continue
            if not registrable(full):
                continue
            match.append(full)
        except FundingRefusal:
            continue
    return match[0] if len(set(match)) == 1 else None


def _board_from_employer(homepage: str, issuer: str, client: GuardedClient,
                         employer_page=None) -> tuple[str, str, str] | None:
    from .resolver import resolve

    seen = set()
    for url in (homepage,):
        if url in seen:
            continue
        seen.add(url)
        page = employer_page if employer_page is not None else client.get(
            url, follow_redirects=True)
        page.raise_for_status()
        html = _article(page.text)
        if (registrable(str(page.request.url)) != registrable(homepage)
                or issuer.casefold() not in html.text[:10000].casefold()):
            return None
        possible = [urljoin(url, link) for _, link in html.links[:150]]
        for board_url in possible:
            try:
                host, path = _safe_url(board_url)
                ats = next((a for a, value in _BOARD_HOST.items() if value == host), "")
                board = path.strip("/")
                if not ats or not _TENANT.fullmatch(board):
                    continue
                native = f"https://{host}/{board}"
                match = resolve(native, client=client, name=issuer)
                if match.ats != ats or match.board != board:
                    continue
                # The board must independently identify its company. A
                # functioning guessed slug or shared ATS host is insufficient.
                board_response = client.get(native, follow_redirects=True)
                board_response.raise_for_status()
                reached = urlsplit(str(board_response.request.url))
                if (reached.scheme != "https" or reached.hostname != host
                        or reached.path.rstrip("/") != f"/{board}"
                        or reached.query or reached.fragment):
                    # A redirect to another tenant is not an owner witness.
                    continue
                board_page = _article(board_response.text)
                if issuer.casefold() not in board_page.text[:12000].casefold():
                    continue
                return ats, board, native
            except (FundingRefusal, httpx.HTTPError, ValueError):
                continue
    return None


def _preview_verified(issuer: str, homepage: str, source_id: str,
                      state: State, settings, client: GuardedClient, now,
                      *, relevance_fn=None, employer_page=None,
                      aliases=()) -> tuple[str, dict | None]:
    """Re-prove the employer, tenant, producer AND consumer, without stores."""
    from .adapters import ADAPTERS
    from .canonical_job_url import admit_feed, source_bound
    from .watchlist import CompanyConfig, load_watchlist, load_preferences

    domain = registrable(homepage)
    if not domain:
        return "psl_unavailable_or_unknown", None
    manual = load_watchlist(Path(settings.config_dir) / "watchlist.yaml")
    identities = {issuer.strip().casefold()} | {
        a.strip().casefold() for a in aliases if isinstance(a, str)}
    if any(c.name.strip().casefold() in identities for c in manual):
        return "manual_precedence", None
    for row in state.data["active"].values():
        existing = {row["name"].strip().casefold()} | {
            a.strip().casefold() for a in row.get("aliases", [])}
        if row["employer_domain"] == domain or not identities.isdisjoint(existing):
            return "duplicate_verified_employer", None
    page = employer_page if employer_page is not None else client.get(
        homepage, follow_redirects=True)
    page.raise_for_status()
    if (registrable(str(page.request.url)) != domain
            or issuer.casefold() not in _article(page.text).text[:10000].casefold()):
        return "employer_homepage_owner_unproved", None
    board = _board_from_employer(homepage, issuer, client, employer_page=page)
    if not board:
        return "guard_not_proven", None
    ats, token, native_url = board
    if any((c.ats == ats and c.board == token)
           or c.careers_url.rstrip("/") == native_url for c in manual):
        return "manual_board_collision", None
    if any(row["ats"] == ats and row["board"] == token
           for row in state.data["active"].values()):
        return "tenant_collision", None
    cfg = CompanyConfig(name=issuer, careers_url=native_url, ats=ats, board=token)
    if not source_bound(cfg, cfg):
        return "source_bound_refused", None
    jobs = ADAPTERS[ats].fetch(cfg, client)
    # The guarded body already bounds the adapter. Sample only N postings,
    # but retain full-feed uniqueness evidence: duplicates after the sample
    # must never make a sampled job's canonical identity look unique.
    if len(jobs) > settings.funding_max_postings_per_pass:
        ids = Counter(j.job_id for j in jobs)
        jobs = [j for j in jobs[:settings.funding_max_postings_per_pass]
                if ids[j.job_id] == 1]
    approved, refused = admit_feed(cfg, cfg, jobs)
    if refused and not approved:
        return "consumer_guard_refused", None
    if not approved:
        return "no_openings", None
    state.count(source_id, "source_and_consumer_proved", now)
    if not state.reserve(now, "day_relevance", 1, settings.funding_daily_relevance_calls):
        return "relevance_budget", None
    if relevance_fn is None:
        from .relevance import relevant
        relevance_fn = relevant
    prefs = load_preferences(Path(settings.config_dir) / "preferences.yaml")
    if not relevance_fn(approved[:min(5, settings.funding_max_postings_per_pass)], prefs):
        return "no_relevant_openings", None
    return "admissible", {
        "name": issuer, "careers_url": native_url, "ats": ats, "board": token,
        "mode": "gated", "discovery": "feed", "employer_domain": domain,
        "homepage": homepage, "aliases": list(aliases),
        "source_id": source_id, "admitted_at": now.isoformat(),
        "last_relevant_at": now.isoformat(),
    }


def _prove_deal(deal: dict, article: dict, state: State, settings,
                client: GuardedClient, now, *, relevance_fn=None
                ) -> tuple[str, dict | None, dict | None]:
    """News evidence plus independently checked homepage; no guessed domain."""
    if not isinstance(deal, dict):
        return "invalid_deal", None, None
    issuer, stage = deal.get("issuer"), deal.get("stage")
    if not isinstance(issuer, str) or not issuer.strip() or len(issuer) > 150:
        return "issuer_unknown", None, None
    if not isinstance(stage, str) or stage not in _STAGE:
        return "round_unconfirmed", None, None
    # In a multi-deal article B's round is not evidence for issuer A.
    mentions = [
        sentence for sentence in re.split(r"[.!?;](?:\s+|$)", article["text"])
        if re.search(r"(?<!\w)" + re.escape(issuer) + r"(?!\w)",
                     sentence, re.I)
    ]
    if not any(_STAGE[stage].search(m) and _FUNDING.search(m) for m in mentions):
        return "round_unconfirmed", None, None
    if not _fresh(deal.get("date", ""), now, settings.funding_eligible_days):
        return "round_date_unconfirmed_or_stale", None, None
    # A model-invented date cannot stand in for a published announcement.
    item_day, round_day = _date(article.get("date", "")), _date(deal["date"])
    if not item_day or not round_day or abs((item_day - round_day).days) > 14:
        return "round_date_unconfirmed_or_stale", None, None
    if deal.get("sector") not in {"ai", "fintech"} or not _SECTOR.search(article["text"]):
        return "sector_unproved", None, None
    if _BLOCKED_SECTOR.search(article["text"]):
        return "fund_not_issuer", None, None
    region = deal.get("region")
    allowed = set(_manifest(settings)["regions"])
    evidenced = _regions(article["text"], allowed)
    remote_exception = (region not in evidenced and deal.get("remote_first") is True
                        and "remote-first-anywhere" in evidenced)
    if region not in evidenced and not remote_exception:
        return "region_unproved", None, None
    homepage = _employer_link(issuer, article["html"], article["url"])
    if not homepage:
        return "employer_site_unproved", None, None
    domain = registrable(homepage)
    if not domain:
        return "psl_unavailable_or_unknown", None, None
    # Independently read the candidate homepage before treating a lead as a
    # verified employer. A publisher hyperlink is NOT proof of ownership.
    page = client.get(homepage, follow_redirects=True)
    page.raise_for_status()
    homepage_text = _article(page.text).text[:10000]
    if (registrable(str(page.request.url)) != domain
            or issuer.casefold() not in homepage_text.casefold()):
        return "employer_homepage_owner_unproved", None, None
    # The news article and employer homepage must independently corroborate
    # every alias before it can participate in identity precedence.
    proposed = deal.get("aliases", [])
    if not isinstance(proposed, list) or len(proposed) > 8:
        return "aliases_unverified", None, None
    proven_aliases = []
    for alias in proposed:
        if (not isinstance(alias, str) or not 2 <= len(alias.strip()) <= 150
                or alias.strip().casefold() == issuer.strip().casefold()):
            continue
        word = r"(?<!\w)" + re.escape(alias.strip()) + r"(?!\w)"
        if (re.search(word, article["text"], re.I)
                and re.search(word, homepage_text, re.I)
                and alias.strip().casefold() not in
                {a.casefold() for a in proven_aliases}):
            proven_aliases.append(alias.strip())
    # A news writer calling a business remote-first is not independent proof.
    if remote_exception and not _REMOTE.search(homepage_text):
        return "remote_first_not_employer_evidenced", None, None
    state.count(article["source"], "verified_homepage", now)
    evidence = {"name": issuer, "homepage": homepage, "employer_domain": domain,
                "source": article["source"], "aliases": proven_aliases}
    reason, active = _preview_verified(
        issuer, homepage, article["source"], state, settings, client, now,
        relevance_fn=relevance_fn, employer_page=page, aliases=proven_aliases)
    return reason, active, evidence


def _recheck_pending(state: State, settings, client: GuardedClient,
                     now, *, relevance_fn=None) -> None:
    """One bounded weekly check per independently verified pending employer."""
    reviewed = 0
    for key, row in list(state.data["pending"].items()):
        if reviewed >= settings.funding_max_candidates_per_pass:
            break
        if not isinstance(row, dict):
            raise FundingRefusal("corrupt_pending_state")
        expiry = _date(row.get("expires", ""))
        if expiry and now >= expiry:
            del state.data["pending"][key]
            state.count(row.get("source", "local"), "pending_expired", now)
            continue
        due = _date(row.get("next_check", ""))
        if due is None or due > now:
            continue
        reviewed += 1
        try:
            if (not isinstance(row.get("name"), str)
                    or not isinstance(row.get("homepage"), str)
                    or registrable(row["homepage"]) != row.get("employer_domain")):
                raise FundingRefusal("corrupt_pending_state")
            reason, active = _preview_verified(
                row["name"], row["homepage"], row["source"],
                state, settings, client, now, relevance_fn=relevance_fn,
                aliases=row.get("aliases", []))
        except (FundingRefusal, httpx.HTTPError, ValueError):
            reason, active = "pending_recheck_failed", None
        if active is not None and not state.reserve(
                now, "day_added", 1, settings.funding_daily_new_companies):
            reason, active = "daily_addition_deferred", None
        if active is not None:
            state.data["active"][key] = active
            del state.data["pending"][key]
            state.data["deals"][key]["status"] = "active"
            state.data["deals"][key]["reason"] = "recheck_admitted"
            state.count(row["source"], "recheck_admitted", now)
            state.count(row["source"], "added", now)
        else:
            row["reason"] = reason
            row["next_check"] = (now + timedelta(
                days=settings.funding_recheck_days)).isoformat()
            if key in state.data["deals"]:
                state.data["deals"][key]["reason"] = reason
            state.count(row["source"], reason, now)
        state.save()


def poll_once(settings=None, *, now=None, client_factory=None, extractor=None,
              relevance_fn=None) -> dict:
    """One offline-injectable pass. This function NEVER writes to pipeline stores."""
    settings = settings or get_settings()
    if not ready(settings):
        return {"status": "disabled"}
    now = _utc(now)
    state = State(Path(settings.local_dir) / "funded-watchlist.yaml")
    manifest = _manifest(settings)
    client = (client_factory(state, settings, now) if client_factory else
              GuardedClient(state=state, settings=settings, now=now))
    candidates = []
    for source in (x for x in manifest["sources"] if x["enabled"]):
        sid = source["id"]
        try:
            items, status = _source_payload(source, state, client, now)
            state.count(sid, status, now)
            for item in items:
                if source["kind"] == "sec_atom":
                    if not _fresh(item["date"], now, settings.funding_eligible_days):
                        state.data["articles"][item["key"]] = {
                            "status": "complete", "source": sid,
                            "seen_at": now.isoformat()}
                        state.save()
                        state.count(sid, "stale", now)
                        continue
                    _sec_proxy(source, item, state, client, now)
                    continue
                if item["key"] in state.data["articles"] and state.data["articles"][item["key"]].get(
                        "status") == "complete":
                    state.count(sid, "duplicate", now)
                    continue
                if not _fresh(item["date"], now, settings.funding_eligible_days):
                    state.data["articles"][item["key"]] = {
                        "status": "complete", "source": sid,
                        "seen_at": now.isoformat()}
                    state.save()
                    state.count(sid, "stale", now)
                    continue
                if not _FUNDING.search(item["title"]) or _BLOCKED_SECTOR.search(item["title"]):
                    state.data["articles"][item["key"]] = {
                        "status": "complete", "source": sid,
                        "seen_at": now.isoformat()}
                    state.save()
                    state.count(sid, "cheap_filtered", now)
                    continue
                if len(candidates) >= settings.funding_max_candidates_per_pass:
                    state.data["articles"][item["key"]] = {
                        "status": "retryable", "source": sid,
                        "seen_at": now.isoformat()}
                    state.save()
                    state.count(sid, "candidate_deferred", now)
                    continue
                try:
                    resp = client.get(item["url"], follow_redirects=True)
                    resp.raise_for_status()
                    html = _article(resp.text)
                    candidates.append({**item, "source": sid, "text": html.text[:10000],
                                       "html": html, "url": item["url"]})
                except (FundingRefusal, httpx.HTTPError):
                    state.data["articles"][item["key"]] = {
                        "status": "retryable", "source": sid,
                        "seen_at": now.isoformat()}
                    state.save()
                    state.count(sid, "article_unavailable", now)
        except FundingRefusal as exc:
            # Reason codes are fixed by this module; never log source payload,
            # the private SEC User-Agent, or arbitrary exception details.
            state.count(sid, str(exc), now)
        except (httpx.HTTPError, OSError, ValueError):
            state.count(sid, "source_error", now)
    for index in range(0, len(candidates), 4):
        batch = candidates[index:index + 4]
        unplanned = [a for a in batch if not isinstance(
            state.data["articles"].get(a["key"], {}).get("children"), list)]
        try:
            extracted = (_extract_batch(unplanned, state, settings, now,
                                        extractor=extractor) if unplanned else {})
        except Exception:  # LLM failures consume the pre-call reservation
            for item in unplanned:
                state.data["articles"][item["key"]] = {
                    "status": "retryable", "source": item["source"],
                    "seen_at": now.isoformat()}
            state.save()
            state.count(batch[0]["source"], "extraction_deferred", now)
            continue
        for article in batch:
            saved = state.data["articles"].get(article["key"], {})
            children = saved.get("children") if isinstance(saved.get("children"), list) else (
                extracted.get(article["key"]))
            if children is None:
                state.data["articles"][article["key"]] = {
                    "status": "retryable", "source": article["source"],
                    "seen_at": now.isoformat()}
                state.save()
                continue
            # Preserve the exact extracted child plan before processing ANY
            # child. Restart replays this plan rather than model drift.
            state.data["articles"][article["key"]] = {
                "status": "processing", "source": article["source"],
                "seen_at": now.isoformat(), "children": children}
            state.save()
            if not isinstance(saved.get("children"), list):
                for _ in children:
                    state.count(article["source"], "extracted_deal", now)
            every_persisted = True
            for deal in children:
                issuer = deal.get("issuer", "") if isinstance(deal, dict) else ""
                stage = deal.get("stage", "") if isinstance(deal, dict) else ""
                announced = deal.get("date", "") if isinstance(deal, dict) else ""
                if not all(isinstance(x, str) and x.strip() for x in (issuer, stage, announced)):
                    key = _key(article["source"], article["key"], "ambiguous",
                               json.dumps(deal, sort_keys=True, default=str)[:512])
                    state.data["unresolved"][key] = {
                        "status": "retryable", "reason": "ambiguous_child_id",
                        "seen_at": now.isoformat()}
                    state.save()
                    every_persisted = False
                    continue
                key = _key(article["source"], article["key"], issuer.casefold(), stage, announced)
                previous = state.data["deals"].get(key, {})
                if previous.get("status") in ("active", "rejected", "unresolved", "pending"):
                    state.count(article["source"], "duplicate", now)
                    continue
                try:
                    reason, active, verified = _prove_deal(
                        deal, article, state, settings, client, now, relevance_fn=relevance_fn)
                except (FundingRefusal, httpx.HTTPError, ValueError, KeyError):
                    reason, active, verified = "probe_retryable", None, None
                status = ("active" if active else "retryable" if reason in
                          {"probe_retryable", "relevance_budget", "postings_limit"}
                          else "pending" if reason in {
                              "guard_not_proven", "no_openings", "no_relevant_openings",
                              "consumer_guard_refused", "source_bound_refused",
                              "manual_board_collision", "tenant_collision"}
                          else "unresolved" if reason in {
                              "employer_site_unproved", "psl_unavailable_or_unknown",
                              "remote_first_not_employer_evidenced",
                              "employer_homepage_owner_unproved",
                              "round_unconfirmed", "round_date_unconfirmed_or_stale",
                              "sector_unproved", "region_unproved", "issuer_unknown"}
                          else "rejected")
                if active and not state.reserve(now, "day_added", 1,
                                                settings.funding_daily_new_companies):
                    status, reason, active = "retryable", "daily_addition_deferred", None
                if active:
                    state.data["active"][key] = active
                if status == "pending":
                    state.data["pending"][key] = {
                        **(verified or {}), "name": issuer[:150],
                        "reason": reason, "seen_at": now.isoformat(),
                        "next_check": (now + timedelta(days=settings.funding_recheck_days)).isoformat(),
                        "expires": (now + timedelta(days=settings.funding_pending_days)).isoformat(),
                        "source": article["source"]}
                state.data["deals"][key] = {
                    "status": status, "reason": reason, "source": article["source"],
                    "seen_at": now.isoformat()}
                state.save()  # child A durably acknowledged before child B
                state.count(article["source"], reason, now)
                state.count(article["source"], "deal_" + status, now)
                if status == "active":
                    state.count(article["source"], "added", now)
                if status == "retryable":
                    every_persisted = False
            state.data["articles"][article["key"]] = {
                "status": "complete" if every_persisted else "retryable",
                "source": article["source"], "seen_at": now.isoformat(),
                **({} if every_persisted else {"children": children})}
            state.save()
    _recheck_pending(state, settings, client, now, relevance_fn=relevance_fn)
    # Unproductive entries can age out, but manual watchlist is never touched.
    for key, row in list(state.data["active"].items()):
        last = _date(row["last_relevant_at"])
        if last and now - last > timedelta(days=settings.funding_active_days):
            del state.data["active"][key]
            state.count(row["source_id"], "auto_expired", now)
            state.count(row["source_id"], "expired", now)
    for key, row in list(state.data["pending"].items()):
        expiry = _date(row.get("expires", ""))
        if expiry and now >= expiry:
            del state.data["pending"][key]
            state.count(row["source"], "pending_expired", now)
    state.compact(now)
    return {"status": "ok", "report": state.data["report"]}


def run_poller(settings=None) -> None:
    """Only daemon startup may call this; never starts cloud or disabled sources."""
    settings = settings or get_settings()
    if not ready(settings):
        return
    from core import flags
    while True:
        try:
            if ready(settings) and not flags.paused():
                poll_once(settings)
        except Exception:
            # Corrupt state must NEVER be overwritten by a fresh empty ledger.
            from core.logging import get_logger
            get_logger(__name__).exception("funding poll pass refused")
        time.sleep(settings.funding_poll_hours * 3600)
