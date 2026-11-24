"""App-owned waiting inactivity, distinct from identity leases and clinical RAM.

The 120-second engineering idle window starts only after real engagement.
Provider/playback occupancy suspends waiting; completion is not user activity.
"""
import asyncio
import uuid
from .domain import Event

class EngagementIdleMixin:
    def _init_engagement_idle(self):
        self._engagement_owners = {}
        self._engagement_pressed = {}
        self._engagement_task = None
        self._engagement_ingress = {}
        self.engagement_wait_seconds = 120.0

    def _engagement_work_busy(self, sid):
        state = self.canonical_runtime.sessions.get(sid)
        engine = getattr(state, 'game_engine_state', None)
        story=self._personal_stories.get(sid)
        story_busy=bool(story and self._personal_story_scope(sid,story) and story.task is not None and not story.task.done())
        bounded=self.bounded_s2s
        active=getattr(bounded,'is_active',None)
        response_busy=bool(callable(active) and active(sid))
        for registry in ('pending_tasks','active_tasks'):
            task=getattr(bounded,registry,{}).get(sid)
            response_busy=response_busy or bool(task is not None and not task.done())
        transfer = self._public_story_transfer(sid)
        return bool(self._active_audio.get(sid) or response_busy
            or (transfer is not None and transfer['resume'])
            or (engine is not None and engine.phase in {'instruction','demonstrate'})
            or any(not task.done() and getattr(task,'_mishka_session_id',None)==sid
                   and callable(getattr(task,'_mishka_owner_current',None))
                   and task._mishka_owner_current() for task in self._owned_text_jobs)
            or story_busy)

    def _engagement_busy(self, sid):
        # VAD/STT occupies ingress but does not yet prove user activity.
        return bool(self._engagement_work_busy(sid) or self._session_has_active_work(sid)
            or getattr(getattr(self.s2s_audio,'ingress',{}).get(sid),'active',False))

    def _engagement_scope(self, sid):
        state = self.canonical_runtime.sessions.get(sid)
        if state is None: return None
        return (self._session_epochs.get(sid,0), state.identity_request_id)

    def _drop_engagement(self, sid, *, keys=False):
        self._engagement_owners.pop(sid,None)
        if keys: self._engagement_pressed.pop(sid,None)

    def _engagement_event(self, event, decision, *, accepted, voice_activity=False):
        sid=event.session_id
        state=self.canonical_runtime.sessions.get(sid)
        reason=decision.get('reason','')
        # Releases remain meaningful while parked: they re-arm a fresh press,
        # but never create an owner or extend waiting. Admission rejects stale edges.
        if event.event_type=='ButtonReleased' and not any(word in reason for word in ('stale','duplicate','untrusted','invalid','rejected')):
            self._engagement_pressed.get(sid,set()).discard(event.payload.get('button_id'))
        if (state is None or state.interaction_idle or state.safety_severity=='urgent'
                or state.mode in {'idle','soft_end','session_closed','safety_stop'}):
            self._drop_engagement(sid)
            return
        if accepted and event.event_type in {'ClientProfileReady','ClientProfileCleared','IdentityReadyExpired','IdentityAuthorizationExpired','SessionReset','SessionCancel','HardwareDisconnected'}:
            self._drop_engagement(sid,keys=True)
            return
        if accepted and event.event_type=='HandsetPlaced' and not self.canonical_runtime.exhibition_mode and state.mode!='game_active':
            self._drop_engagement(sid)
            return  # suspended clinical RAM keeps its independent 300-second clock
        rejected = (not accepted or any(word in reason for word in ('stale','duplicate','untrusted','ignored','held','invalid','rejected','already')))
        fresh=False
        keys=self._engagement_pressed.setdefault(sid,set())
        if not rejected and event.event_type=='ButtonPressed':
            button=event.payload.get('button_id')
            fresh=button not in keys
            keys.add(button)
        elif event.event_type=='ButtonReleased' and 'stale' not in reason and 'duplicate' not in reason:
            keys.discard(event.payload.get('button_id'))
        elif not rejected and ((event.event_type in {'HandsetLifted','SessionStarted','SessionAudioEnabled','InterviewSkipped'} or voice_activity)
                or (event.event_type=='HandsetPlaced' and state.mode=='game_active')):
            fresh=True
        now=self.monotonic_now()
        owner=self._engagement_owners.get(sid)
        scope=self._engagement_scope(sid)
        if owner is not None and owner['scope']!=scope:
            self._drop_engagement(sid);owner=None
        if fresh:
            owner={'scope':scope,'nonce':uuid.uuid4().hex,'deadline':now+self.engagement_wait_seconds,'busy':False,'busy_resets_wait':True,'completed_work':False}
            self._engagement_owners[sid]=owner
        if owner is not None:
            if (event.event_type=='GameTimer' or (event.event_type in {'SpeechReceived','VoiceIntentReady'} and not voice_activity)) and not owner['busy']:
                owner['busy_resets_wait']=False
            self._observe_engagement_busy(sid,owner,now)
            self._start_engagement_monitor()

    def _observe_engagement_busy(self,sid,owner,now):
        busy=self._engagement_busy(sid)
        work_busy=self._engagement_work_busy(sid)
        if work_busy and owner.get('busy_resets_wait',True):
            owner['completed_work']=True
        # End the meaningful-work clock at its own completion, even while
        # unrelated input occupancy is still draining.
        if not work_busy and owner.get('completed_work',False) and not owner.get('preserve_deadline',False):
            owner['deadline']=now+self.engagement_wait_seconds
            owner['nonce']=uuid.uuid4().hex
        if not work_busy:
            owner['completed_work']=False
        owner['busy']=busy

    def _engagement_due(self,sid,nonce):
        owner=self._engagement_owners.get(sid)
        state=self.canonical_runtime.sessions.get(sid)
        return bool(owner and owner['nonce']==nonce and owner['scope']==self._engagement_scope(sid)
            and state is not None and not state.interaction_idle and state.safety_severity!='urgent'
            and state.mode!='safety_stop' and not self._engagement_ingress.get((sid,self._engagement_scope(sid)),0) and not owner['busy'] and not self._engagement_busy(sid)
            and self.monotonic_now()>=owner['deadline'])

    async def expire_engagement_sessions(self):
        # Authorization expiry always wins, including during busy playback.
        await self.expire_identity_leases()
        self.expire_dialogue_memory()
        results=[]
        for sid,owner in tuple(self._engagement_owners.items()):
            if owner['scope']!=self._engagement_scope(sid):
                self._drop_engagement(sid);continue
            self._observe_engagement_busy(sid,owner,self.monotonic_now())
            nonce=owner['nonce']
            if not self._engagement_due(sid,nonce): continue
            state=self.canonical_runtime.sessions[sid]
            sequence=state.last_sequence_by_source.get('internal_interaction_idle',0)+1
            results.append(await self._receive_canonical_event(Event('InteractionIdleExpired',sid,sequence,
                self.now(),'internal_interaction_idle',{},'interaction-idle:'+nonce,self.monotonic_now()),
                interaction_idle_authorized=True,_interaction_nonce=nonce))
        return results

    def _start_engagement_monitor(self):
        if not self.background_scheduler or self._engagement_task is not None: return
        async def monitor():
            try:
                while self._engagement_owners:
                    await asyncio.sleep(.25)
                    await self.expire_engagement_sessions()
            except asyncio.CancelledError: pass
            finally:
                if self._engagement_task is asyncio.current_task(): self._engagement_task=None
        self._engagement_task=asyncio.create_task(monitor(),name='mishka-engagement-idle')

    def _refresh_engagement_busy(self,sid):
        owner=self._engagement_owners.get(sid)
        if owner is not None and owner['scope']==self._engagement_scope(sid):
            self._observe_engagement_busy(sid,owner,self.monotonic_now())
