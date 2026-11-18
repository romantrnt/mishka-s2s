"""Public menu cache boundaries; providers are synthetic and never use a GPU."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.foundation.public_menu_audio import (
    PublicMenuAudioCache, enable_public_menu_audio_cache, menu_validation_text,
    public_menu_audio_plan, public_menu_audio_readiness,
)
from app.foundation.s2s import AudioChunk
from test_scene_menu_runtime import Harness


class Validator:
    async def validate_trusted(self, text, **kwargs):
        return SimpleNamespace(allowed=True)
    async def validate(self, text, **kwargs):
        return SimpleNamespace(allowed=True)


class Voice:
    def __init__(self):
        self.tts_calls = self.voice_calls = 0
        self.entered, self.release = asyncio.Event(), None
        self.resist = False
        self.model_sha256 = 'a' * 64
    def configuration_readiness(self):
        return {'model_sha256': self.model_sha256}
    async def synthesize(self, text, token, cancelled):
        self.tts_calls += 1
        self.entered.set()
        if self.release is not None:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.resist:
                    raise
                await self.release.wait()
        return AudioChunk((.125, .25), 24000, token)
    async def convert_private(self, audio, cancelled):
        self.voice_calls += 1
        return replace(audio, samples=(.5, .75), sample_rate=16000)


def setup():
    h = Harness()
    voice = Voice()
    engine = h.app.bounded_s2s
    engine.tts = engine.voice = voice
    engine.response_validator = Validator()
    h.state.mode = 'game_menu'
    page = h.app.canonical_runtime._menu_page(h.state)
    h.state.scene_menu_descriptor = page.as_dict()
    cache = enable_public_menu_audio_cache(h.app)
    return h, voice, cache, page


async def render(h, page, token=1, content_id='MENU_GAME', current=lambda: True):
    return await h.app._render_typed_audio(h.sid, page.spoken_text,
        {'content_id': content_id}, token, current)


@pytest.mark.asyncio
async def test_hit_uses_final_voice_pcm_without_extra_inference_and_rebinds_owner():
    h, voice, cache, page = setup()
    try:
        first = await render(h, page, token=1)
        second = await render(h, page, token=92)
        assert (voice.tts_calls, voice.voice_calls) == (1, 1)
        assert second is not first and second.samples == (.5, .75)
        assert second.sample_rate == 16000 and second.token == 92 and second.session_id == h.sid
        assert cache.snapshot()['entries'] == 1 and cache.snapshot()['hits'] == 1
        assert not hasattr(cache, 'save')
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['title', 'mapping', 'provider', 'config', 'loaded_model'])
async def test_changed_public_contract_or_loaded_provider_misses(change):
    h, voice, cache, page = setup()
    try:
        await render(h, page)
        if change in {'title', 'mapping'}:
            item = replace(page.items[0], **({'title': 'Новый свет'} if change == 'title' else {'id': 'new-public-id'}))
            text = page.spoken_text.replace(page.items[0].title, item.title)
            page = replace(page, items=(item, *page.items[1:]), spoken_text=text)
            h.app.canonical_runtime._menu_page = lambda state: page
            h.state.scene_menu_descriptor = page.as_dict()
        if change == 'provider':
            h.app.bounded_s2s.voice = Voice()
        if change == 'config':
            h.app.bounded_s2s.config = replace(h.app.bounded_s2s.config, tts_timeout_s=11)
        if change == 'loaded_model':
            voice.model_sha256 = 'b' * 64
        assert await render(h, page) is not None
        assert voice.tts_calls == 2
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_hit_still_validates_live_mapping_and_trusted_text_first():
    h, voice, cache, page = setup()
    try:
        await render(h, page)
        h.state.scene_menu_descriptor['items'][0]['id'] = 'obsolete'
        assert await render(h, page) is None
        h.state.scene_menu_descriptor = page.as_dict()
        async def blocked(*args, **kwargs):
            return SimpleNamespace(allowed=False, reason='blocked')
        h.app.bounded_s2s.response_validator.validate_trusted = blocked
        assert await render(h, page) is None
        assert voice.tts_calls == 1 and cache.hits == 0
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('content_id', ['TYPED_DATE', 'TYPED_WEATHER', 'TYPED_WHAT_NEXT', 'PERSONAL_WELCOME', 'GAME_PROMPT'])
async def test_non_menu_content_is_never_cached(content_id):
    h, voice, cache, page = setup()
    try:
        await render(h, page, content_id=content_id)
        await render(h, page, content_id=content_id)
        assert voice.tts_calls == 2 and cache.snapshot()['entries'] == 0
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cause', ['stale', 'timeout'])
async def test_stale_or_cancellation_resistant_late_result_cannot_fill_cache(cause):
    h, voice, cache, page = setup()
    voice.release = asyncio.Event()
    live = [True]
    if cause == 'timeout':
        voice.resist = True
        h.app.audio.owned_render_timeout_s = .02
    pending = asyncio.create_task(render(h, page, current=lambda: live[0]))
    try:
        await voice.entered.wait()
        if cause == 'stale':
            live[0] = False
            voice.release.set()
        assert await pending is None
        voice.release.set()
        if h.app._owned_text_jobs:
            await asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True)
        assert cache.snapshot()['entries'] == 0
    finally:
        voice.release.set()
        await h.close()


def test_entry_and_compact_pcm_byte_bounds():
    h, voice, _, page = setup()
    cache = PublicMenuAudioCache(max_entries=2, max_bytes=16)
    for index in range(3):
        key = cache.key(h.app, replace(page, page=index), page.spoken_text)
        assert cache.put(key, AudioChunk((.5, .75), 16000, 0))
    assert cache.snapshot()['entries'] == 2 and cache.bytes == 16
    key = cache.key(h.app, page, page.spoken_text)
    assert not cache.put(key, AudioChunk((.1,) * 5, 16000, 0))
    assert cache.bytes <= 16


@pytest.mark.asyncio
async def test_warmup_uses_real_public_pages_without_sessions_owner_or_playback():
    h, voice, cache, _ = setup()
    try:
        h.app.canonical_runtime.sessions.clear()
        h.app.audio.private_pcm_supported = False
        def forbidden(*args, **kwargs):
            raise AssertionError('warmup queried a patient or dispatched audio')
        h.app._canonical_profiles = SimpleNamespace(get=forbidden)
        h.app.audio.play_pcm_owned = forbidden
        plan = public_menu_audio_plan(h.app)
        assert 1 <= len(plan) <= 32 and plan[-1].scene == 'game_menu'
        assert not any(page.scene == 'personal_story_theme' for page in plan)
        updates = []
        result = await h.app.warm_public_menu_audio(progress=updates.append)
        assert result['ok'] and result['rendered'] == len(plan) and result['entries'] == len(plan)
        assert voice.tts_calls == voice.voice_calls == len(plan)
        assert len(updates) == len(plan) and updates[-1]['completed'] == len(plan)
        again = await h.app.warm_public_menu_audio()
        assert again['ok'] and again['rendered'] == 0 and again['hits'] == len(plan)
        assert voice.tts_calls == len(plan) and not h.app.canonical_runtime.sessions
        assert public_menu_audio_readiness(h.app)['ok']
        cache._entries.pop(next(iter(cache._entries)))
        assert not public_menu_audio_readiness(h.app)['ok']
    finally:
        # Restore the ordinary empty identity repository for shutdown cleanup.
        h.app._canonical_profiles = {}
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('approved_hash', [
    '239c1c3d0308d2a20693c8818dfd5c77a04a1d030f0e69554b2c3cd4971637db',
    '21a29500591e0e085b4d78a6e715167e92e7e3fa87594cee617d39793e9574ec',
])
async def test_telefon_exception_remains_exact_not_a_blanket_catalogue_bypass(approved_hash):
    h, voice, cache, page = setup()
    try:
        from app.foundation.menu_content import MenuItem
        page = replace(page, scene='fixed_story_menu', items=(MenuItem(1, 'fixed_neznayka_v1', 'Телефон'),),
                       spoken_text='Кнопка один: Телефон. Пять — помощь. Семь — назад.')
        async def trusted(text, **kwargs):
            return SimpleNamespace(allowed='Телефон' not in text, reason='personal_data')
        h.app.bounded_s2s.response_validator.validate_trusted = trusted
        record = {'display_title': 'Телефон', 'audio_sha256': approved_hash}
        h.app.canonical_runtime.stories = SimpleNamespace(get_verified=lambda story_id: record)
        assert await menu_validation_text(h.app, page, page.spoken_text) == page.spoken_text.replace('Телефон', 'Сказка')
        record['audio_sha256'] = '0' * 64
        assert await menu_validation_text(h.app, page, page.spoken_text) is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_provider_replaced_while_validating_cannot_poison_new_identity():
    h, original, cache, page = setup()
    replacement = Voice()
    replacement.model_sha256 = 'b' * 64
    async def validate(text, **kwargs):
        if text == page.spoken_text:
            h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = replacement
            await asyncio.sleep(0)
        return SimpleNamespace(allowed=True)
    h.app.bounded_s2s.response_validator.validate_trusted = validate
    try:
        assert await render(h, page) is not None
        assert original.tts_calls == original.voice_calls == 0
        assert replacement.tts_calls == replacement.voice_calls == 1
        assert await render(h, page) is not None
        assert replacement.tts_calls == 1 and cache.hits == 1
    finally:
        await h.close()
