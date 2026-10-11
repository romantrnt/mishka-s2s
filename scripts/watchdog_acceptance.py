import asyncio,json,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
from app.foundation.application import ScenarioApplication
from app.foundation.observability import Metrics

class L:
 def __init__(s): s.state="READY";s.generation=1;s.degraded=[]
 def snapshot(s): return {"state":s.state,"generation":s.generation}
 def degrade(s,r,d=None): s.generation+=1;s.state="DEGRADED";s.degraded.append({"reason":r,"details":d or {}})

class E:
 def __init__(s):
  s.task_started_at={};s.active_tasks={};s.active_queues={};s.cancel=SimpleNamespace(tokens={});s.metrics=SimpleNamespace(active_playback={})
 def resource_snapshot(s): return {"active_tasks":len(s.active_tasks),"active_sessions":len(s.active_queues),"registry_size":len(s.active_tasks)+len(s.active_queues),"queued_chunks":0,"pcm_bytes":0}
 async def cancel_session(s,k):
  s.task_started_at.pop(k,None);s.active_tasks.pop(k,None);s.active_queues.pop(k,None);s.cancel.tokens.pop(k,None);s.metrics.active_playback.pop(k,None)

class P:
 def __init__(s,e): s.e=e;s.pending_started_at={};s.pending_tasks={};s.generations={};s.ingress={}
 async def cancel_session(s,k):
  s.pending_started_at.pop(k,None);s.pending_tasks.pop(k,None);s.generations.pop(k,None);s.ingress.pop(k,None);await s.e.cancel_session(k)

class Bad(Metrics):
 def watchdog_check(s,*a,**k): raise RuntimeError("synthetic_check_failure")

def make():
 e=E();p=P(e);m=SimpleNamespace(engine=e,audio_pipeline=p);a=object.__new__(ScenarioApplication)
 a.sessions=SimpleNamespace(iter_sessions=lambda:());a.bounded_s2s=e;a.s2s_audio=p;a.runtime_manager=m;a.runtime_status={};a.metrics=Metrics()
 a._audio_completions={};a._audio_completion_started_at={};a.conversation_contexts={};a._session_epochs={};a._last_audio_ack_at={};a._idle_expiration_count=0;a._last_idle_cleanup=None
 a._watchdog_task=None;a._watchdog_lifecycle=None;a._watchdog_runtime_manager=None;a._watchdog_cooldown_until={}
 a.watchdog_interval_seconds=.01;a.watchdog_stale_after_seconds=1.;a.watchdog_cooldown_seconds=10.
 return a,m,e

async def main():
 a,m,e=make();l=L();trace=[]
 x=await a.start_watchdog(l,m);y=await a.start_watchdog(l,m);trace.append({"start":x,"repeat":y})
 await asyncio.sleep(.03);assert a._watchdog_task and not a._watchdog_task.done()
 for kind in ("task","playback","generation"):
  k="stale_"+kind
  if kind=="task": e.task_started_at[k]=time.monotonic()-10;e.active_tasks[k]=object();e.cancel.tokens[k]=1
  elif kind=="playback": e.metrics.active_playback[k]=time.monotonic()-10;e.active_queues[k]=SimpleNamespace(token=1);e.cancel.tokens[k]=1
  else: e.active_queues[k]=SimpleNamespace(token=1);e.cancel.tokens[k]=2
  await a.watchdog_check_once();trace.append({"case":kind,"snapshot":e.resource_snapshot(),"event":a.metrics.watchdog_events[-1]});assert k not in e.active_queues and k not in e.active_tasks
 n=len(a.metrics.watchdog_events);await a.watchdog_check_once();assert len(a.metrics.watchdog_events)==n+1 and a.metrics.watchdog_events[-1]["condition"]=="clean"
 e.task_started_at["debounced"]=time.monotonic()-10;e.active_tasks["debounced"]=object();e.cancel.tokens["debounced"]=1;a._watchdog_cooldown_until[("stale_task","debounced")]=time.monotonic()+10
 n=len(a.metrics.watchdog_events);await a.watchdog_check_once();assert len(a.metrics.watchdog_events)==n
 await a._cleanup_session_resources("debounced")
 a.metrics=Bad();g=l.generation;await a.watchdog_check_once();trace.append({"case":"exception","lifecycle":l.snapshot(),"event":a.metrics.watchdog_events[-1]});assert l.state=="DEGRADED" and l.generation>g
 a.metrics=Metrics();await a.stop_watchdog();trace.append({"shutdown_task":a._watchdog_task,"live":[t.get_name() for t in asyncio.all_tasks() if t.get_name()=="mishka-stale-watchdog"]});assert a._watchdog_task is None and not any(t.get_name()=="mishka-stale-watchdog" for t in asyncio.all_tasks())
 print(json.dumps({"ok":True,"trace":trace},ensure_ascii=False,indent=2))
asyncio.run(main())
