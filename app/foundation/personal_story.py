"""Whole-story generation and session-private PCM. No text/PCM in effects."""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field, replace
import json
import re
import uuid
from .age_policy import POLICIES
from .private_welcome import valid_pcm
from .domain import Event, PlaybackHandle

STORY_ID = 'PERSONAL_STORY'
STORY_MODES = {'personal_story_generating', 'personal_story_playing', 'story_pause_announcement', 'story_paused', 'story_resume_announcement'}
DURATION_BOUNDS = {'1-3': (45,70), '3-5': (60,100), '4-5': (60,100), '5-7': (90,150), '6-7': (90,150)}
THEMES = {'dinosaurs':'динозавры', 'космос':'космос', 'лес':'лес', 'дружелюбные машины':'дружелюбные машины', 'море':'море'}

def story_theme(value):
    value = str(value or '').strip().casefold()
    aliases = {'динозавры':'dinosaurs', 'динозавров':'dinosaurs', 'динозаврах':'dinosaurs', 'космосе':'космос', 'лесе':'лес', 'машины':'дружелюбные машины', 'машинах':'дружелюбные машины'}
    value = re.sub(r'^(?:расскажи\s+(?:сказку\s+)?(?:про|о)|про|о)\s+', '', value)
    value = aliases.get(value, value)
    return value if value in THEMES else None

def story_messages(theme,band,constraints):
    lower,upper=DURATION_BOUNDS[band]
    # Only enums/numeric policy reach generation. Never name/profile/STT/history.
    brief={'theme':theme,'age_band':band,'forbidden_topics':['diagnosis','treatment','real_schedule','secrets'],
           'content_constraints':[v for v in constraints if v in {'avoid_parent_assumptions','avoid_body_assumptions'}]}
    prompt = ('Create a complete original Russian children story with setting, a gentle problem, an attempt and a safe resolved ending. '
              'No medical advice/promises, real schedules, secrets from adults, identifying details, instructions or quoted copyrighted stories. '
              'Return ONLY JSON with exact keys title,plot,age_band,estimated_duration_sec,safety_tags,ending_state. Plot is an object with exactly setting,problem,attempt,resolution, each a distinct nonempty paragraph of complete sentences. Do not repeat the story in a separate text field; the application joins the four paragraphs. '
              f'estimated_duration_sec must be a JSON integer from {lower} to {upper}, never float, string, boolean or null. '
              'ending_state must be safe_resolved. Full text at most 6000 characters; no markup. '
              f'Each spoken sentence at most {POLICIES[band].max_words} words (engineering age adaptation, not clinical assurance). '
              f'Natural spoken duration {lower} to {upper} seconds, never silence/padding. Brief: {json.dumps(brief,ensure_ascii=False)}')
    return [{'role':'system','content':prompt}]

