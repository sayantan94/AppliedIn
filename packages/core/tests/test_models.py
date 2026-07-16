from appliedin_core.models import JobRecord


def _job(**kw):
    base = dict(
        company="Acme",
        job_id="123",
        title="SWE",
        jd_url="u",
        jd_text="build things",
        location="Remote",
        ats="greenhouse",
    )
    base.update(kw)
    return JobRecord(**base)


def test_pk_is_company_hash_job():
    assert _job().pk == "acme#123"


def test_jd_hash_stable_across_cosmetic_reposts():
    a = _job(job_id="1", jd_text="Build  things.\n")
    b = _job(job_id="2", jd_text="build things.")
    assert a.jd_hash == b.jd_hash
