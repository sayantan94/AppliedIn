from core.models import JobRecord
from discovery.filters import stage1_match
from discovery.watchlist import Preferences


def _job(title="Backend Engineer", jd="python distributed systems", loc="Remote - US"):
    return JobRecord(
        company="Acme", job_id="1", title=title, jd_url="u", jd_text=jd, location=loc,
        ats="greenhouse",
    )


def test_exclude_keyword_rejects():
    prefs = Preferences(exclude_keywords=["clearance"])
    assert stage1_match(_job(jd="requires security clearance"), prefs) is False


def test_a_missing_include_keyword_does_not_reject():
    """include_keywords RAISE fit; they are not a requirement.

    preferences.yaml calls them "soft signals that RAISE fit (not required)" and
    this filter treated them as mandatory. It only runs when the relevance screen
    fails, so the effect was that a degraded screen became a STRICTER one — and
    the plain "Senior Software Engineer" roles the owner also asked for were
    silently dropped for lacking an AI keyword.
    """
    prefs = Preferences(include_keywords=["golang"])
    assert stage1_match(_job(jd="python only"), prefs) is True

    # The exclude list is the one that still rejects outright.
    assert stage1_match(_job(jd="python only"),
                        Preferences(exclude_keywords=["python"])) is False


def test_location_mismatch_rejects():
    prefs = Preferences(locations=["New York"])
    assert stage1_match(_job(loc="London, UK"), prefs) is False


def test_remote_only_passes_remote():
    prefs = Preferences(remote_only=True)
    assert stage1_match(_job(loc="Remote - US"), prefs) is True


def test_happy_path_matches():
    prefs = Preferences(include_keywords=["python"], titles=["Engineer"], locations=["US"])
    assert stage1_match(_job(), prefs) is True


def test_only_one_discovery_runs_at_a_time():
    """A scheduled sweep and a Discover click both call run_discovery().

    They were able to run together, so a single-company run the owner had just
    started got interleaved with a full watchlist sweep and the log they were
    watching described neither.
    """
    from discovery.handler import _RUNNING, run_discovery

    assert _RUNNING.acquire(blocking=False)
    try:
        result = run_discovery(only=["NVIDIA"])
        assert result["enqueued"] == 0
        assert "another discovery is running" in result["skipped"]
    finally:
        _RUNNING.release()

    # and the lock is free again afterwards
    assert _RUNNING.acquire(blocking=False)
    _RUNNING.release()


def test_a_reset_voids_work_that_was_already_running():
    """Emptying the store does not stop the workers.

    A job the evaluate worker was midway through finishes, writes itself back,
    and reappears on a board the owner just cleared — a zombie they did not ask
    for and cannot explain. Each run carries the epoch it began in.
    """
    from core import flags

    started = flags.reset_epoch()
    assert not flags.stale_run(started)

    flags.mark_reset()
    assert flags.stale_run(started), "work from before the reset must be void"
    assert not flags.stale_run(flags.reset_epoch()), "work started after it is fine"
