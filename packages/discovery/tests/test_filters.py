from appliedin_core.models import JobRecord
from appliedin_discovery.filters import stage1_match
from appliedin_discovery.watchlist import Preferences


def _job(title="Backend Engineer", jd="python distributed systems", loc="Remote - US"):
    return JobRecord(
        company="Acme", job_id="1", title=title, jd_url="u", jd_text=jd, location=loc,
        ats="greenhouse",
    )


def test_exclude_keyword_rejects():
    prefs = Preferences(exclude_keywords=["clearance"])
    assert stage1_match(_job(jd="requires security clearance"), prefs) is False


def test_missing_include_keyword_rejects():
    prefs = Preferences(include_keywords=["golang"])
    assert stage1_match(_job(jd="python only"), prefs) is False


def test_location_mismatch_rejects():
    prefs = Preferences(locations=["New York"])
    assert stage1_match(_job(loc="London, UK"), prefs) is False


def test_remote_only_passes_remote():
    prefs = Preferences(remote_only=True)
    assert stage1_match(_job(loc="Remote - US"), prefs) is True


def test_happy_path_matches():
    prefs = Preferences(include_keywords=["python"], titles=["Engineer"], locations=["US"])
    assert stage1_match(_job(), prefs) is True