class PersonalStoryService:
    def __init__(self, engine):
        self.engine = engine

    def provider(self):
        from .s2s import RetryingLLMProvider
        provider=getattr(self.engine, "llm", None)
        while isinstance(provider,RetryingLLMProvider): provider=provider.delegate
        return provider

    def capable(self):
        from .s2s import CachedTTSProvider, ResilientVoiceProvider
        e = self.engine
        if e is None: return False
        tts, voice = getattr(e, "tts", None), getattr(e, "voice", None)
        while isinstance(tts, CachedTTSProvider): tts=tts.delegate
        if isinstance(voice, ResilientVoiceProvider): voice=voice.primary
        preflight=getattr(self.provider(),'supports_story_request',None)
        if callable(preflight) and not all(preflight(story_messages(theme,band,('avoid_parent_assumptions','avoid_body_assumptions')))
                                          for theme in THEMES for band in POLICIES):return False
        return (callable(getattr(self.provider(),'stream_story',None))
                and getattr(self.provider(),'supports_full_story',False) is True and callable(getattr(tts,'synthesize',None))
                and callable(getattr(voice,'convert_private',None)) and callable(getattr(getattr(e,'response_validator',None),'validate',None))
                and getattr(getattr(e,'response_validator',None),'supports_full_story',False) is True)

    async def render(self, theme, band, constraints, token, current):
        from .canonical_contract import validate_generated_story
        from .s2s import CachedTTSProvider, ResilientVoiceProvider
        if not self.capable() or theme not in THEMES or band not in POLICIES: return None
        e=self.engine; lower,upper=DURATION_BOUNDS[band]
        brief={'theme':theme,'age_band':band,'forbidden_topics':['diagnosis','treatment','real_schedule','secrets'],
               'content_constraints':[v for v in constraints if v in {'avoid_parent_assumptions','avoid_body_assumptions'}]}
        raw=''
        async for part in self.provider().stream_story(story_messages(theme,band,constraints),str(uuid.uuid4()),lambda:not current()):
            if not current(): return None
            if not isinstance(part,str): return None
            raw+=part
            if len(raw)>8000: return None
        if not current(): return None
        try:
            def unique(pairs):
                result={}
                for key,value in pairs:
                    if key in result: raise ValueError('duplicate')
                    result[key]=value
                return result
            candidate=json.loads(raw,object_pairs_hook=unique)
        except (ValueError,TypeError): return None
        required={'title','plot','age_band','estimated_duration_sec','safety_tags','ending_state'}
        if not isinstance(candidate,dict) or set(candidate)!=required: return None
        plot=candidate.get('plot')
        keys=('setting','problem','attempt','resolution')
        if not isinstance(plot,dict) or set(plot)!=set(keys):return None
        parts=[plot[k] for k in keys]
        if any(not isinstance(v,str) or not v.strip() or v!=v.strip()
               or v[-1] not in '.!?' for v in parts):return None
        if len(set(parts))!=4:return None
        candidate['text']=' '.join(parts)
        # One derived body, not duplicate model-produced copies. The wire JSON
        # stays bounded at8000; the spoken body separately stays at6000.
        if len(candidate['text'])>6000:return None
        if not validate_generated_story(candidate,brief)[0]: return None
        text=candidate['text']; sentences=[v for v in re.split(r'[.!?]+',text) if v.strip()]
        if not sentences or any(len(v.split())>POLICIES[band].max_words for v in sentences): return None
        if re.search(r'[<>]|https?://',text): return None
        # Whole-text safety before any synthesis. Model tags are not authority.
        validate=getattr(e.response_validator,'validate_story',e.response_validator.validate)
        decision=await validate(candidate['title']+'. '+text)
        if not current() or not decision.allowed: return None
        tts,voice=e.tts,e.voice
        while isinstance(tts,CachedTTSProvider): tts=tts.delegate
        if isinstance(voice,ResilientVoiceProvider): voice=voice.primary
        chunks=[];sample_rate=None;sample_count=0
        # Validate the entire plot first, then synthesize bounded sentences.
        # Never request a full 6000-char native tuple or concatenate beyond
        # the age duration ceiling. A provider's own native allocations remain
        # its responsibility; this bounds accepted aggregate PCM in this owner.
        spoken=re.findall(r'[^.!?]+[.!?]+|[^.!?]+$',text)
        for sentence in spoken:
            if not current():return None
            audio=await tts.synthesize(sentence.strip(),token,lambda:not current())
            if (not current() or not valid_pcm(audio) or audio.token!=token
                    or audio.sample_rate>192000 or len(audio.samples)>upper*audio.sample_rate):return None
            audio=await voice.convert_private(audio,lambda:not current())
            if (not current() or not valid_pcm(audio) or audio.token!=token
                    or audio.sample_rate>192000):return None
            if sample_rate is None:sample_rate=audio.sample_rate
            if audio.sample_rate!=sample_rate:return None
            sample_count+=len(audio.samples)
            if sample_count>upper*sample_rate:return None
            chunks.append(audio.samples)
        if not chunks or not lower<=sample_count/sample_rate<=upper:return None
        from itertools import chain
        return replace(audio,samples=tuple(chain.from_iterable(chunks)))

@dataclass(repr=False)
class StoryOwner:
    epoch: int
    request_id: str
    identity: tuple
    band: str
    deadline: float
    audio: object = field(default=None,repr=False)
    task: object = field(default=None,repr=False)
    offset_ms: int = 0
    checkpoint_ms: int | None = None
    closed: bool = False
    def close(self):
        self.closed=True;self.audio=None
        self.offset_ms=0;self.checkpoint_ms=None
        task,self.task=self.task,None
        if task is not None and not task.done(): task.cancel()

