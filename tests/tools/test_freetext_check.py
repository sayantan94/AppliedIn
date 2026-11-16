"""Issue #370: one canonical free-text checker and its two daemon storage boundaries.

Synthetic text only (Test User / example-co). No network, browser, Redis, owner
stores, application approval, queue or Submit: every collaborator is a stub.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import freetext_check as ft

REPO = Path(__file__).resolve().parents[2]
CANONICAL = REPO / "src" / "tools" / "freetext_check.py"
SHIM = REPO / "scripts" / "freetext_check_shim.py"

CLEAN = "I led a small product team and shipped two releases. I would enjoy joining yours."


# --- pure tells(): cases carried over from the operator-local suite -------------------

def test_tells_clean_text_passes():
    assert ft.tells(CLEAN) == []


def test_tells_empty_text_passes():
    assert ft.tells("") == []


def test_tells_transparency_sentence_alone_does_not_trigger():
    assert ft.tells(ft.TRANSPARENCY) == []
    assert ft.tells(CLEAN + " " + ft.TRANSPARENCY) == []


def test_tells_transparency_exemption_is_exact_and_strips_only_itself():
    assert ft.tells(ft.TRANSPARENCY + " I am passionate about this.") == ["passionate about"]
    assert ft.tells(ft.TRANSPARENCY.replace("; ", " — ")) == ["em dash"]


TELL_CASES = [
    ("I worked there — happily.", "em dash"),
    ("N/A", "N/A"),
    ("n/a", "N/A"),
    ("Answer: N/A.", "N/A"),
    ("Not applicable", "not applicable"),
    ("This is not applicable to me.", "not applicable"),
    ("Not specified in my profile.", "not specified in my profile"),
    ("not specified in my resume", "not specified in my resume"),
    ("It is not mentioned in my resume.", "not mentioned in my resume"),
    ("See my resume.", "see my resume"),
    ("See my CV for details.", "see my cv"),
    ("As per my resume, I did it.", "as per my resume"),
    ("Per my resume I did it.", "per my resume"),
    ("I am not an AI.", "I am not an AI"),
    ("I'm not an AI.", "I'm not an AI"),
    ("I’m not an AI.", "I'm not an AI"),
    ("I am not an LLM.", "I am not an LLM"),
    ("I'm not an LLM.", "I'm not an LLM"),
    ("Rest assured I am not a bot.", "not a bot"),
    ("As an AI, I cannot say.", "as an AI"),
    ("As a language model I think so.", "as a language model"),
    ("I'm excited to apply.", "I'm excited"),
    ("I am excited to apply.", "I am excited"),
    ("I'm thrilled to apply.", "I'm thrilled"),
    ("I am thrilled to apply.", "I am thrilled"),
    ("I am passionate about payments.", "passionate about"),
    ("I want to delve into the stack.", "delve"),
    ("I leverage data daily.", "leverage"),
    ("I am writing to apply.", "I am writing to"),
]


@pytest.mark.parametrize("text,rule", TELL_CASES)
def test_tells_each_phrase_triggers(text, rule):
    assert rule in ft.tells(text)


@pytest.mark.parametrize("bullet", ["- one", "* one", "• one"])
def test_tells_short_bullet_list_triggers(bullet):
    assert "bullet list in a short answer" in ft.tells("Skills:\n" + bullet + "\n" + bullet)


def test_tells_long_bullet_list_is_allowed():
    assert ft.tells("- " + " ".join(["word"] * 200)) == []


def test_tells_non_tells_are_not_flagged():
    text = "We ship a data-driven product; 3-5 years, an AI product, a banana split."
    assert ft.tells(text) == []
    assert ft.tells("I enjoy the delivery side and a leveled team.") == []


def test_tells_rule_list_has_one_owner():
    """The shim and graph hold no rules of their own; only the canonical module does."""
    for path in (SHIM, REPO / "src" / "agent" / "graph.py"):
        text = path.read_text(encoding="utf-8")
        assert "_TELL_PATTERNS: list" not in text and '("em dash"' not in text
        assert "passionate about" not in text
        assert "COMMON = {" not in text


# --- legacy CLI through the shim, from a disposable synthetic layout ------------------

STUB_STORES = '''
from types import SimpleNamespace

ROW = {"company": "example-co", "title": "Test Engineer", "location": "Remote",
       "jd_text": "Example-co builds Widgetry tools."}


def make_stores():
    return SimpleNamespace(tracking=SimpleNamespace(get=lambda pk: dict(ROW)))
'''


@pytest.fixture
def layout(tmp_path):
    """A synthetic checkout: canonical checker copy, a stub store, a synthetic seed
    résumé and the shim installed at the legacy ignored path."""
    root = tmp_path / "checkout"
    (root / "src" / "tools").mkdir(parents=True)
    (root / "src" / "core").mkdir(parents=True)
    (root / "src" / "tools" / "__init__.py").write_text("")
    (root / "src" / "core" / "__init__.py").write_text("")
    shutil.copy(CANONICAL, root / "src" / "tools" / "freetext_check.py")
    (root / "src" / "core" / "stores.py").write_text(STUB_STORES)
    (root / "resume").mkdir()
    (root / "resume" / "base.tex").write_text(
        r"\resumeItem{Test User shipped Widgetry for 12 teams at Example Labs.}")
    (root / ".local" / "operator").mkdir(parents=True)
    shutil.copy(SHIM, root / ".local" / "operator" / "freetext_check.py")
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    return SimpleNamespace(root=root, foreign=foreign, tmp=tmp_path)


def _env(pythonpath: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    return env


def _run(argv, cwd, pythonpath=None):
    proc = subprocess.run([sys.executable, *argv], cwd=cwd, env=_env(pythonpath),
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


ANSWERS = {
    "clean": ("I shipped Widgetry at Example Labs for 12 teams.", 0, "OK\n"),
    "tell": ("I shipped Widgetry. I am passionate about it.", 1,
             "TELL: passionate about\n"
             "Rewrite the answer in plain first-person prose without these agent tells.\n"),
    "unsupported": ("I shipped Gizmocorp tooling for 40 teams.", 1,
                    "UNSUPPORTED: 40, Gizmocorp\n"
                    "Remove or replace each item with a fact from the tailored résumé or "
                    "facts file.\n"),
}


@pytest.mark.parametrize("case", sorted(ANSWERS))
def test_cli_shim_matches_canonical_from_foreign_cwd(layout, case):
    text, status, stdout = ANSWERS[case]
    answer = layout.tmp / f"{case}.txt"
    answer.write_text(text)
    legacy = layout.root / ".local" / "operator" / "freetext_check.py"
    canonical = layout.root / "src" / "tools" / "freetext_check.py"
    # Legacy invocation from the repo root, as documented.
    root_run = _run([str(legacy), "example-co#job-1", str(answer)], layout.root, "src")
    # Foreign CWD with no checkout PYTHONPATH at all.
    foreign_run = _run([str(legacy), "example-co#job-1", str(answer)], layout.foreign)
    canonical_run = _run([str(canonical), "example-co#job-1", str(answer)], layout.root,
                         str(layout.root / "src"))
    assert root_run == foreign_run == canonical_run
    assert root_run[0] == status and root_run[1] == stdout and root_run[2] == ""


@pytest.mark.parametrize("argv", [[], ["example-co#job-1"]])
def test_cli_shim_usage_errors_match_canonical(layout, argv):
    legacy = layout.root / ".local" / "operator" / "freetext_check.py"
    canonical = layout.root / "src" / "tools" / "freetext_check.py"
    shim_code, shim_out, shim_err = _run([str(legacy), *argv], layout.foreign)
    can_code, can_out, can_err = _run([str(canonical), *argv], layout.root,
                                      str(layout.root / "src"))
    assert shim_code == can_code == 1
    assert shim_out == can_out == ""
    assert shim_err.strip().splitlines()[-1] == can_err.strip().splitlines()[-1]
    assert "IndexError" in shim_err


def test_cli_shim_missing_answer_file_matches_canonical(layout):
    legacy = layout.root / ".local" / "operator" / "freetext_check.py"
    canonical = layout.root / "src" / "tools" / "freetext_check.py"
    missing = str(layout.tmp / "absent.txt")
    shim = _run([str(legacy), "example-co#job-1", missing], layout.foreign)
    can = _run([str(canonical), "example-co#job-1", missing], layout.root,
               str(layout.root / "src"))
    assert shim[0] == can[0] == 1
    assert shim[2].strip().splitlines()[-1] == can[2].strip().splitlines()[-1]
    assert "FileNotFoundError" in shim[2]


def test_cli_shim_module_reexports_canonical_names(layout, monkeypatch):
    import importlib.util

    monkeypatch.syspath_prepend(str(layout.root / "src"))
    for name in [m for m in sys.modules if m == "tools" or m.startswith("tools.")]:
        monkeypatch.delitem(sys.modules, name)
    legacy = layout.root / ".local" / "operator" / "freetext_check.py"
    spec = importlib.util.spec_from_file_location("legacy_freetext_check", legacy)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.tells is sys.modules["tools.freetext_check"].tells
    assert Path(sys.modules["tools.freetext_check"].__file__).resolve() == (
        layout.root / "src" / "tools" / "freetext_check.py").resolve()
    assert module.TRANSPARENCY == ft.TRANSPARENCY
    assert module.tells("I am passionate about this.") == ["passionate about"]


# --- writer answer-bank storage boundary ---------------------------------------------

PK = "example-co#job-1"


@pytest.fixture
def writer_world(monkeypatch):
    from agent import graph  # noqa: F401  (import before settings are stubbed)

    puts: list[tuple] = []
    engine = {"value": "chrome"}
    applied = {"result": {}}
    tells_calls: list[str] = []
    rows = {PK: {"status": "tailored"}}

    class Tracking:
        def get(self, pk):
            return dict(rows.get(pk, {}))

        def set_status(self, pk, status, **changes):
            rows.setdefault(pk, {}).update(status=status, **changes)

    class AnswerBank:
        def all_facts(self, company):
            return {"Full name": "Test User"}

        def put(self, *args, **kwargs):
            puts.append((args, kwargs))

    stores = SimpleNamespace(tracking=Tracking(), answer_bank=AnswerBank(),
                             artifacts=SimpleNamespace(), secrets=SimpleNamespace())

    async def fake_apply(*args, **kwargs):
        return dict(applied["result"])

    real_tells = ft.tells

    def spy_tells(text):
        tells_calls.append(text)
        return real_tells(text)

    monkeypatch.setattr("core.stores.make_stores", lambda *a, **kw: stores)
    monkeypatch.setattr("tools.browser_runtime.configuration",
                        lambda: {"engine": engine["value"]})
    monkeypatch.setattr("tools.browser_apply.apply", fake_apply)
    monkeypatch.setattr("tools.submit_hold.blocked", lambda *a, **kw: False)
    monkeypatch.setattr("core.rotation.ensure", lambda *a, **kw: None)
    monkeypatch.setattr("core.profiles.resolve_for", lambda *a, **kw: None)
    monkeypatch.setattr("core.profiles.expand_all", lambda facts: facts)
    monkeypatch.setattr("tools.credentials.get_login", lambda *a, **kw: None)
    monkeypatch.setattr("core.events.emit", lambda *a, **kw: None)
    monkeypatch.setattr("core.config.get_settings",
                        lambda: SimpleNamespace(browser_model="synthetic-model",
                                                local_dir="/nonexistent-synthetic"))
    monkeypatch.setattr(ft, "tells", spy_tells)
    return SimpleNamespace(puts=puts, engine=engine, applied=applied,
                           tells_calls=tells_calls)


async def _apply():
    from agent import graph

    return await graph.apply_to_job(SimpleNamespace(state={
        "pk": PK, "company": "example-co", "jd_url": "https://example.test/jobs/job-1",
        "jd_text": "Synthetic role", "person": "Test User",
    }))


@pytest.mark.asyncio
async def test_writer_tell_positive_answer_not_stored_clean_answer_stored(writer_world):
    from core.models import AnswerScope

    writer_world.applied["result"] = {"status": "applied", "drafted": {
        "Why example-co?": "I am passionate about payments and widgets.",
        "Describe a project": CLEAN,
    }}
    result = await _apply()
    assert result == {"status": "applied"}
    assert writer_world.puts == [(
        ("Describe a project", CLEAN, AnswerScope.COMPANY),
        {"company": "example-co", "source": "writer"},
    )]
    assert set(writer_world.tells_calls) == {
        "I am passionate about payments and widgets.", CLEAN}


@pytest.mark.asyncio
@pytest.mark.parametrize("text,_rule", TELL_CASES[:6])
async def test_writer_every_known_tell_is_excluded(writer_world, text, _rule):
    writer_world.applied["result"] = {"status": "applied", "drafted": {"Q": text}}
    await _apply()
    assert writer_world.puts == []


@pytest.mark.asyncio
async def test_writer_no_draft_stores_nothing_and_checks_nothing(writer_world):
    writer_world.applied["result"] = {"status": "applied"}
    assert await _apply() == {"status": "applied"}
    assert writer_world.puts == [] and writer_world.tells_calls == []


@pytest.mark.asyncio
async def test_writer_browser_skill_engine_branch_unchanged(writer_world):
    writer_world.engine["value"] = "browser_skill"
    result = await _apply()
    assert result["status"] == "gate" and result["reason"] == "leased_dispatch_required"
    assert writer_world.puts == [] and writer_world.tells_calls == []


@pytest.mark.asyncio
async def test_writer_storage_never_runs_unsupported_claim_check(writer_world, monkeypatch):
    monkeypatch.setattr(ft, "check", lambda *a, **kw: pytest.fail("claim check invoked"))
    monkeypatch.setattr(ft, "_sources", lambda *a, **kw: pytest.fail("claim sources read"))
    writer_world.applied["result"] = {"status": "applied", "drafted": {"Q": CLEAN}}
    await _apply()
    assert len(writer_world.puts) == 1


# --- tailored résumé save boundary ---------------------------------------------------

PRE = r"\documentclass{article}" + "\n" + r"\begin{document}" + "\n"
ROLE = r"\role{example-co}{Role A}{2022--2024}"


def doc(bullet=r"\resumeItem{Synthetic work for Test User.}"):
    return PRE + ROLE + "\n" + bullet + "\n" + r"\end{document}"


@pytest.fixture
def tailor(monkeypatch, tmp_path):
    from agent import graph
    from agent import run as run_module
    from core import profiles
    from core import stores as stores_module
    from tools import render

    writes: dict = {}
    row = {"status": "tailored", "resume_s3_key": "resumes/previous.pdf"}
    status_calls: list = []
    tells_calls: list[str] = []

    class Tracking:
        def get(self, pk):
            return dict(row)

        def set_status(self, pk, status, **changes):
            status_calls.append((status, changes))
            row.update(status=status, **changes)

    class Artifacts:
        def put(self, bucket, name, contents, media_type):
            writes[f"{bucket}/{name}"] = contents
            return f"{bucket}/{name}"

    stores = SimpleNamespace(tracking=Tracking(), artifacts=Artifacts())
    monkeypatch.setattr(stores_module, "make_stores", lambda: stores)
    monkeypatch.setattr(graph, "get_settings",
                        lambda: SimpleNamespace(local_dir=str(tmp_path)))
    monkeypatch.setattr(profiles, "resolve_for", lambda row: object())
    monkeypatch.setattr(profiles, "apply_to_latex", lambda tex, profile: tex)
    monkeypatch.setattr(run_module, "seed_fingerprint", lambda: "synthetic-seed")
    monkeypatch.setattr(render, "render_pdf", lambda tex: b"%PDF-synthetic")
    real_tells = ft.tells

    def spy_tells(text):
        tells_calls.append(text)
        return real_tells(text)

    monkeypatch.setattr(ft, "tells", spy_tells)
    monkeypatch.setattr(ft, "check", lambda *a, **kw: pytest.fail("claim check invoked"))
    monkeypatch.setattr(ft, "_sources", lambda *a, **kw: pytest.fail("claim sources read"))

    def save(base, tailored):
        context = SimpleNamespace(state={"base_latex": base, "pk": "synthetic"})
        return graph.save_tailored_resume(tailored, context), context.state

    return SimpleNamespace(save=save, row=row, writes=writes, status_calls=status_calls,
                           tells_calls=tells_calls, profiles=profiles, render=render)


def _assert_rejected_without_writes(t, result, state, rule):
    assert result["ok"] is False and result["reason"] == "agent_tells"
    assert rule in result["tells"] and rule in result["message"]
    assert "re-save" in result["message"]
    assert t.writes == {} and t.status_calls == []
    assert t.row["resume_s3_key"] == "resumes/previous.pdf"
    assert "tailored" not in state and "resume_s3_key" not in state


def test_tailor_tell_positive_candidate_never_persisted(tailor):
    seed = doc()
    tailored = doc(r"\resumeItem{Synthetic work for Test User; I leverage data.}")
    result, state = tailor.save(seed, tailored)
    _assert_rejected_without_writes(tailor, result, state, "leverage")


def test_tailor_tell_introduced_by_profile_application_is_rejected(tailor, monkeypatch):
    monkeypatch.setattr(tailor.profiles, "apply_to_latex",
                        lambda tex, profile: tex.replace("Synthetic work",
                                                         "Synthetic work — delivered"))
    result, state = tailor.save(doc(), doc())
    _assert_rejected_without_writes(tailor, result, state, "em dash")
    assert "—" in tailor.tells_calls[-1]


def test_tailor_tell_exposed_by_render_repair_is_rejected(tailor, monkeypatch):
    def render(tex):
        if "&" in tex and r"\&" not in tex:
            raise RuntimeError("synthetic bare special")
        return b"%PDF-repaired"

    seed = doc(r"\resumeItem{A & B for Test User.}")
    monkeypatch.setattr(tailor.render, "render_pdf", render)
    monkeypatch.setattr(tailor.render, "sanitize_latex",
                        lambda tex: tex.replace("A & B", r"A \& B, see my CV"))
    result, state = tailor.save(seed, seed)
    _assert_rejected_without_writes(tailor, result, state, "see my cv")
    assert r"\&" in tailor.tells_calls[-1]


def test_tailor_clean_candidate_follows_existing_success_path(tailor):
    tailored = doc(r"\resumeItem{Synthetic work for Test User, shipped twice.}")
    result, state = tailor.save(doc(), tailored)
    assert result == {"ok": True, "resume_s3_key": "resumes/synthetic.pdf", "format": "pdf"}
    assert tailor.writes["resumes/synthetic.tex"].decode() == tailored
    assert tailor.writes["resumes/synthetic.pdf"] == b"%PDF-synthetic"
    assert state["tailored"] == tailored
    assert tailor.row["resume_s3_key"] == "resumes/synthetic.pdf"
    assert tailor.tells_calls == [tailored]


def test_tailor_existing_validator_failure_precedes_and_stays(tailor):
    seed = doc()
    dropped = PRE + ROLE + "\n" + r"\end{document}"
    result, _ = tailor.save(seed, dropped)
    assert result["ok"] is False and "dropped" in result["message"]
    assert tailor.writes == {} and tailor.tells_calls == []
