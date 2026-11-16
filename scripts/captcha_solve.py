#!/usr/bin/env python3
"""Solve a visible CAPTCHA on a board agent's application tab through CapSolver.

Owner decision 2026-11-08: board agents may pass a visible CAPTCHA on any job
site with CapSolver, except LinkedIn and login / sign-up / password pages.
Those exclusions are enforced here, in code, not in the agent brief.

Usage (board agent, own bsk session only):

    .venv/bin/python scripts/captcha_solve.py --session <SID> [--tab-id <TAB>] [--dry-run]

Flow: `bsk cooperate sync` + `bsk evaluate` to find the widget, guard checks,
CapSolver createTask/getTaskResult (token API only, nothing but the page URL
and site key, optional widget metadata or isolated image bytes), then a second sync + evaluate that puts
the token into the response field and calls the widget callback only when
exactly one callback exists. It never clicks Submit.

Exit codes:
    0  injected and one matching callback confirmed (or dry-run/utility mode)
    2  refused by guard (LinkedIn, login/sign-up/password page)
    3  no visible challenge on the page (nothing to do)
    4  unsupported or ambiguous widget (hand the tab to the owner)
    5  solver or browser error (hand the tab to the owner)

Every run appends one line (no token) to <local_dir>/captcha-log.jsonl.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

API = "https://api.capsolver.com"
POLL_SECONDS = 3.0
POLL_TIMEOUT = 150.0

EXIT_OK, EXIT_REFUSED, EXIT_NONE, EXIT_UNSUPPORTED, EXIT_ERROR = 0, 2, 3, 4, 5

BLOCKED_HOSTS = ("linkedin.com", "lnkd.in", "licdn.com")
BLOCKED_URL = re.compile(
    r"(log-?in|sign-?in|sign-?up|register|create-?account|/auth\b|/sso\b|password|forgot)",
    re.IGNORECASE,
)

# Both evaluations use the *same* scanner: no policy blocklist is copied into JS.
# Only Python guard() decides protected URLs/passwords. Snapshot equality
# prevents a stale, previously approved binding from mutating a changed page.
SCAN_JS = r"""(()=>{
 const out={__cs:1,url:String(window.location.href),password:false,frames:[],found:[],opaque:[]};
 const handles=Object.create(null), docs=[], wins=[];
 // SHA-256 of the base64 body lets the approval cross the CLI boundary
 // without passing a 256 KiB image back as a single process argument.
 const bodyHash=text=>{
   const k=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
     0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
     0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
     0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
     0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
     0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
     0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
     0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2];
   const bits=text.length*8,bytes=Array.from(text,c=>c.charCodeAt(0));
   bytes.push(128);while(bytes.length%64!==56)bytes.push(0);
   const hi=Math.floor(bits/4294967296),lo=bits>>>0;
   for(let i=3;i>=0;i--)bytes.push((hi>>>(i*8))&255);
   for(let i=3;i>=0;i--)bytes.push((lo>>>(i*8))&255);
   const h=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
     0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19];
   const rot=(x,n)=>(x>>>n)|(x<<(32-n));
   for(let offset=0;offset<bytes.length;offset+=64){
     const w=new Array(64);
     for(let t=0;t<16;t++){
       const i=offset+t*4;
       w[t]=((bytes[i]<<24)|(bytes[i+1]<<16)|(bytes[i+2]<<8)|bytes[i+3])>>>0;
     }
     for(let t=16;t<64;t++){
       const a=w[t-15],b=w[t-2];
       const s0=rot(a,7)^rot(a,18)^(a>>>3);
       const s1=rot(b,17)^rot(b,19)^(b>>>10);
       w[t]=(w[t-16]+s0+w[t-7]+s1)>>>0;
     }
     let [a,b,c,d,e,f,g,z]=h;
     for(let t=0;t<64;t++){
       const s1=rot(e,6)^rot(e,11)^rot(e,25);
       const choose=(e&f)^(~e&g);
       const t1=(z+s1+choose+k[t]+w[t])>>>0;
       const s0=rot(a,2)^rot(a,13)^rot(a,22);
       const majority=(a&b)^(a&c)^(b&c);
       const t2=(s0+majority)>>>0;
       z=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;
     }
     for(const [i,v] of [a,b,c,d,e,f,g,z].entries())h[i]=(h[i]+v)>>>0;
   }
   return h.map(v=>v.toString(16).padStart(8,'0')).join('');
 };

 const key=e=>{const d=e.ownerDocument;return Array.prototype.indexOf.call(d.querySelectorAll('*'),e)};
 const style=(w,e)=>{try{return w.getComputedStyle(e)}catch(_){return {display:'none'}}};
 const shown=(w,e)=>{
   if(!e||typeof e.getBoundingClientRect!=='function')return false;
   let r;try{r=e.getBoundingClientRect()}catch(_){return false}
   if(!r||r.width<=20||r.height<=20||r.bottom<=0||r.top>=w.innerHeight+2000)return false;
   for(let n=e;n&&n.nodeType===1;n=n.parentElement){
     const s=style(w,n);
     if(s.display==='none'||s.visibility==='hidden'||Number(s.opacity)===0)return false;
   }
   return true;
 };
 const attr=(e,n)=>e&&typeof e.getAttribute==='function'?String(e.getAttribute(n)||'').trim():'';
 const kindOf=e=>{const c=String(e.className||'');
   return /(?:^|\s)h-captcha(?:\s|$)/.test(c)?'hcaptcha':
   /(?:^|\s)cf-turnstile(?:\s|$)/.test(c)?'turnstile':
   /(?:^|\s)g-recaptcha(?:\s|$)/.test(c)?'recaptcha':''};
 const provider=u=>{
   const h=u.hostname,p=u.pathname;
   if(/(^|\.)(google\.com|recaptcha\.net)$/.test(h)&&p.includes('/recaptcha/'))
     return {kind:'recaptcha',sitekey:u.searchParams.get('k')||'',enterprise:p.includes('/enterprise/'),
       frame:p.includes('/bframe')?'bframe':p.includes('/anchor')?'anchor':'other',
       size:u.searchParams.get('size')||'',s:u.searchParams.get('s')||'',action:u.searchParams.get('sa')||''};
   if(/(^|\.)hcaptcha\.com$/.test(h)){
     const m=u.hash.match(/sitekey=([^&]+)/);return {kind:'hcaptcha',sitekey:u.searchParams.get('sitekey')||(m&&m[1])||'',
       frame:p.includes('challenge')||p.includes('bframe')?'bframe':'anchor'};}
   if(h==='challenges.cloudflare.com'){
     const m=p.match(/\/(0x[\w-]+)\//);return {kind:'turnstile',sitekey:(m&&m[1])||'',frame:'anchor',
       action:u.searchParams.get('action')||'',cdata:u.searchParams.get('cdata')||''};}
   return null;
 };
 const callbacks=(w,owner,receiver)=>{
   const refs=[], hints=[];
   const add=(label,fn)=>{if(typeof fn==='function'&&!refs.some(x=>x.fn===fn))refs.push({label,fn})};
   for(const e of [owner,receiver]){
     const name=attr(e,'data-callback');
     if(name){hints.push(name);add('named:'+name,w[name]);}
   }
   const client=attr(owner,'data-client-id');
   if(client&&w.___grecaptcha_cfg&&w.___grecaptcha_cfg.clients){
     const root=w.___grecaptcha_cfg.clients[client],seen=new Set();
     const walk=(o,path,d)=>{
       if(!o||typeof o!=='object'||d>5||seen.has(o))return;seen.add(o);
       for(const k of Object.keys(o)){
         let v;try{v=o[k]}catch(_){continue}
         if(k==='callback')add('client:'+client+':'+path+'.'+k,typeof v==='string'?w[v]:v);
         else if(v&&typeof v==='object')walk(v,path+'.'+k,d+1);
       }
     };walk(root,'root',0);
   }
   return {refs,hints};
 };
 // This WeakMap preserves callable reference identity between evaluations in
 // the same page realm. Losing the realm/map changes the nonce and fails closed;
 // labels and source text alone cannot distinguish replacement closures.
 const callbackWitness=(w,fn)=>{
   const marker=Symbol.for('appliedin.captcha.callback-witness.v1');
   try{
     let state=w[marker];
     if(!state){
       state={ids:new WeakMap(),next:0,
         nonce:Math.random().toString(36).slice(2)+Math.random().toString(36).slice(2)};
       w[marker]=state;
       if(w[marker]!==state)return '';
     }
     if(!state.ids||typeof state.ids.get!=='function'||!state.nonce)return '';
     let id=state.ids.get(fn);
     if(!id){
       id=state.nonce+':'+(++state.next);
       state.ids.set(fn,id);
     }
     return id;
   }catch(_){return ''}
 };
 // Document-order indices (key()) cannot detect a same-shape replacement.
 // This page-realm WeakMap gives every DOM node a stable identity, so a
 // replaced owner/receiver/image/frame fails the pre-write snapshot compare
 // even when its position and attributes still match.
 const nodeWitness=(w,e)=>{
   if(!e||(typeof e!=='object'&&typeof e!=='function'))return '';
   const marker=Symbol.for('appliedin.captcha.node-witness.v1');
   try{
     let state=w[marker];
     if(!state){
       state={ids:new WeakMap(),next:0,
         nonce:Math.random().toString(36).slice(2)+Math.random().toString(36).slice(2)};
       w[marker]=state;
       if(w[marker]!==state)return '';
     }
     if(!state.ids||typeof state.ids.get!=='function'||!state.nonce)return '';
     let id=state.ids.get(e);
     if(!id){
       id=state.nonce+':'+(++state.next);
       state.ids.set(e,id);
     }
     return id;
   }catch(_){return ''}
 };
 const markCallbacks=(c,cb,w)=>{
   c.callbacks=cb.refs.map(x=>x.label);
   c.callbackHints=cb.hints;
   c.callbackWitnesses=cb.refs.map(x=>callbackWitness(w,x.fn));
   if(c.callbackWitnesses.some(x=>!x))c.invalid=true;
 };
 const add=(c,h)=>{out.found.push(c);handles[c.id]=h};
 const visit=(w,d,path,visible)=>{
   const frame={path,url:String(w.location.href),password:!!d.querySelector('input[type=password]')};
   out.frames.push(frame);docs.push(d);wins.push(w);
   if(path==='top')out.password=frame.password;
   const owners=[];
   d.querySelectorAll('[data-sitekey],.g-recaptcha,.cf-turnstile,.h-captcha').forEach(e=>{
     const kind=kindOf(e);if(!kind)return;
     const id=path+'|owner:'+key(e),c={id,kind,sitekey:attr(e,'data-sitekey'),visible:visible&&shown(w,e),
       size:attr(e,'data-size'),enterprise:!!(w.grecaptcha&&w.grecaptcha.enterprise),
       frame:'',action:attr(e,'data-action'),cdata:attr(e,'data-cdata'),s:attr(e,'data-s'),
       ownerWitness:nodeWitness(w,e),receiverWitness:'',
       invalid:false,providerFrames:[],receiver:'',receiverCount:0,callbacks:[],callbackHints:[]};
     if(!c.ownerWitness)c.invalid=true;
     owners.push({c,e,w,d});add(c,{owner:e,w,d});
   });
   const iframes=Array.from(d.querySelectorAll('iframe'));
   iframes.forEach((f,i)=>{
     const src=attr(f,'src'),fpath=path+'/iframe:'+i+':'+attr(f,'id')+':'+attr(f,'name')+'|src:'+src;
     let u;try{u=new URL(src,String(w.location.href))}catch(_){u=null}
     const p=u?provider(u):null;
     if(p){
       const compatible=o=>o.c.kind===p.kind&&(!p.sitekey||!o.c.sitekey||o.c.sitekey===p.sitekey);
       const near=typeof f.closest==='function'?f.closest('.g-recaptcha,.cf-turnstile,.h-captcha'):null;
       // Only an observed owner association or a previously seen virtual
       // anchor proves this provider frame belongs to a widget. A detached
       // frame with the same kind/sitekey is a distinct, ambiguous challenge,
       // never evidence for the sole matching owner.
       let target=near?owners.find(o=>o.e===near&&compatible(o)):null;
       if(!target){
         // Attach a popup to one unowned anchor, but never merge separate anchors.
         const virtual=owners.filter(o=>o.c.kind===p.kind&&o.c.virtual&&o.c.sitekey===p.sitekey);
         if(p.frame==='bframe'&&virtual.length===1)target=virtual[0];
       }
       if(!target){
         const c={id:path+'|provider:'+i,kind:p.kind,sitekey:p.sitekey,visible:false,
           size:p.size||'',enterprise:!!p.enterprise,frame:p.frame||'',action:p.action||'',
           cdata:p.cdata||'',s:p.s||'',ownerWitness:'',receiverWitness:'',
           invalid:false,providerFrames:[],receiver:'',
           receiverCount:0,callbacks:[],callbackHints:[]};
         target={c,e:null,w,d,virtual:true};owners.push(target);add(c,{owner:null,w,d});
       }
       const c=target.c,vis=visible&&shown(w,f);
       const frameWitness=nodeWitness(w,f);
       if(!frameWitness)c.invalid=true;
       // A bframe can accompany its anchor, but two distinct anchors are
       // distinct instances even when their provider and sitekey agree.
       if(p.frame==='anchor'&&c.providerFrames.some(frame=>frame.frame==='anchor'&&frame.path!==fpath))
         c.invalid=true;
       c.providerFrames.push({path:fpath,src:String(u.href),visible:vis,frame:p.frame||'',witness:frameWitness});
       c.visible=c.visible||vis && (p.frame!=='anchor'||c.size!=='invisible');
       c.enterprise=!!(c.enterprise||p.enterprise);
       if(!c.sitekey)c.sitekey=p.sitekey;
       else if(p.sitekey&&c.sitekey!==p.sitekey)c.invalid=true;
       for(const name of ['s','action','cdata']){
         if(p[name]&&c[name]&&c[name]!==p[name])c.invalid=true;
         else if(p[name])c[name]=p[name];
       }
       if(p.frame==='bframe')c.frame='bframe';
       else if(!c.frame)c.frame=p.frame||'';
       return;
     }
     let cw=null,cd=null;
     try{cw=f.contentWindow;cd=cw&&cw.document}catch(_){}
     if(!cw||!cd){
       if(visible&&shown(w,f)&&/captcha|challenge|verification/i.test(src+' '+attr(f,'title')+' '+attr(f,'id')))
         out.opaque.push(fpath);
     }else{
       // Visibility of the embedding iframe applies to everything inside it;
       // a checker cannot see a challenge behind an opacity:0/offscreen frame.
       const fvisible=visible&&shown(w,f);
       try{visit(cw,cd,fpath,fvisible)}catch(_){out.opaque.push('scan_error:'+fpath)}
     }
   });
   // Bind receivers only within this document and only to an unambiguous owner.
   for(const o of owners){
     const c=o.c,sel=c.kind==='turnstile'?'input[name="cf-turnstile-response"],textarea[name="cf-turnstile-response"]':
       c.kind==='recaptcha'?'textarea[name="g-recaptcha-response"],textarea[id^="g-recaptcha-response"]':'';
     if(!sel)continue;
     let receivers=o.e?Array.from(o.e.querySelectorAll(sel)):[];
     if(!receivers.length&&o.e){
       // A response field outside the owner is only attributable when it
       // shares the owner's form/container. A document-wide sole field is
       // often a stale field from an unrelated form.
       const container=typeof o.e.closest==='function'?o.e.closest('form,[data-captcha-container]'):null;
       if(container)receivers=Array.from(container.querySelectorAll(sel));
     }
     c.receiverCount=receivers.length;
     if(receivers.length===1){
       c.receiver=path+'|field:'+key(receivers[0]);
       c.receiverWitness=nodeWitness(w,receivers[0]);
       if(!c.receiverWitness)c.invalid=true;
       const cb=callbacks(w,o.e,receivers[0]);
       markCallbacks(c,cb,w);
       Object.assign(handles[c.id],{receiver:receivers[0],callbacks:cb.refs});
     }
   }
   // Text/image CAPTCHA: only an explicitly identifiable img and one nearby answer field.
   const images=Array.from(d.querySelectorAll('img[data-captcha],img.captcha,img[id*="captcha" i],img[alt*="captcha" i]'));
   // The wrapper can identify the CAPTCHA even when the contained img has no marker.
   d.querySelectorAll('.captcha-image').forEach(wrapper=>{
     for(const img of wrapper.querySelectorAll('img'))
       if(!images.includes(img))images.push(img);
   });
   images.forEach(img=>{
     if(!visible||!shown(w,img))return;
     const owner=typeof img.closest==='function'?img.closest('form,[data-captcha-container]'):null;
     const inputSel='input[data-captcha-answer],input[name="captcha"],input[name="captcha_answer"],input[id*="captcha" i]';
     // A CSS-hidden or read-only decoy is not the answer receiver the operator
     // sees; binding it would pay for and write a field the challenge excludes.
     const answers=Array.from((owner||img.parentElement||d).querySelectorAll(inputSel))
       .filter(e=>e!==img&&e.type!=='hidden'&&e.type!=='password'&&!e.disabled&&!e.readOnly&&shown(w,e));
     const id=path+'|image:'+key(img),c={id,kind:'image',sitekey:'',visible:true,frame:'',
       receiver:'',receiverCount:answers.length,callbacks:[],callbackHints:[],body:'',
       ownerWitness:nodeWitness(w,img),receiverWitness:'',
       imageStatus:'image_unreadable',src:attr(img,'src'),bodyHash:'',invalid:false};
     if(!c.ownerWitness)c.invalid=true;
     const h={owner:owner||img.parentElement,w,d};
     if(answers.length===1){
       c.receiver=path+'|field:'+key(answers[0]);h.receiver=answers[0];
       c.receiverWitness=nodeWitness(w,answers[0]);
       if(!c.receiverWitness)c.invalid=true;
       const cb=callbacks(w,owner||img.parentElement,answers[0]);
       markCallbacks(c,cb,w);h.callbacks=cb.refs;
     }
     const width=Number(img.naturalWidth),height=Number(img.naturalHeight);
     // Bound canvas allocation before creating a canvas, regardless of
     // how small the compressed image file or displayed CSS box may be.
     const safePixels=Number.isSafeInteger(width)&&Number.isSafeInteger(height)&&
       width>0&&height>0&&width<=4096&&height<=4096&&width*height<=4194304;
     if(answers.length===1&&img.complete&&safePixels){
       try{
         let uri=c.src;
         if(!/^data:/i.test(uri)){
           const canvas=d.createElement('canvas');
           canvas.width=width;canvas.height=height;
           canvas.getContext('2d').drawImage(img,0,0);
           uri=canvas.toDataURL('image/png');
         }
         const m=uri.match(/^data:image\/(png|jpeg);base64,([a-z0-9+/]+={0,2})$/i);
         if(m&&m[2].length<=Math.ceil(262144/3)*4+4){
           const raw=atob(m[2]),png=raw.slice(0,8)==='\x89PNG\r\n\x1a\n';
           const jpeg=raw.charCodeAt(0)===255&&raw.charCodeAt(1)===216&&raw.charCodeAt(2)===255;
           if(raw.length>0&&raw.length<=262144&&(m[1].toLowerCase()==='png'?png:jpeg)){
             c.body=m[2];c.bodyHash=bodyHash(c.body);c.imageStatus='ok';
           }
         }
       }catch(_){}
     }
     add(c,h);
   });
   // Canvas-source and CSS-only image challenges must not be "no challenge".
   d.querySelectorAll('canvas[data-captcha],canvas.captcha,canvas[id*="captcha" i],.captcha-image,[data-captcha-background]').forEach(e=>{
     if(!visible||!shown(w,e)||e.tagName==='IMG')return;
     // A visible marked child was already scanned above. An unshown child
     // still leaves visible wrapper evidence that cannot be treated as absent.
     if(Array.from(e.querySelectorAll('img')).some(img=>shown(w,img)))return;
     const id=path+'|unsupported-image:'+key(e);
     add({id,kind:'image',sitekey:'',visible:true,receiver:'',receiverCount:0,
       callbacks:[],callbackHints:[],body:'',imageStatus:'image_unreadable',
       ownerWitness:nodeWitness(w,e),receiverWitness:'',invalid:false}, {});
   });
 };
 try{visit(window,document,'top',true)}catch(_){out.opaque.push('scan_error')}
 return {snapshot:out,handles,docs,wins};
})"""
DETECT_JS = "(()=>JSON.stringify((" + SCAN_JS + ")().snapshot))()"
INJECT_JS = r"""((approved,selected,answer)=>{
 let live;
 try{live=(""" + SCAN_JS + r""")()}catch(_){return JSON.stringify({__cs:1,reason:'binding_changed'})}
 const now=live.snapshot;
 // Only the image body bytes are replaced by their SHA-256 binding witness.
 // All URL/frame/widget/receiver/callback facts remain exactly compared.
 const view=s=>({...s,found:s.found.map(c=>{
   if(c.kind!=='image')return c;
   const {body,...other}=c;
   if(/^data:/i.test(other.src||''))other.src='data:image/sha256';
   return other;
 })});
 if(JSON.stringify(view(now))!==JSON.stringify(approved))
   return JSON.stringify({__cs:1,reason:'binding_changed'});
 const matching=now.found.filter(c=>c.id===selected);
 if(matching.length!==1||matching[0].receiverCount!==1||!matching[0].receiver||matching[0].invalid)
   return JSON.stringify({__cs:1,reason:'binding_changed'});
 const h=live.handles[selected],field=h&&h.receiver;
 if(!field||typeof answer!=='string'||!answer)
   return JSON.stringify({__cs:1,reason:'binding_changed'});
 let submitted=false,called=0,failed=false,networkObserved=false;
 const watch=()=>{submitted=true};
 const watched=[],fetchRestores=[];
 try{
   for(const d of live.docs){d.addEventListener('submit',watch,true);watched.push(d)}
   // Observe synchronous fetch on reachable windows during events/callback.
   // No claim is made about deferred requests, other network APIs or commits.
   for(const w of live.wins){
     if(typeof w.fetch!=='function')continue;
     const original=w.fetch;
     const wrapped=function(...args){networkObserved=true;return original.apply(this,args)};
     w.fetch=wrapped;
     if(w.fetch===wrapped)fetchRestores.push([w,original,wrapped]);
   }
 }catch(_){
   for(const d of watched){try{d.removeEventListener('submit',watch,true)}catch(e){}}
   for(const [w,original,wrapped] of fetchRestores){try{if(w.fetch===wrapped)w.fetch=original}catch(e){}}
   return JSON.stringify({__cs:1,reason:'possible_commit'});
 }
 try{
   field.value=answer;
   if(field.tagName==='TEXTAREA')field.innerHTML=answer;
   field.dispatchEvent(new Event('input',{bubbles:true}));
   field.dispatchEvent(new Event('change',{bubbles:true}));
   const refs=h.callbacks||[];
   if(submitted||networkObserved)return JSON.stringify({__cs:1,reason:'possible_commit',called});
   if(!field.isConnected||field.value!==answer)
     return JSON.stringify({__cs:1,reason:'response_unverified',called});
   // Input/change handlers may drift the binding; do not call back into it.
   let afterEvents;
   try{afterEvents=(""" + SCAN_JS + r""")()}
   catch(_){return JSON.stringify({__cs:1,reason:'possible_commit',called})}
   if(JSON.stringify(view(afterEvents.snapshot))!==JSON.stringify(approved))
     return JSON.stringify({__cs:1,reason:'possible_commit',called});
   const afterRefs=(afterEvents.handles[selected]||{}).callbacks||[];
   if(afterRefs.length!==refs.length||
       afterRefs.some((ref,i)=>ref.fn!==refs[i].fn||ref.label!==refs[i].label))
     return JSON.stringify({__cs:1,reason:'possible_commit',called});
   if(refs.length===1){
     try{
       const returned=refs[0].fn(answer);called=1;
       if(returned&&typeof returned.then==='function')failed=true;
     }catch(_){failed=true}
   }
   if(submitted||failed||networkObserved)return JSON.stringify({__cs:1,reason:'possible_commit',called});
   let after;
   try{after=(""" + SCAN_JS + r""")().snapshot}catch(_){return JSON.stringify({__cs:1,reason:'possible_commit',called})}
   if(JSON.stringify(view(after))!==JSON.stringify(approved))
     return JSON.stringify({__cs:1,reason:'possible_commit',called});
   if(!field.isConnected||!field.value||field.value!==answer)
     return JSON.stringify({__cs:1,reason:'response_unverified',called});
   if(refs.length!==1)
     return JSON.stringify({__cs:1,reason:refs.length===0?'callback_missing':'callback_ambiguous',called});
   if(called!==1)return JSON.stringify({__cs:1,reason:'callback_unconfirmed',called});
   return JSON.stringify({__cs:1,result:'injected',fields:1,callbacks:1,called:1});
 }catch(_){return JSON.stringify({__cs:1,reason:'possible_commit',called})}
 finally{
   for(const d of watched){try{d.removeEventListener('submit',watch,true)}catch(_){}}
   for(const [w,original,wrapped] of fetchRestores){
     try{if(w.fetch===wrapped)w.fetch=original}catch(_){}
   }
 }
})"""


class CaptchaError(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


# ---------------------------------------------------------------- local state

def local_dir() -> Path:
    env = os.environ.get("APPLIEDIN_LOCAL_DIR")
    if env:
        return Path(env)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from core.config import get_settings  # type: ignore

        return Path(get_settings().local_dir)
    except Exception:
        return Path(__file__).resolve().parents[1] / ".local"


def load_api_key(base: Path) -> str:
    key = os.environ.get("CAPSOLVER_API_KEY", "").strip()
    if key:
        return key
    path = base / "secrets.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    entry = data.get("capsolver")
    if isinstance(entry, dict):
        entry = entry.get("api_key")
    if isinstance(entry, str) and entry.strip():
        return entry.strip()
    raise CaptchaError(EXIT_ERROR, "key_missing")


def write_log(base: Path, record: dict) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    try:
        with (base / "captcha-log.jsonl").open("a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def short_url(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


# ---------------------------------------------------------------- guard

def guard(url: str, has_password: bool) -> None:
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if not host or parts.scheme not in ("http", "https"):
        raise CaptchaError(EXIT_REFUSED, f"not a web page: {url!r}")
    if any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS):
        raise CaptchaError(EXIT_REFUSED, f"blocked host {host}: LinkedIn CAPTCHA is never solved")
    if BLOCKED_URL.search(parts.path + "?" + parts.query):
        raise CaptchaError(EXIT_REFUSED, "login / sign-up / password page: human gate")
    if has_password:
        raise CaptchaError(EXIT_REFUSED, "page has a password field: human gate")


def pick_challenge(found: list[dict]) -> dict:
    """Select one visible logical widget, not one (kind, sitekey) pair."""
    visible = []
    for item in found:
        if not item.get("visible"):
            continue
        if item.get("kind") == "recaptcha" and item.get("size") == "invisible" and item.get("frame") != "bframe":
            continue
        visible.append(item)
    if not visible:
        raise CaptchaError(EXIT_NONE, "no_visible_challenge")
    if len(visible) != 1:
        raise CaptchaError(EXIT_UNSUPPORTED, "unsupported_widget")
    chosen = dict(visible[0])
    if chosen.get("kind") == "hcaptcha" or chosen.get("invalid"):
        raise CaptchaError(EXIT_UNSUPPORTED, "unsupported_widget")
    if chosen.get("kind") not in {"recaptcha", "turnstile", "image"}:
        raise CaptchaError(EXIT_UNSUPPORTED, "unsupported_widget")
    if chosen["kind"] == "image":
        if chosen.get("imageStatus") != "ok":
            raise CaptchaError(EXIT_UNSUPPORTED, "image_unreadable")
    elif not chosen.get("sitekey"):
        raise CaptchaError(EXIT_UNSUPPORTED, "unsupported_widget")
    if chosen.get("receiverCount") != 1 or not chosen.get("receiver"):
        raise CaptchaError(EXIT_UNSUPPORTED, "unsupported_widget")
    return chosen


def build_task(challenge: dict, url: str) -> dict:
    kind = challenge["kind"]
    if kind == "recaptcha":
        task = {
            "type": "ReCaptchaV2EnterpriseTaskProxyLess" if challenge.get("enterprise") else "ReCaptchaV2TaskProxyLess",
            "websiteURL": url,
            "websiteKey": challenge["sitekey"],
        }
        if challenge.get("size") == "invisible" or challenge.get("frame") == "bframe":
            task["isInvisible"] = True
        if challenge.get("enterprise"):
            if challenge.get("s"):
                task["enterprisePayload"] = {"s": challenge["s"]}
            if challenge.get("action"):
                task["pageAction"] = challenge["action"]
        return task
    if kind == "turnstile":
        task = {"type": "AntiTurnstileTaskProxyLess", "websiteURL": url, "websiteKey": challenge["sitekey"]}
        meta = {k: challenge[k] for k in ("action", "cdata") if challenge.get(k)}
        if meta:
            task["metadata"] = meta
        return task
    if kind == "image":
        import base64
        import hashlib
        body = challenge.get("body", "")
        try:
            data = base64.b64decode(body, validate=True)
        except (ValueError, base64.binascii.Error):
            raise CaptchaError(EXIT_UNSUPPORTED, "image_unreadable") from None
        if not 0 < len(data) <= 262144 or not (
            data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
        ):
            raise CaptchaError(EXIT_UNSUPPORTED, "image_unreadable")
        if hashlib.sha256(body.encode("ascii")).hexdigest() != challenge.get("bodyHash"):
            raise CaptchaError(EXIT_UNSUPPORTED, "image_unreadable")
        return {"type": "ImageToTextTask", "body": body}
    raise CaptchaError(EXIT_UNSUPPORTED, "unsupported_widget")


# ---------------------------------------------------------------- CapSolver

def _post(path: str, payload: dict, opener=urllib.request.urlopen) -> dict:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def solve(task: dict, key: str, *, opener=urllib.request.urlopen, sleep=time.sleep,
          clock=time.monotonic) -> tuple[str, str]:
    try:
        created = _post("/createTask", {"clientKey": key, "task": task}, opener)
        if created.get("errorId"):
            raise CaptchaError(EXIT_ERROR, "provider_error")
        task_id = created.get("taskId", "")
        if created.get("status") == "ready" and created.get("solution"):
            return task_id, _token(created["solution"], task["type"])
        if not task_id:
            raise CaptchaError(EXIT_ERROR, "provider_error")
        deadline = clock() + POLL_TIMEOUT
        while clock() < deadline:
            sleep(POLL_SECONDS)
            res = _post("/getTaskResult", {"clientKey": key, "taskId": task_id}, opener)
            if res.get("errorId"):
                raise CaptchaError(EXIT_ERROR, "provider_error")
            if res.get("status") == "ready":
                return task_id, _token(res.get("solution") or {}, task["type"])
        raise CaptchaError(EXIT_ERROR, "solver_timeout")
    except CaptchaError:
        raise
    except Exception:
        raise CaptchaError(EXIT_ERROR, "provider_error") from None


def _token(solution: dict, task_type: str = "") -> str:
    if not isinstance(solution, dict):
        raise CaptchaError(EXIT_ERROR, "solution_missing")
    token = (solution.get("text") if task_type == "ImageToTextTask" else
             solution.get("gRecaptchaResponse") or solution.get("token"))
    if not isinstance(token, str) or not token:
        raise CaptchaError(EXIT_ERROR, "solution_missing")
    return token


# ---------------------------------------------------------------- bsk

def _find(obj, key):
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], str):
            return obj[key]
        for v in obj.values():
            hit = _find(v, key)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find(v, key)
            if hit:
                return hit
    return None


def _marker_payload(obj):
    """Find the JSON string our scripts return (it carries "__cs":1)."""
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("{") and '"__cs"' in s:
            try:
                return json.loads(s)
            except ValueError:
                return None
        return None
    if isinstance(obj, dict):
        if obj.get("__cs") == 1:
            return obj
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            hit = _marker_payload(v)
            if hit is not None:
                return hit
    return None


def _bsk(args: list[str], runner=subprocess.run) -> dict:
    proc = runner(["bsk", *args, "--json"], capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        raise CaptchaError(EXIT_ERROR, "browser_error")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise CaptchaError(EXIT_ERROR, "browser_error") from None


def evaluate(session: str, tab: str | None, expression: str, runner=subprocess.run) -> dict:
    sync_args = ["cooperate", "sync", "--session", session]
    if tab:
        sync_args += ["--tab-id", tab]
    sync = _bsk(sync_args, runner)
    context = _find(sync, "context_id")
    observation = sync.get("observation_id") if isinstance(sync, dict) else None
    observation = observation or _find(sync, "observation_id")
    if not context:
        raise CaptchaError(EXIT_ERROR, "browser_error")
    args = ["evaluate", "--session", session, "--context", context]
    if observation:
        args += ["--observation", observation]
    if tab:
        args += ["--tab-id", tab]
    args.append(expression)
    result = _marker_payload(_bsk(args, runner))
    if result is None:
        raise CaptchaError(EXIT_ERROR, "browser_error")
    return result


# ---------------------------------------------------------------- main

def run(session: str, tab: str | None, dry_run: bool, base: Path) -> tuple[int, dict]:
    log: dict = {"session": session}
    try:
        page = evaluate(session, tab, DETECT_JS)
        url = page.get("url", "")
        frames = page.get("frames")
        if not isinstance(frames, list) or not frames or frames[0].get("url") != url:
            raise CaptchaError(EXIT_UNSUPPORTED, "unsupported_widget")
        for frame in frames:
            try:
                guard(frame.get("url", ""), bool(frame.get("password")))
            except CaptchaError:
                raise CaptchaError(EXIT_REFUSED, "protected_page") from None
        if page.get("opaque"):
            raise CaptchaError(EXIT_UNSUPPORTED, "unsupported_widget")
        challenge = pick_challenge(page.get("found") or [])
        task = build_task(challenge, url)
        log.update(kind=challenge["kind"], task_type=task["type"])
        if dry_run:
            log["result"] = "dry_run"
            return EXIT_OK, log
        key = load_api_key(base)
        started = time.monotonic()
        task_id, token = solve(task, key)
        log["solve_seconds"] = round(time.monotonic() - started, 1)
        # Retain the isolated image bytes only for the paid task. The browser
        # scanner and Python verify the same SHA-256, then compare the compact
        # approved snapshot inside the mutating evaluation.
        approved = json.loads(json.dumps(page))
        for item in approved.get("found", []):
            if item.get("kind") == "image":
                item.pop("body", None)
                if str(item.get("src", "")).lower().startswith("data:"):
                    item["src"] = "data:image/sha256"
        expression = (INJECT_JS + "(" + json.dumps(approved, ensure_ascii=True) +
                      "," + json.dumps(challenge["id"]) + "," +
                      json.dumps(token, ensure_ascii=True) + ")")
        try:
            injected = evaluate(session, tab, expression)
        except Exception:
            # The evaluation may have already called a committing callback.
            raise CaptchaError(EXIT_UNSUPPORTED, "possible_commit") from None
        if not isinstance(injected, dict):
            raise CaptchaError(EXIT_UNSUPPORTED, "possible_commit")
        reason = injected.get("reason")
        if reason is not None:
            allowed = {"binding_changed", "callback_missing", "callback_ambiguous",
                       "callback_unconfirmed", "response_unverified", "possible_commit"}
            safe = reason if isinstance(reason, str) and reason in allowed else "possible_commit"
            raise CaptchaError(EXIT_UNSUPPORTED, safe)
        if (injected.get("result") != "injected" or injected.get("fields") != 1 or
                injected.get("callbacks") != 1 or injected.get("called") != 1):
            raise CaptchaError(EXIT_UNSUPPORTED, "callback_unconfirmed")
        log.update(fields=1, callbacks=1, called=1, result="injected")
        return EXIT_OK, log
    except CaptchaError as exc:
        log.update(result="refused" if exc.code == EXIT_REFUSED else "failed", reason=exc.reason)
        return exc.code, log
    except Exception:
        log.update(result="failed", reason="browser_error")
        return EXIT_ERROR, log


def save_api_key(base: Path, key: str) -> Path:
    """Store the key in <local_dir>/secrets.json, keeping other entries, mode 600."""
    key = key.strip()
    if not key:
        raise CaptchaError(EXIT_ERROR, "empty key")
    path = base / "secrets.json"
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError
    except FileNotFoundError:
        data = {}
    except ValueError:
        raise CaptchaError(EXIT_ERROR, "key_file_invalid") from None
    data["capsolver"] = {"api_key": key}
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def balance(key: str, opener=urllib.request.urlopen) -> float:
    res = _post("/getBalance", {"clientKey": key}, opener)
    if res.get("errorId"):
        raise CaptchaError(EXIT_ERROR, "provider_error")
    return float(res.get("balance", 0))


def set_key_interactive(base: Path) -> int:
    import getpass

    try:
        key = getpass.getpass("CapSolver API key (input hidden): ")
        path = save_api_key(base, key)
        print(f"saved to {path} (mode 600)")
        print(f"key works, balance: {balance(load_api_key(base)):.2f} USD")
        return EXIT_OK
    except CaptchaError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return exc.code
    except Exception as exc:  # network error on the balance check
        print("key saved, balance check failed: provider_error", file=sys.stderr)
        return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set-key", action="store_true", help="prompt for the CapSolver key (hidden) and save it")
    ap.add_argument("--balance", action="store_true", help="check the stored key and print the balance")
    ap.add_argument("--session", help="your own bsk session id")
    ap.add_argument("--tab-id", help="tab id (default: the session's active tab)")
    ap.add_argument("--dry-run", action="store_true", help="detect and check only, no CapSolver call")
    ns = ap.parse_args(argv)
    base = local_dir()
    if ns.set_key:
        return set_key_interactive(base)
    if ns.balance:
        try:
            print(f"balance: {balance(load_api_key(base)):.2f} USD")
            return EXIT_OK
        except CaptchaError as exc:
            print(f"error: {exc.reason}", file=sys.stderr)
            return exc.code
    if not ns.session:
        ap.error("--session is required")
    code, log = run(ns.session, ns.tab_id, ns.dry_run, base)
    write_log(base, {**log, "exit": code})
    print(json.dumps({"exit": code, **{k: v for k, v in log.items() if k != "session"}}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