class PersonalStoryMixin:
    def _discard_personal_story(self,sid):
        owner=self._personal_stories.pop(sid,None)
        if owner: owner.close()

    def _personal_story_scope(self,sid,owner,*,playback=False,checkpoint=False):
        state=self.canonical_runtime.sessions.get(sid)
        if not (owner and not owner.closed and self._personal_stories.get(sid) is owner
                and state and state.handset=='up' and state.mode in STORY_MODES
                and self._session_epochs.get(sid,0)==owner.epoch
                and (state.profile_ref,state.identity_request_id)==owner.identity):return False
        if owner.identity[0] and not self._identity_scope_live(sid,'personalize'):return False
        if checkpoint:return (state.mode in {'story_pause_announcement','story_paused'}
                              and state.active_story_id==STORY_ID and owner.audio is not None)
        if playback:return (state.mode=='personal_story_playing' and state.active_story_id==STORY_ID
                            and owner.audio is not None)
        return state.mode=='personal_story_generating' and self.monotonic_now()<owner.deadline

    def personal_story_available(self):
        return bool(getattr(self.audio,'private_pcm_supported',False)
            and callable(getattr(self.audio,'play_pcm_owned',None))
            and PersonalStoryService(self.bounded_s2s).capable())

    async def _prepare_personal_story(self,sid,brief,token):
        self._discard_personal_story(sid)
        state=self.canonical_runtime.sessions[sid]
        theme=story_theme(brief.get('theme')); band=state.age_band
        if not self.personal_story_available() or theme is None or band not in POLICIES:return None
        if len(self._personal_story_jobs)>=1:return None
        e=self.bounded_s2s
        budget=min(e.config.max_turn_seconds,e.config.llm_timeout_s+e.config.tts_timeout_s+e.config.rvc_timeout_s)
        owner=StoryOwner(self._session_epochs.get(sid,0),uuid.uuid4().hex,
                         (state.profile_ref,state.identity_request_id),band,self.monotonic_now()+budget)
        self._personal_stories[sid]=owner
        current=lambda:self._personal_story_scope(sid,owner)
        task=asyncio.create_task(PersonalStoryService(e).render(theme,band,brief.get('content_constraints',()),token,current),name='mishka-personal-story')
        owner.task=task;self._personal_story_jobs.add(task)
        def observed(done):
            self._personal_story_jobs.discard(done)
            if not done.cancelled():done.exception()
        task.add_done_callback(observed)
        try:
            done,_=await asyncio.wait({task},timeout=budget)
            if not done or task.cancelled() or not current():owner.close();return None
            owner.audio=task.result()
            owner.task=None
            if owner.audio is None:owner.close();return None
            return owner
        except asyncio.CancelledError:
            owner.close();raise
        except Exception:
            owner.close();return None

    def _play_personal_story(self,sid,effect,token):
        owner=self._personal_stories.get(sid)
        if not self._personal_story_scope(sid,owner,playback=True):return False
        audio=owner.audio
        checkpoint=effect.get('checkpoint') or {}
        offset_ms=checkpoint.get('offset_ms',0)
        duration_ms=int(1000*len(audio.samples)/audio.sample_rate)
        if type(offset_ms) is not int or not 0<=offset_ms<=duration_ms:return False
        if checkpoint and (checkpoint.get('story_id')!=STORY_ID or owner.checkpoint_ms!=offset_ms):return False
        owner.offset_ms=offset_ms
        offset=int(owner.offset_ms*audio.sample_rate/1000)
        handle=PlaybackHandle(sid,owner.epoch,f'{sid}:{uuid.uuid4().hex}',effect['playback_lease'],STORY_ID,token)
        self._register_audio_lease(sid,STORY_ID,token,handle.playback_id,handle.playback_lease,duration_ms=max(1,duration_ms-offset_ms))
        effect.update(playback_id=handle.playback_id,generation=handle.generation,session_id=sid)
        owner.checkpoint_ms=None
        if checkpoint and offset_ms==duration_ms:
            return 'completed'
        self.audio.play_pcm_owned(handle,replace(audio,samples=audio.samples[offset:],token=token,session_id=sid,generation_id=owner.epoch))
        return True
