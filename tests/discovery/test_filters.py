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


def test_a_company_is_never_scanned_by_two_runs_at_once():
    """A scheduled sweep and a Discover click both call run_discovery().

    They were able to run together, so a single-company run the owner had just
    started got interleaved with a full watchlist sweep and the log they were
    watching described neither.

    The guard is per COMPANY, not global. Two crawls of different employers share
    nothing — different sites, different feeds, different browser sessions — and
    refusing them wholesale meant a scan of one company blocked every other one.
    What must not happen is two runs scanning the SAME company, duplicating the
    work and racing each other's writes.
    """
    from discovery.handler import _claim, _release, run_discovery, scanning

    assert _claim({"nvidia"}) == {"nvidia"}
    try:
        result = run_discovery(only=["NVIDIA"])
        assert result["enqueued"] == 0
        assert "already scanning" in result["skipped"]

        # A DIFFERENT company is not blocked by it.
        assert _claim({"waymo"}) == {"waymo"}
        _release({"waymo"})
    finally:
        _release({"nvidia"})

    assert scanning() == set(), "claims are released afterwards"


def test_pause_does_not_veto_a_scan_the_owner_pressed():
    """Pause means "do not go and find work on your own", not "refuse me".

    These were one flag. The in loop stop check read `paused`, sat at the top of
    the company loop, and so a Discover press on a paused board broke out after
    zero companies while /actions/discover still returned ok — six times in one
    evening, each one reported to the owner as a scan that had started.

    A scheduled sweep must still freeze while paused, and a Stop pressed DURING
    a manual run must still land. All three are asserted here because fixing any
    one of them by hand is what broke the other two.
    """
    from core import flags
    from discovery.handler import _stop_requested

    real_paused, real_epoch = flags.paused, flags.stop_epoch
    try:
        flags.paused = lambda: True          # the board is paused throughout
        flags.stop_epoch = lambda: 7

        # Owner presses Discover: the run began at epoch 7 and no Stop has been
        # asserted since, so it proceeds despite the pause.
        assert _stop_requested(manual=True, epoch0=7) is False

        # Same paused board, but the SCHEDULED sweep stays frozen.
        assert _stop_requested(manual=False, epoch0=7) is True

        # Stop pressed while the manual run is going: the epoch moved, so it lands.
        flags.stop_epoch = lambda: 8
        assert _stop_requested(manual=True, epoch0=7) is True
    finally:
        flags.paused, flags.stop_epoch = real_paused, real_epoch


def test_pause_does_not_veto_a_process_pass_the_owner_pressed():
    """The same contract for the backlog pass, which has the same shape.

    Every caller of process_backlog_once is a button, so reading `paused` in the
    per job check meant a paused board took the press, returned ok:true, and
    broke before job one with the whole backlog waiting. Asserted separately from
    discovery because the two checks live in different modules and the first fix
    landed in only one of them.
    """
    from core import flags
    from daemon import _pass_cancelled

    real_paused, real_epoch = flags.paused, flags.stop_epoch
    try:
        flags.paused = lambda: True
        flags.stop_epoch = lambda: 3

        assert _pass_cancelled(manual=True, epoch0=3) is False   # owner pressed it
        assert _pass_cancelled(manual=False, epoch0=3) is True   # scheduled, frozen

        flags.stop_epoch = lambda: 4                             # Stop pressed
        assert _pass_cancelled(manual=True, epoch0=3) is True
    finally:
        flags.paused, flags.stop_epoch = real_paused, real_epoch


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


def test_the_rate_limit_brake_backs_off_and_recovers():
    """A tokens-per-minute budget belongs to the whole process.

    Retrying inside one call does not help: four lanes each retrying hit the same
    ceiling four times over. So a limit anywhere holds everyone, the hold doubles
    while limits keep arriving, and it decays once they stop rather than leaving
    the pipeline crawling for the rest of the run.
    """
    from core import throttle

    throttle._resume_at = throttle._cooldown = throttle._last_limit = 0.0
    assert not throttle.state()["cooling_down"]

    assert throttle.note_rate_limit() == 4.0
    assert throttle.note_rate_limit() == 8.0
    assert throttle.note_rate_limit() == 16.0
    assert throttle.state()["cooling_down"]

    # a burst that has passed starts over instead of compounding
    throttle._last_limit -= throttle._DECAY_AFTER_S + 1
    assert throttle.note_rate_limit() == 4.0

    throttle._resume_at = throttle._cooldown = throttle._last_limit = 0.0
