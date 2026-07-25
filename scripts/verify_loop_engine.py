#!/usr/bin/env python3
"""Drive the loop engine against a real posting with a FAKE model — zero cost.

The model's judgment is the one part this can't check, so it stands in a scripted
one: read the form, fill what it sees, try a disability affirmation and a sanctions
"Yes" on the way past. Everything else is the real thing — real Chrome, the real
tools, the real guards. What it proves is that the loop's plumbing works and that
a model cannot write what the tool layer refuses, which is the whole reason this
engine exists.

    .venv/bin/python scripts/verify_loop_engine.py [url]
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

ARGS = [a for a in sys.argv[1:] if not a.startswith("-")]
REAL = "--real" in sys.argv          # use the configured model instead of the fake one
URL = ARGS[0] if ARGS else (
    "https://jobs.ashbyhq.com/openai/2cba0d45-7a4f-4f38-ac73-3f8633bf0349")


class _Call:
    def __init__(self, name, args, i):
        self.id = f"call_{i}"
        self.function = type("F", (), {"name": name, "arguments": json.dumps(args)})()


class _Msg:
    def __init__(self, calls):
        self.tool_calls = calls
        self.content = ""

    def model_dump(self):
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.function.name,
                                             "arguments": c.function.arguments}}
                               for c in (self.tool_calls or [])]}


async def main() -> int:
    import tools.apply_loop as loop

    seen: dict = {}
    turns = [
        [_Call("read_form", {}, 0)],
        [_Call("fill_fields", {"values": {
            "Legal Name": "Alex Kim",
            "Email": "alex@example.com",
            "Cover letter": "{{DRAFT_ESSAY_ANSWER}}",          # must be refused
            "Yes, I have a disability, or have had one in the past": "on",  # refused
        }}, 1)],
        [_Call("select_choices", {"answers": {
            "Are you authorized to work in the country where the job is located?": "Yes",
            "Are you a citizen of, or located in, Cuba, Iran, North Korea, or Syria?": "Yes",
        }}, 2)],
        [_Call("read_form", {}, 3)],
        [],                       # stop before submitting — never send a fake application
    ]

    async def fake_acompletion(model=None, messages=None, tools=None, **kw):  # noqa: ANN001
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") == "tool":
                seen.setdefault(m["name"], []).append(m["content"])
        calls = turns.pop(0) if turns else []
        return type("R", (), {"choices": [type("C", (), {"message": _Msg(calls)})()]})()

    if not REAL:
        import litellm
        litellm.acompletion = fake_acompletion
    else:
        # Real model, real judgment — but STOPPED at the submit. An engine's first
        # live run should be judged on how it filled the form, and submitting is
        # not reversible.
        import tools.browser_apply as ba

        async def _no_submit(*a, **k):  # noqa: ANN001, ANN002, ANN003
            page = a[0]
            shot = Path(".local/loop-dry-run.png")
            shot.parent.mkdir(parents=True, exist_ok=True)
            shot.write_bytes(await page.screenshot(full_page=True))
            state = await page.evaluate(ba._READ_FORM_JS)
            filled = [(f.get("label"), f.get("value")) for f in state
                      if f.get("value") not in (None, "", False)]
            print("\n--- WOULD SUBMIT, with these values ---")
            for lbl, val in filled:
                print(f"   {str(lbl)[:52]:54s} = {str(val)[:44]}")
            missing = [f.get("label") for f in state if f.get("required")
                       and not f.get("value") and f.get("type") not in ("file",)]
            if missing:
                print("\n   still empty (required):", [str(m)[:44] for m in missing][:6])
            print(f"\n   screenshot: {shot}")
            return {"status": "dry-run", "detail": f"{len(filled)} field(s) filled; "
                                                   f"{len(missing)} required still empty"}

        ba._finalize_submit = _no_submit
        loop._finalize_submit = _no_submit

    print(f"driving the loop against {URL}\n")
    facts, model, resume, pk = {"Legal Name": "Alex Kim",
                                "Email": "alex@example.com"}, "fake/model", "", ""
    if REAL:
        from core.config import get_settings
        from core import profiles as _prof
        from core.stores import make_stores
        st = make_stores()
        row = next((r for r in st.tracking.all()
                    if r.get("jd_url") and r["jd_url"].split("?")[0].rstrip("/") in URL), {})
        pk = row.get("pk", "")
        facts = st.answer_bank.all_facts(row.get("company") or "OpenAI")
        p_ = _prof.resolve(row.get("profile_id", ""))
        if p_:
            facts = p_.override(facts)
            print(f"applying as: {p_.label} <{p_.email}>")
        facts = _prof.expand_all(facts)
        key = row.get("resume_s3_key")
        if key:
            src = Path(".local/artifacts") / key
            if src.exists():
                import tempfile
                name = (facts.get("Full name") or "Resume") + ".pdf"
                dst = Path(tempfile.gettempdir()) / "appliedin_uploads" / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
                resume = str(dst)
        model = get_settings().litellm_model
        print(f"model: {model}  |  facts: {len(facts)}  |  résumé: {Path(resume).name if resume else 'none'}\n")

    result = await loop.apply_loop(URL, "OpenAI", facts, model, pk=pk, resume_path=resume)

    if REAL:
        # The assertions below check the FAKE model's deliberate violations; a real
        # run is judged by the values it would submit, printed above.
        print("\nDRY RUN — nothing was submitted. Review the values, then apply for real.")
        return 0

    ok = True
    fills = json.loads(seen.get("fill_fields", ["{}"])[0])
    print("FILL   kept   :", fills.get("filled"))
    for r in fills.get("refused", []):
        print("       refused:", r)
    if "Legal Name" not in (fills.get("filled") or []):
        print("  ✗ ordinary field was not filled"); ok = False
    if len(fills.get("refused") or []) != 2:
        print("  ✗ expected the placeholder AND the self-declaration to be refused"); ok = False

    ch = json.loads(seen.get("select_choices", ["{}"])[0])
    print("\nCHOICE set    :", ch.get("set"))
    for r in ch.get("refused", []):
        print("       refused:", r)
    if not any("safe option" in r for r in (ch.get("refused") or [])):
        print("  ✗ the sanctions 'Yes' was not forced to the safe option"); ok = False

    n = len(json.loads(seen.get("read_form", ["[]"])[0] or "[]"))
    print(f"\nFORM   fields read: {n}")
    print("RESULT:", result.get("status"), "-", str(result.get("detail"))[:70])
    print("\n" + ("PASS — the loop drove a real form and the guards held"
                  if ok else "FAIL — see ✗ above"))
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
