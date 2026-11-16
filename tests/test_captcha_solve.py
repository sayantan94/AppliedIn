"""Offline synthetic CAPTCHA contract tests. No browser, network, owner data or Redis.

The Node vm shim executes the actual DETECT_JS and INJECT_JS expressions.
It cannot establish browser layout, cross-origin/CORS behavior, real canvas
pixels/event ordering or deferred network/Submit effects.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "captcha_solve.py"
_spec = importlib.util.spec_from_file_location("captcha_solve", _PATH)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

# Deliberately minimal DOM. Frames, attributes, event stubs and one isolated
# image canvas only; no live Node browser or transport APIs available in vm.
_NODE = r"""
'use strict';
const vm=require('node:vm');
let request=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const counts={callbacks:0,submits:0,fetches:0};
const windows=[],all=[];
function matches(e,selector){
  return selector.split(',').some(raw=>{
    const s=raw.trim();
    if(s==='*')return true;
    if(s[0]==='.')return e.className.split(/\s+/).includes(s.slice(1));
    const m=s.match(/^([\w-]+)?\[([\w-]+)(?:(\^=|\*=|=)"([^"]*)")?(?: i)?\]$/);
    if(m){
      if(m[1]&&e.tagName.toLowerCase()!==m[1])return false;
      const v=e.getAttribute(m[2]);if(v===null)return false;
      if(!m[3])return true;
      const a=s.endsWith(' i]')?v.toLowerCase():v;
      const b=s.endsWith(' i]')?m[4].toLowerCase():m[4];
      return m[3]==='='?a===b:m[3]==='^='?a.startsWith(b):a.includes(b);
    }
    return e.tagName.toLowerCase()===s;
  });
}
class Element{
  constructor(tag,attrs,doc){
    this.tagName=tag.toUpperCase();this.attrs={...attrs};this.ownerDocument=doc;
    this.parentElement=null;this.children=[];this.nodeType=1;
    this.className=String(attrs.class||'');this.value='';this.innerHTML='';
    this.type=attrs.type||'text';this.isConnected=true;
    this.complete=attrs.complete!==false;this.naturalWidth=attrs.naturalWidth||100;
    this.naturalHeight=attrs.naturalHeight||40;
  }
  getAttribute(k){return Object.prototype.hasOwnProperty.call(this.attrs,k)?String(this.attrs[k]):null}
  setAttribute(k,v){this.attrs[k]=String(v)}
  appendChild(e){e.parentElement=this;this.children.push(e);return e}
  querySelectorAll(s){const out=[];const walk=n=>{for(const e of n.children){if(matches(e,s))out.push(e);walk(e)}};walk(this);return out}
  closest(s){for(let n=this;n;n=n.parentElement)if(matches(n,s))return n;return null}
  getBoundingClientRect(){
    const hidden=this.attrs.hidden===true;
    return {width:hidden?0:90,height:hidden?0:50,bottom:hidden?0:100,top:10};
  }
  dispatchEvent(ev){
    if(this.attrs['data-event-clear'])this.value='';
    if(this.attrs['data-event-submit'])this.ownerDocument.emit('submit');
    return true;
  }
}
class Document{
  constructor(){
    this.documentElement=new Element('html',{},this);this.handlers={};
    all.push(this);
  }
  querySelectorAll(s){
    const out=[];
    if(matches(this.documentElement,s))out.push(this.documentElement);
    return out.concat(this.documentElement.querySelectorAll(s));
  }
  querySelector(s){return this.querySelectorAll(s)[0]||null}
  createElement(tag){
    if(tag!=='canvas')return new Element(tag,{},this);
    return {width:0,height:0,getContext:()=>({drawImage:()=>{
      if(request.fixture.tainted)throw Error('tainted')}}),
      toDataURL:()=>request.fixture.canvasURI||'data:image/png;base64,iVBORw0KGgo='};
  }
  addEventListener(event,fn){(this.handlers[event]||=[]).push(fn)}
  removeEventListener(event,fn){this.handlers[event]=(this.handlers[event]||[]).filter(f=>f!==fn)}
  emit(event){counts.submits++;for(const fn of this.handlers[event]||[])fn()}
}
function makeWindow(spec){
  const doc=new Document(),w={document:doc,location:{href:spec.url||'https://example.test/apply'},
    innerHeight:900,getComputedStyle:e=>({display:e.attrs.hidden?'none':'block',
      visibility:'visible',opacity:'1'})};
  windows.push(w);
  if(spec.password)doc.documentElement.appendChild(new Element('input',{type:'password'},doc));
  if(spec.enterprise)w.grecaptcha={enterprise:{}};
  const addFrame=(desc,parent)=>{
    const el=parent.appendChild(new Element('iframe',{src:desc.src||'',title:desc.title||'',
      id:desc.id||''},doc));
    if(desc.crossOrigin)el.contentWindow=null;
    else el.contentWindow=makeWindow(desc);
    return el;
  };
  for(const widget of spec.widgets||[]){
    const name=widget.kind==='recaptcha'?'g-recaptcha':
      widget.kind==='turnstile'?'cf-turnstile':'h-captcha';
    const attrs={class:name,'data-sitekey':widget.sitekey||'',
      'data-size':widget.size||'','data-action':widget.action||'',
      'data-cdata':widget.cdata||'','data-s':widget.s||''};
    if(widget.callback)attrs['data-callback']=widget.callback;
    if(widget.client)attrs['data-client-id']=widget.client;
    const owner=doc.documentElement.appendChild(new Element('div',attrs,doc));
    if(widget.hidden)owner.attrs.hidden=true;
    if(widget.enterprise)w.grecaptcha={enterprise:{}};
    const receiver=widget.kind==='turnstile'?'cf-turnstile-response':'g-recaptcha-response';
    if(widget.receiver!==false){
      owner.appendChild(new Element(widget.kind==='turnstile'?'input':'textarea',
        {name:receiver},doc));
    }
    if(widget.duplicateReceiver)owner.appendChild(new Element('textarea',{name:receiver},doc));
    if(widget.callback){
      w[widget.callback]=(answer)=>{
        counts.callbacks++;
        if(widget.behavior==='throw')throw Error('PRIVATE CALLBACK ERROR');
        if(widget.behavior==='clear'){
          const el=owner.querySelectorAll('input,textarea')[0];if(el)el.value='';
        }
        if(widget.behavior==='navigate')w.location.href='https://example.test/elsewhere';
        if(widget.behavior==='submit')doc.emit('submit');
        if(widget.behavior==='fetch')w.fetch('/synthetic');
        if(widget.behavior==='remove'){
          const el=owner.querySelectorAll('input,textarea')[0];if(el)el.isConnected=false;
        }
      };
    }
    if(widget.provider){
      addFrame({src:widget.provider,crossOrigin:true},owner);
    }
  }
  for(const frame of spec.frames||[])addFrame(frame,doc.documentElement);
  for(const provider of spec.providers||[])addFrame({src:provider.src,title:provider.title||'',
    crossOrigin:true},doc.documentElement);
  for(const im of spec.images||[]){
    const form=doc.documentElement.appendChild(new Element('form',{},doc));
    const img=form.appendChild(new Element('img',{
      class:'captcha',src:im.src||'',complete:im.loaded!==false,
      naturalWidth:im.loaded===false?0:100,naturalHeight:40},doc));
    if(im.hidden)img.attrs.hidden=true;
    if(im.inputs!==0)for(let i=0;i<(im.inputs||1);i++)
      form.appendChild(new Element('input',{name:'captcha'},doc));
    if(im.callback)form.attrs['data-callback']=im.callback;
  }
  w.fetch=(..._args)=>{counts.fetches++;return {ok:true}};
  return w;
}
const top=makeWindow(request.fixture);
const ctx=vm.createContext({window:top,document:top.document,URL,Event:class Event{
  constructor(type,options){this.type=type;this.options=options;}
},atob:s=>Buffer.from(s,'base64').toString('binary')});
function evaluate(expr){return JSON.parse(vm.runInContext(expr,ctx,{timeout:3000}))}
const detection=evaluate(request.detect);
let injection=null;
if(request.inject){
  if(request.drift==='password')windows[windows.length-1].document.documentElement.appendChild(
    new Element('input',{type:'password'},windows[windows.length-1].document));
  if(request.drift==='url')windows[windows.length-1].location.href='https://example.test/changed';
  if(request.drift==='callback'){
    const e=windows[windows.length-1].document.querySelector('.g-recaptcha');
    if(e)e.setAttribute('data-callback','changed_callback');
  }
  if(request.drift==='added_frame'){
    const d=top.document;
    const el=d.documentElement.appendChild(new Element('iframe',{src:'https://example.test/protected/login'},d));
    el.contentWindow=makeWindow({url:'https://example.test/protected/login'});
  }
  const chosen=request.selected||detection.found.find(c=>c.visible)?.id||'none';
  injection=evaluate(request.inject+'('+JSON.stringify(detection)+','+
    JSON.stringify(chosen)+','+JSON.stringify(request.answer||'SYNTHETIC_TOKEN')+')');
}
const values=[];
for(const d of all)for(const e of d.querySelectorAll('input,textarea'))values.push(e.value);
process.stdout.write(JSON.stringify({detection,injection,counts,values}));
"""


def _node(fixture: dict, *, inject: bool = False, drift: str | None = None,
          answer: str = "SYNTHETIC_TOKEN") -> dict:
    if not shutil.which("node"):
        pytest.skip("Node absent: actual DETECT_JS/INJECT_JS tests not executed")
    result = subprocess.run(
        ["node", "--input-type=commonjs", "-e", _NODE],
        input=json.dumps({"fixture": fixture, "detect": cs.DETECT_JS,
                          "inject": cs.INJECT_JS if inject else None,
                          "drift": drift, "answer": answer}),
        text=True, capture_output=True, timeout=10, check=True,
    )
    return json.loads(result.stdout)


def _widget(**overrides):
    return {"url": "https://example.test/apply",
            "widgets": [{"kind": "recaptcha", "sitekey": "K",
                         "callback": "complete", **overrides}]}


def _nested(**widget):
    return {"url": "https://example.test/apply",
            "frames": [{"url": "https://example.test/one",
                        "frames": [_widget(**widget)]}]}


# Existing guard must remain the sole policy authority.
@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/123",
    "https://lnkd.in/a", "https://example.test/login",
    "https://example.test/forgot-password", "file:///tmp/test.html",
])
def test_guard_refuses(url):
    with pytest.raises(cs.CaptchaError) as exc:
        cs.guard(url, False)
    assert exc.value.code == cs.EXIT_REFUSED


def test_guard_refuses_password_field():
    with pytest.raises(cs.CaptchaError) as exc:
        cs.guard("https://example.test/apply", True)
    assert exc.value.code == cs.EXIT_REFUSED


def test_guard_allows_application():
    cs.guard("https://example.test/apply", False)


def test_actual_js_two_nested_frames_enterprise_and_one_callback():
    page = _node(_nested(enterprise=True, s="S1", action="apply",
                  provider="https://www.google.com/recaptcha/enterprise/anchor?k=K&s=S1&sa=apply"),
                 inject=True)
    d, j = page["detection"], page["injection"]
    assert len(d["frames"]) == 3 and len(d["found"]) == 1
    assert cs.pick_challenge(d["found"])["receiverCount"] == 1
    assert cs.build_task(cs.pick_challenge(d["found"]), d["url"]) == {
        "type": "ReCaptchaV2EnterpriseTaskProxyLess",
        "websiteURL": "https://example.test/apply", "websiteKey": "K",
        "enterprisePayload": {"s": "S1"}, "pageAction": "apply"}
    assert j == {"__cs": 1, "result": "injected", "fields": 1, "callbacks": 1, "called": 1}
    assert page["counts"] == {"callbacks": 1, "submits": 0, "fetches": 0}
    assert page["values"].count("SYNTHETIC_TOKEN") == 1


@pytest.mark.parametrize("drift", ["password", "url", "callback", "added_frame"])
def test_actual_js_prewrite_drift_has_zero_effects(drift):
    page = _node(_nested(), inject=True, drift=drift)
    assert page["injection"]["reason"] == "binding_changed"
    assert page["counts"]["callbacks"] == 0
    assert not any(v == "SYNTHETIC_TOKEN" for v in page["values"])


def test_actual_js_same_sitekey_separate_instances_not_merged():
    f = _widget()
    f["widgets"].append({"kind": "recaptcha", "sitekey": "K", "callback": "complete2"})
    page = _node(f)
    with pytest.raises(cs.CaptchaError) as exc:
        cs.pick_challenge(page["detection"]["found"])
    assert exc.value.code == cs.EXIT_UNSUPPORTED


def test_actual_js_keyless_hcaptcha_popup_hidden_anchor():
    f = {"url": "https://example.test/apply", "widgets": [
        {"kind": "hcaptcha", "sitekey": "", "hidden": True, "receiver": False}],
        "providers": [{"src": "https://newassets.hcaptcha.com/captcha/v1/challenge"}]}
    page = _node(f)
    assert any(c["kind"] == "hcaptcha" and c["visible"] for c in page["detection"]["found"])
    with pytest.raises(cs.CaptchaError) as exc:
        cs.pick_challenge(page["detection"]["found"])
    assert exc.value.reason == "unsupported_widget"


def test_actual_js_absent_invisible_and_opaque():
    page = _node({"url": "https://example.test/apply"})
    with pytest.raises(cs.CaptchaError) as exc:
        cs.pick_challenge(page["detection"]["found"])
    assert exc.value.code == cs.EXIT_NONE
    page = _node({"url": "https://example.test/apply",
                  "providers": [{"src": "https://unknown.test/challenge", "title": "captcha"}]})
    assert page["detection"]["opaque"]


def test_actual_js_turnstile_scoped_receiver():
    page = _node({"url": "https://example.test/apply", "widgets": [{
        "kind": "turnstile", "sitekey": "0xAB", "action": "apply",
        "cdata": "CD", "callback": "complete"}]}, inject=True)
    task = cs.build_task(cs.pick_challenge(page["detection"]["found"]), "https://example.test/apply")
    assert task == {"type": "AntiTurnstileTaskProxyLess", "websiteURL": "https://example.test/apply",
                    "websiteKey": "0xAB", "metadata": {"action": "apply", "cdata": "CD"}}
    assert page["injection"]["result"] == "injected"
    assert page["values"].count("SYNTHETIC_TOKEN") == 1


def test_actual_js_duplicate_receiver_refuses():
    page = _node(_widget(duplicateReceiver=True), inject=True)
    assert page["injection"]["reason"] == "binding_changed"
    assert page["counts"]["callbacks"] == 0


@pytest.mark.parametrize(("widget", "reason"), [
    ({"callback": ""}, "callback_missing"),
    ({"callback": "complete", "behavior": "throw"}, "possible_commit"),
    ({"callback": "complete", "behavior": "submit"}, "possible_commit"),
    ({"callback": "complete", "behavior": "navigate"}, "possible_commit"),
    ({"callback": "complete", "behavior": "remove"}, "possible_commit"),
    ({"callback": "complete", "behavior": "clear"}, "response_unverified"),
])
def test_actual_js_callback_outcomes(widget, reason):
    page = _node(_widget(**widget), inject=True)
    assert page["injection"]["reason"] == reason
    assert page["counts"]["callbacks"] <= 1
    # Any submission here is callback-origin, never script-issued.
    assert page["counts"]["submits"] == (1 if widget.get("behavior") == "submit" else 0)


def test_enterprise_conflicting_metadata_refused():
    page = _node(_widget(enterprise=True, s="A",
                 provider="https://www.google.com/recaptcha/enterprise/anchor?k=K&s=B"))
    with pytest.raises(cs.CaptchaError) as exc:
        cs.pick_challenge(page["detection"]["found"])
    assert exc.value.reason == "unsupported_widget"


# One PNG data URI is kept byte-exact. Canvas-based extraction is a re-encoding
# (the shim controls the encoded output, not the original image bytes).
_PNG = b"\x89PNG\r\n\x1a\n" + b"SYNTHETIC"
import base64
_URI = "data:image/png;base64," + base64.b64encode(_PNG).decode()


def test_actual_js_image_data_uri_callback_free_fill_handoff():
    page = _node({"url": "https://example.test/apply", "images": [{"src": _URI}]},
                 inject=True, answer="AB12")
    c = cs.pick_challenge(page["detection"]["found"])
    task = cs.build_task(c, "https://example.test/apply")
    assert task == {"type": "ImageToTextTask", "body": base64.b64encode(_PNG).decode()}
    assert base64.b64decode(task["body"]) == _PNG
    assert page["injection"]["reason"] == "callback_missing"
    assert "AB12" in page["values"]


def test_actual_js_image_isolated_canvas_reencode_and_refusals():
    f = {"url": "https://example.test/apply", "images": [{"src": "https://example.test/captcha.png"}],
         "canvasURI": _URI}
    page = _node(f)
    task = cs.build_task(cs.pick_challenge(page["detection"]["found"]), f["url"])
    assert task["body"] == base64.b64encode(_PNG).decode()
    for overrides in [{"tainted": True}, {"images": [{"src": _URI, "loaded": False}]},
                      {"images": [{"src": _URI, "inputs": 2}]}]:
        data = dict(f, **overrides)
        bad = _node(data)
        with pytest.raises(cs.CaptchaError) as exc:
            cs.pick_challenge(bad["detection"]["found"])
        assert exc.value.code == cs.EXIT_UNSUPPORTED


def test_image_body_oversize_rejected_before_payment():
    with pytest.raises(cs.CaptchaError) as exc:
        cs.build_task({"kind": "image", "body": base64.b64encode(_PNG + b"x"*262144).decode()}, "url")
    assert exc.value.reason == "image_unreadable"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(replies, calls):
    def open_req(req, timeout):
        calls.append((req.full_url, json.loads(req.data)))
        return _Resp(json.dumps(replies.pop(0)).encode())
    return open_req


def test_capsolver_provider_payloads_error_and_image_text():
    calls = []
    task = {"type": "ImageToTextTask", "body": base64.b64encode(_PNG).decode()}
    replies = [{"errorId": 0, "taskId": "T", "status": "ready", "solution": {"text": "AB12"}}]
    assert cs.solve(task, "KEY", opener=_opener(replies, calls)) == ("T", "AB12")
    assert calls[0][1]["task"] == task
    replies = [{"errorId": 1, "errorCode": "ERROR_1001",
                "errorDescription": "PRIVATE DATA DO NOT LOG"}]
    with pytest.raises(cs.CaptchaError) as exc:
        cs.solve({"type": "ReCaptchaV2EnterpriseTaskProxyLess"}, "KEY",
                 opener=_opener(replies, []))
    assert exc.value.code == cs.EXIT_ERROR and exc.value.reason == "provider_error"


def test_capsolver_poll_and_missing_solution():
    calls = []
    replies = [{"errorId": 0, "taskId": "T"},
               {"errorId": 0, "status": "processing"},
               {"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": "TKN"}}]
    assert cs.solve({"type": "ReCaptchaV2TaskProxyLess"}, "KEY",
                    opener=_opener(replies, calls), sleep=lambda _: None) == ("T", "TKN")
    with pytest.raises(cs.CaptchaError) as exc:
        cs._token({"text": "not a token"}, "ReCaptchaV2TaskProxyLess")
    assert exc.value.reason == "solution_missing"


def test_run_guards_nested_frame_before_provider(tmp_path, monkeypatch):
    detected = _node(_nested())["detection"]
    detected["frames"][-1]["url"] = "https://example.test/login"
    monkeypatch.setattr(cs, "evaluate", lambda *a, **kw: detected)
    monkeypatch.setattr(cs, "solve", lambda *a, **kw: pytest.fail("paid task must not run"))
    code, log = cs.run("S", None, False, tmp_path)
    assert (code, log["reason"]) == (2, "protected_page")


def test_run_fails_closed_after_ambiguous_callback(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSOLVER_API_KEY", "KEY")
    detected = _node(_widget(callback=""))["detection"]
    def eval_fake(session, tab, expr):
        if expr == cs.DETECT_JS:
            return detected
        return _node(_widget(callback=""), inject=True)["injection"]
    monkeypatch.setattr(cs, "evaluate", eval_fake)
    monkeypatch.setattr(cs, "solve", lambda *a: ("T", "VERY_PRIVATE_TOKEN"))
    code, log = cs.run("S", None, False, tmp_path)
    assert code == 4 and log["reason"] == "callback_missing"
    assert "VERY_PRIVATE_TOKEN" not in json.dumps(log)


def test_run_dry_run_is_not_injected(tmp_path, monkeypatch):
    detected = _node(_widget())["detection"]
    monkeypatch.setattr(cs, "evaluate", lambda *a: detected)
    monkeypatch.setattr(cs, "solve", lambda *a: pytest.fail("dry run must not solve"))
    code, log = cs.run("S", None, True, tmp_path)
    assert code == 0 and log["result"] == "dry_run"


def test_evaluate_preserves_own_session_context():
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        if argv[1:3] == ["cooperate", "sync"]:
            data = {"context_id": "C", "observation_id": "O"}
        else:
            data = {"value": json.dumps({"__cs": 1, "url": "safe"})}
        return SimpleNamespace(returncode=0, stdout=json.dumps(data), stderr="")

    assert cs.evaluate("S", "TAB", "1+1", runner=runner)["url"] == "safe"
    assert calls[1][calls[1].index("--context")+1] == "C"
    assert calls[1][calls[1].index("--observation")+1] == "O"
    assert calls[1][calls[1].index("--tab-id")+1] == "TAB"


def test_key_and_balance_utility_compat(tmp_path, monkeypatch):
    monkeypatch.delenv("CAPSOLVER_API_KEY", raising=False)
    (tmp_path / "secrets.json").write_text(json.dumps({"other": "ok"}))
    cs.save_api_key(tmp_path, "  TEST_KEY ")
    assert cs.load_api_key(tmp_path) == "TEST_KEY"
    assert json.loads((tmp_path / "secrets.json").read_text())["other"] == "ok"
    assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600
    calls = []
    assert cs.balance("TEST_KEY", opener=_opener([{"errorId": 0, "balance": 12.5}], calls)) == 12.5
