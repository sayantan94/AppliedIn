"""Seed config/watchlist.yaml from a plain list of career-page URLs.

The seed for a company is just its career-page URL. This script resolves each
URL to an ATS + feed token (or crawl) and writes a ready watchlist.yaml.

Input file: one company per line, either
    Acme = https://boards.greenhouse.io/acme
or just
    https://boards.greenhouse.io/acme        (name inferred from the token/host)

Usage:
    uv run python scripts/seed_watchlist.py careers.txt > config/watchlist.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml
from discovery.resolver import resolve


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "=" in line:
        name, url = line.split("=", 1)
        return name.strip(), url.strip()
    url = line
    host = urlparse(url).netloc
    # infer a rough name from the last path segment or host label
    path_tail = [p for p in urlparse(url).path.split("/") if p]
    name = (path_tail[-1] if path_tail else host.split(".")[0]).replace("-", " ").title()
    return name, url


def build(lines: list[str]) -> dict:
    companies = []
    with httpx.Client(headers={"User-Agent": "AppliedIn/0.1"}) as client:
        for raw in lines:
            parsed = _parse_line(raw)
            if not parsed:
                continue
            name, url = parsed
            match = resolve(url, client)
            entry = {
                "name": name,
                "careers_url": url,
                "ats": match.ats,
                "board": match.board,
                "discovery": match.discovery.value,
                "login_secret": f"portal/{name.lower().replace(' ', '')}",
                "mode": "gated",
            }
            companies.append(entry)
            print(f"# {name}: {match.ats} ({match.discovery.value})", file=sys.stderr)
    return {"companies": companies}


def main(path: str) -> None:
    lines = Path(path).read_text().splitlines()
    print(yaml.safe_dump(build(lines), sort_keys=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "careers.txt")
