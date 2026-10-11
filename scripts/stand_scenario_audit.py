#!/usr/bin/env python3
import json, urllib.error, urllib.request
import os
from collections import Counter
BASE='http://127.0.0.1:8000'
HEADERS={"Content-Type":"application/json","X-Stand-Token":os.environ["STAND_ACCESS_TOKEN"]}

def call(path, **fields):
    req=urllib.request.Request(BASE+path, data=json.dumps({'session_id':'audit-scenario', **fields}).encode(), headers=HEADERS, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or '{}')

def run(name, fn):
    try:
        result=fn()
        print(json.dumps({'case':name,'ok':True,'result':result},ensure_ascii=False))
        return True
    except Exception as exc:
        print(json.dumps({'case':name,'ok':False,'error':repr(exc)},ensure_ascii=False))
        return False

def reset(): call('/stand/reset')
def up(): call('/stand/handset', state='up')
def down(): call('/stand/handset', state='down')
def tag(v): return call('/stand/rfid', tag_id=v)
def button(v): return call('/stand/button', button_id=v)
def release(v): return call('/stand/button-released', button_id=v)
def hold(v,ms,kind='button-hold'): return call('/stand/'+kind, button_id=v, duration_ms=ms)
def state():
    req=urllib.request.Request(BASE+'/stand/state',headers=HEADERS)
    with urllib.request.urlopen(req,timeout=10) as r: return json.load(r)

def one_button(b):
    reset(); tag('test-child-001'); up(); status, body=button(b); release(b)
    return {'status':status,'node':body.get('node'),'reason':body.get('reason'),'outputs':[x.get('action') for x in body.get('outputs',[])], 'state':state().get('node')}

passed=0
cases=[]
cases += [('health', lambda: (urllib.request.urlopen(BASE+'/health').status, json.load(urllib.request.urlopen(BASE+'/health')).get('status')))]
cases += [('ready', lambda: (urllib.request.urlopen(BASE+'/ready').status, json.load(urllib.request.urlopen(BASE+'/ready')).get('ready')))]
cases += [('unknown_rfid', lambda: (reset(), tag('audit-unknown')[0]))]
cases += [('pre_rfid_button', lambda: (reset(), button(1)[1].get('reason')))]
cases += [('guest_button6', lambda: (reset(), up(), button(6)[1].get('node')))]
cases += [('guest_button7', lambda: (reset(), up(), button(7)[1].get('node')))]
for b in range(1,8): cases.append(('child_button_'+str(b), lambda b=b: one_button(b)))
def rapid():
    reset(); tag('test-child-001'); up(); out=[]
    for b in [1,2,3,4,5,6,7,1,7,6,2,5]:
        s,r=button(b); out.append((b,s,r.get('node'),r.get('reason')))
    return out
cases.append(('rapid_conflict_sequence', rapid))
cases.append(('hold_6', lambda: (reset(),tag('test-child-001'),up(),hold(6,5000)[1].get('node'))))
cases.append(('long_hold_6', lambda: (reset(),tag('test-child-001'),up(),hold(6,10000,'button-long-hold')[1].get('node'))))
cases.append(('reset_after_end', lambda: (reset(),tag('test-child-001'),up(),button(7)[1].get('node'),reset(),state().get('state'))))
for name,fn in cases:
    if run(name,fn): passed += 1
print(json.dumps({'summary':{'passed':passed,'total':len(cases)},'note':'physical UART/mic and external production DB remain outside this HTTP audit'},ensure_ascii=False))
raise SystemExit(0 if passed==len(cases) else 1)
