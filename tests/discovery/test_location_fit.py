"""Posting-side eligibility matrix; no applicant or runtime state is consulted."""

import pytest

from discovery.relevance import classify_location_fit as fit


@pytest.mark.parametrize("posting,expected", [
    ("Remote — Vietnam", "in_class"),
    ("Remote — APAC", "in_class"),
    ("Remote worldwide", "in_class"),
    ("Remote from Asia-Pacific", "in_class"),
    ("Remote — United States only", "out_of_class"),
    ("Remote US-only", "out_of_class"),
    ("Remote — Canada-only", "out_of_class"),
    ("Remote — Japan residents only", "out_of_class"),
    ("Remote — EU-only", "out_of_class"),
    ("Remote — APAC except Vietnam", "out_of_class"),
    ("WorkplaceType: Remote; Location: APAC; applicants must be based in Japan",
     "out_of_class"),
    ("Remote — APAC; Office: Japan", "in_class"),
    ("Remote, eligible worldwide; Office: US", "in_class"),
    ("Remote US-only; Office: Vietnam", "out_of_class"),
    ("Remote; Location: US", "unknown"),
    ('Ashby workplaceType: "Remote"\nAshby isRemote: true\n'
     'Ashby location: "United States"', "unknown"),
    ('Ashby workplaceType: "Remote"\nAshby isRemote: false\n'
     'Ashby secondaryLocations: []', "unknown"),
    ("isRemote: false", "unknown"),
    ("Remote worldwide; Remote US-only", "unknown"),
    ("Remote — APAC; remote applicants must reside in Japan", "unknown"),
    ("Office: Japan; Client: Singapore; hires worldwide as a company", "unknown"),
    ("Remote; must be authorized to work in Japan", "unknown"),
    ("Remote; nationality: Japan", "unknown"),
    ("Remote; Visa sponsorship only in US", "unknown"),
    ("We operate offices in Vietnam and Japan.", "unknown"),
    ("Remote; Location: []; secondaryLocations: []", "unknown"),
    ("Remote; Location: null; isRemote: false", "unknown"),
    ("On-site — Japan", "out_of_class"),
    ("Hybrid in Singapore", "out_of_class"),
    ("On-site — Australia", "out_of_class"),
    ("On-site — Vietnam", "in_class"),
    ("Hybrid in United Kingdom", "in_class"),
    ("On-site — United States", "in_class"),
    ("Hybrid in Canada", "in_class"),
    ("Hybrid — Europe", "in_class"),
    ("On-site — Germany", "in_class"),
    ("WorkplaceType: Hybrid; Worksite: Saudi Arabia", "in_class"),
    ("WorkplaceType: Hybrid; Worksite: UAE", "in_class"),
    ("WorkplaceType: Hybrid; Worksite: Qatar", "in_class"),
    ("WorkplaceType: Hybrid; Worksite: Oman", "in_class"),
    ("WorkplaceType: Hybrid; Worksite: Bahrain", "in_class"),
    ("WorkplaceType: Hybrid; Worksite: Kuwait", "in_class"),
    ("Hybrid — Vietnam or Japan", "unknown"),
    ("WorkplaceType: Remote; Office: Singapore", "unknown"),
])
def test_bounded_location_fit(posting, expected):
    assert fit(posting) == expected


def test_location_fit_does_not_change_discovery_selection_contract():
    from discovery import relevance

    assert callable(relevance.relevant)
    assert relevance.classify_location_fit("Remote — APAC") == "in_class"
