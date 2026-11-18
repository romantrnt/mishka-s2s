"""Opt-in, instance-local cache of validated public menu TTS + RVC audio."""
from __future__ import annotations

from array import array
from collections import OrderedDict
import asyncio
import hashlib
import inspect
import json
from pathlib import Path

from .menu_content import MenuPage, build_menu_page
from .private_welcome import valid_pcm
from .scene_menus import MENU_PCM_MODES


def providers(application):
    from .s2s import CachedTTSProvider, ResilientVoiceProvider
    engine = application.bounded_s2s
    tts, voice = engine.tts, engine.voice
    while isinstance(tts, CachedTTSProvider):
        tts = tts.delegate
    if isinstance(voice, ResilientVoiceProvider):
        voice = voice.primary
    return tts, voice


async def menu_validation_text(application, page, text):
    """Retain the exact existing verified Telefon catalogue exception."""
    engine = application.bounded_s2s
    if not isinstance(page, MenuPage) or text != engine.normalizer.normalize(page.spoken_text):
        return None
    validation_text = page.spoken_text
    for item in page.items:
        decision = await engine.response_validator.validate_trusted(item.title)
        if decision.allowed:
            continue
        verified = (application.canonical_runtime.stories.get_verified(item.id)
                    if page.scene == 'fixed_story_menu' else None)
        if not (decision.reason == 'personal_data' and item.id == 'fixed_neznayka_v1'
                and item.title == 'Телефон' and verified
                and verified.get('display_title') == 'Телефон'
                and verified.get('audio_sha256') in {
                    '239c1c3d0308d2a20693c8818dfd5c77a04a1d030f0e69554b2c3cd4971637db',
                    # Operator-approved original: assets/stories/original/Сказка Телефон.wav.
                    '21a29500591e0e085b4d78a6e715167e92e7e3fa87594cee617d39793e9574ec',
                }):
            return None
        words = {1:'один',2:'два',3:'три',4:'четыре'}
        span = f'Кнопка {words[item.slot]}: Телефон.'
        if validation_text.count(span) != 1:
            return None
        validation_text = validation_text.replace(span, f'Кнопка {words[item.slot]}: Сказка.', 1)
    return engine.normalizer.normalize(validation_text)


def current_menu_page(application, session_id, text):
    state = application.canonical_runtime.sessions.get(session_id)
    if state is None or state.mode not in MENU_PCM_MODES.values():
        return None
    page = application.canonical_runtime._menu_page(state)
    if (text != application.bounded_s2s.normalizer.normalize(page.spoken_text)
            or state.scene_menu_descriptor != page.as_dict()):
        return None
    return page


