"""Internal bookkeeping rows must never be confused with a company called Meta.

Bookkeeping rows — discovery watermarks, run markers, the daily cap — are keyed
``meta#<kind>…``. A job at Meta is keyed by `make_pk` as ``meta#<job_id>``. For as
long as "is this internal?" was spelled ``pk.startswith("meta#")``, every one of
Meta's real postings was bookkeeping: hidden from the board, left out of the
status index so no sweep could find them, skipped by orphan recovery, and
reported after a successful crawl of 26 postings as "no postings could be read
from this careers page at all". Seventeen call sites, one wrong idea.

The rule lives here now, and only here. An internal row is one whose kind is
registered below; a test scans the source so a new kind cannot be written
without being registered.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.ids import INTERNAL_KINDS, is_internal_pk, make_pk

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.mark.parametrize("pk", [
    "meta#watermark#stripe", "meta#run#meta", "meta#profiles", "meta#prefs",
    "meta#dailycap", "meta#undated#meta", "meta#age#netflix",
])
def test_bookkeeping_rows_are_internal(pk):
    assert is_internal_pk(pk)


@pytest.mark.parametrize("pk", [
    make_pk("Meta", "2176819739360965"),
    "meta#1380052417665663",
    make_pk("Stripe", "8095390"),
    "waymo#7859522",
    "meta#some-slug-job",
])
def test_a_job_is_never_internal(pk):
    assert not is_internal_pk(pk)


def test_a_company_named_meta_gets_ordinary_job_keys():
    assert make_pk("Meta", "42") == "meta#42"
    assert not is_internal_pk("meta#42")


def test_empty_and_garbage_are_not_internal():
    assert not is_internal_pk("")
    assert not is_internal_pk("meta")
    assert not is_internal_pk("#")


def test_every_internal_kind_written_in_source_is_registered():
    """A new bookkeeping kind that is not registered would be treated as a job at
    Meta. Scan for the writers rather than trusting memory."""
    seen = set()
    rx = re.compile(r'''["']meta#([a-z_]+)''')
    for path in SRC.rglob("*.py"):
        for m in rx.finditer(path.read_text()):
            seen.add(m.group(1))
    missing = seen - set(INTERNAL_KINDS)
    assert not missing, f"unregistered internal kinds written in src/: {sorted(missing)}"


def test_no_call_site_spells_the_rule_by_hand():
    """The seventeen sites are gone; none may come back."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "ids.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if 'startswith("meta#")' in line:
                offenders.append(f"{path.relative_to(SRC)}:{i}")
    assert not offenders, offenders