class PublicMenuAudioCache:
    """Compact final PCM, with no stored child/session/playback identity."""
    def __init__(self, max_entries=32, max_bytes=64 * 1024 * 1024):
        if type(max_entries) is not int or not 1 <= max_entries <= 32:
            raise ValueError('public_menu_cache_entry_limit')
        if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError('public_menu_cache_byte_limit')
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._entries = OrderedDict()
        self._source = None
        self._generation = 0
        self._provider_evidence = {}
        self._expected = ()
        self._expected_contract = None
        self.bytes = self.hits = self.misses = 0

    def _provider_fingerprint(self, provider):
        fields = ('provider', 'requested_route', 'model_sha256', 'model_inventory_sha256',
                  'adapter_sha256', 'reference_sha256', 'source_inventory_sha256',
                  'source_revision', 'runtime_profile', 'sample_rate', 'index_sha256')
        method = getattr(provider, 'configuration_readiness', None) or getattr(provider, 'readiness', None)
        snapshot = method() if callable(method) else {}
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        metadata = dict(snapshot.get('worker_info') or {})
        metadata.update({key: snapshot[key] for key in fields if key in snapshot})
        prior = self._provider_evidence.get(id(provider))
        evidence = dict(prior[1]) if prior is not None and prior[0] is provider else {}
        # A temporarily drained worker does not change its configured model.
        # Fresh loaded evidence replaces this identity after a native restart.
        for key in fields:
            value = metadata.get(key)
            if key in metadata and (value is None or type(value) in (str, int, float, bool)):
                evidence[key] = value[:256] if isinstance(value, str) else value
        self._provider_evidence[id(provider)] = provider, evidence
        config = {}
        for key in ('f0_method', 'filter_radius', 'f0_up_key', 'index_rate', 'protect'):
            value = getattr(provider, key, None)
            if value is None or type(value) in (str, int, float, bool):
                config[key] = value
        for key in ('model_path', 'index_path', 'worker_script', 'project_root'):
            value = getattr(provider, key, None)
            if isinstance(value, (str, Path)):
                path = Path(value)
                try:
                    stat = path.stat()
                    config[key] = str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino
                except OSError:
                    config[key] = str(path), None
        return json.dumps([evidence, config], sort_keys=True, separators=(',', ':'))

    def key(self, application, page, text):
        if not isinstance(page, MenuPage) or page.scene not in MENU_PCM_MODES.values():
            return None
        engine = application.bounded_s2s
        tts, voice = providers(application)
        identity = (tts, voice, engine.normalizer, engine.response_validator)
        config = (engine.config, getattr(application.settings, 'voice_conversion', None),
                  getattr(application.settings, 'rvc_fallback_mode', None),
                  self._provider_fingerprint(tts), self._provider_fingerprint(voice))
        if (self._source is None or any(left is not right for left, right in zip(self._source[0], identity))
                or self._source[1] != config):
            self._entries.clear()
            self.bytes = 0
            self._generation += 1
            self._source = identity, config
            self._provider_evidence = {id(provider): self._provider_evidence[id(provider)]
                                       for provider in (tts, voice)}
        descriptor = json.dumps([text, page.as_dict()], sort_keys=True, ensure_ascii=False,
                                separators=(',', ':')).encode('utf-8')
        return self._generation, hashlib.sha256(descriptor).digest()

    def get(self, key, token, session_id):
        from .s2s import AudioChunk
        item = self._entries.get(key)
        if item is None:
            self.misses += 1
            return None
        self.hits += 1
        self._entries.move_to_end(key)
        rate, data = item
        values = array('f')
        values.frombytes(data)
        return AudioChunk(tuple(values), rate, token, session_id)

    def put(self, key, audio):
        if key is None or key[0] != self._generation or not valid_pcm(audio):
            return False
        data = array('f', audio.samples).tobytes()
        size = len(data)
        if size > self.max_bytes:
            return False
        previous = self._entries.pop(key, None)
        if previous:
            self.bytes -= len(previous[1])
        while self._entries and (len(self._entries) >= self.max_entries or self.bytes + size > self.max_bytes):
            _, evicted = self._entries.popitem(last=False)
            self.bytes -= len(evicted[1])
        self._entries[key] = audio.sample_rate, data
        self.bytes += size
        return True

    def snapshot(self):
        return {'entries': len(self._entries), 'bytes': self.bytes, 'hits': self.hits,
                'misses': self.misses, 'max_entries': self.max_entries, 'max_bytes': self.max_bytes}

    def readiness(self, application):
        if self._expected_contract != _public_contract(application):
            return {'ok': False, 'pages': len(self._expected), 'missing': len(self._expected),
                    **self.snapshot()}
        missing = 0
        for page, text in self._expected:
            if self.key(application, page, text) not in self._entries:
                missing += 1
        return {'ok': bool(self._expected) and missing == 0,
                'pages': len(self._expected), 'missing': missing, **self.snapshot()}


def enable_public_menu_audio_cache(application, *, max_entries=32, max_bytes=64 * 1024 * 1024):
    cache = PublicMenuAudioCache(max_entries, max_bytes)
    application._public_menu_audio_cache = cache
    return cache


def public_menu_audio_readiness(application):
    cache = getattr(application, '_public_menu_audio_cache', None)
    return cache.readiness(application) if isinstance(cache, PublicMenuAudioCache) else {'ok': False, 'pages': 0, 'missing': 0}


def _public_contract(application):
    """Cheap live metadata check; actual menu use still verifies asset hashes."""
    from .canonical_contract import IMMERSIVE_GAME_SPECS
    from .menu_capabilities import day_menu_items
    from .personal_story import PersonalStoryService
    stories = application.canonical_runtime.stories
    records = getattr(stories, 'stories', {})
    files = []
    for item in records.values():
        for field in ('audio_file', 'exact_text_file'):
            value = item.get(field)
            if isinstance(value, str):
                path = Path(stories.root) / value
                try:
                    stat = path.stat()
                    files.append((value, stat.st_size, stat.st_mtime_ns, stat.st_ino))
                except OSError:
                    files.append((value, None))
    value = [records, files, [(item.game_id, item.display_title, item.available)
                             for item in IMMERSIVE_GAME_SPECS],
             day_menu_items(application, None), PersonalStoryService(application.bounded_s2s).capable()]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).digest()


def public_menu_audio_plan(application):
    """Enumerate real public catalogue pages without creating any session."""
    from .canonical_contract import IMMERSIVE_GAME_SPECS
    from .menu_capabilities import day_menu_items
    from .personal_story import PersonalStoryService
    engine = application.bounded_s2s
    themes = PersonalStoryService(engine).capable()
    # None cannot be a canonical session ID. The capability helper returns
    # before the profile/schedule lookup, examining only public adapters.
    public_day = tuple(item for item in day_menu_items(application, None) if item[0] in {'date', 'weather'})
    days = ((('date', 'Дата'),), public_day)
    pages = OrderedDict()
    for scene in ('fixed_story_menu', 'personal_story_theme', 'schedule_weather', 'game_menu'):
        if scene == 'personal_story_theme' and not themes:
            continue
        for band in ('1-3', '3-5', '5-7'):
            for day in (days if scene == 'schedule_weather' else ((),)):
                options = dict(stories=application.canonical_runtime.stories,
                               game_specs=IMMERSIVE_GAME_SPECS, personal_story_available=themes,
                               day_items=day)
                first = build_menu_page(scene, band, **options)
                for index in range(first.page_count):
                    page = build_menu_page(scene, band, index, **options)
                    key = json.dumps(page.as_dict(), sort_keys=True, ensure_ascii=False)
                    pages[key] = page
    if len(pages) > 32:
        raise ValueError('public_menu_warmup_page_limit')
    return tuple(pages.values())


async def warm_public_menu_audio(application, *, progress=None):
    cache = getattr(application, '_public_menu_audio_cache', None)
    if not isinstance(cache, PublicMenuAudioCache):
        return {'ok': False, 'pages': 0, 'rendered': 0, 'hits': 0, 'failed': 0, 'entries': 0, 'bytes': 0}
    pages = public_menu_audio_plan(application)
    engine = application.bounded_s2s
    expected, rendered, hits, failed = [], 0, 0, 0
    cache._expected = tuple((page, engine.normalizer.normalize(page.spoken_text)) for page in pages)
    cache._expected_contract = _public_contract(application)
    task = asyncio.current_task()
    def cancelled():
        return bool(task and task.cancelling())
    for index, page in enumerate(pages):
        try:
            text = engine.normalizer.normalize(page.spoken_text)
            if not text or len(text) > engine.config.max_response_chars:
                raise ValueError('public_menu_text_bound')
            validation_text = await menu_validation_text(application, page, text)
            if validation_text is None:
                raise ValueError('public_menu_validation')
            decision = await engine.response_validator.validate_trusted(validation_text)
            if not decision.allowed:
                raise ValueError('public_menu_validation')
            tts, voice = providers(application)
            key = cache.key(application, page, text)
            expected.append(key)
            audio = cache.get(key, 0, None)
            if audio is not None:
                hits += 1
            else:
                async with asyncio.timeout(engine.config.max_turn_seconds):
                    audio = await tts.synthesize(text, 0, cancelled)
                    if cancelled():
                        raise asyncio.CancelledError
                    if not valid_pcm(audio):
                        raise ValueError('public_menu_tts_failed')
                    audio = await voice.convert_private(audio, cancelled)
                if cancelled():
                    raise asyncio.CancelledError
                if not valid_pcm(audio) or cache.key(application, page, text) != key:
                    raise ValueError('public_menu_voice_failed')
                if not cache.put(key, audio):
                    raise ValueError('public_menu_cache_capacity')
                rendered += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            # Provider exceptions may contain source paths; the public report
            # exposes counts only, never exception text or inference inputs.
            failed += 1
        finally:
            audio = None
        if progress is not None:
            update = progress({'completed': index + 1, 'total': len(pages), 'rendered': rendered,
                               'hits': hits, 'failed': failed, 'scene': page.scene, **cache.snapshot()})
            if inspect.isawaitable(update):
                await update
    ok = failed == 0 and all(key in cache._entries for key in expected)
    return {'ok': ok, 'pages': len(pages), 'rendered': rendered, 'hits': hits, 'failed': failed,
            'entries': len(cache._entries), 'bytes': cache.bytes}
