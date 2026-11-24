"""Real VAD/STT in a current catalogue; capture phase authority is synthetic.

The retired welcome/mood listener is not an entry fixture. Button/audio callbacks
enter the current chooser; a transport seam revokes ordinary input authority.
"""
import asyncio
import pytest
from test_scene_menu_runtime import Harness
from test_demo_story_offline_contract import UnavailableModels
from app.foundation.s2s import STTResult
from app.foundation.input_safety import InputSafetyDecision


class CaptureHarness(Harness):
    def __init__(self):
        super().__init__()
        self.capture_phase = 0
        self.closed = False
        self.updates = []
        self.models = UnavailableModels()
        self.app.bounded_s2s.llm = self.models
        async def updated(_sid, result):
            self.updates.append(result['reason'])
        self.app.audio_update_hook = updated

    async def listen(self):
        await self.button(2); await self.release(2)
        assert self.state.mode == 'fixed_story_menu' and self.state.scene_menu_phase == 'narrating'
        await self.complete(); await self.complete()
        assert self.state.scene_menu_phase == 'repeat_listen' and self.state.active_audio_id is None
        assert self.app._catalogue_voice_scope(self.sid) is not None
        self.catalogue_owner = self.app._catalogue_voice_scope(self.sid)
        self.narrations = len(self.played)

    def guard(self):
        phase = self.capture_phase
        current = lambda: not self.closed and self.capture_phase == phase
        current.lifecycle_current = lambda: not self.closed
        return current

    async def feed(self, samples, sequence, **kwargs):
        kwargs.setdefault('_ingress_guard', self.guard())
        return await self.app.receive_audio_async(self.sid, samples, sequence, **kwargs)

    async def close(self):
        self.closed = True
        await self.app._cleanup_session_resources(self.sid)
        await super().close()
        assert not self.models.calls


async def settle(tasks):
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 2)


@pytest.mark.parametrize('wait_for_turn', [False, True])
@pytest.mark.parametrize('text,severity,expected', [
    ('Да, повтори пожалуйста', 'none', 'fixed_story_menu'),
    ('Мне угрожают', 'urgent', 'safety_stop'),
    ('Позови взрослого', 'none', 'safety_stop'),
    ('Мне грустно', 'concern', 'support_intake'),
])
def test_old_phase_stt_is_safety_only_after_retry(text, severity, expected, wait_for_turn):
    async def run():
        h = CaptureHarness()
        first_safety, release_safety = asyncio.Event(), asyncio.Event()
        second_stt, release_stt = asyncio.Event(), asyncio.Event()
        class STT:
            calls = 0
            async def transcribe(self, audio, cancelled):
                self.calls += 1
                if self.calls == 2:
                    second_stt.set()
                    await asyncio.wait_for(release_stt.wait(), 2)
                    return STTResult(text, .99, 0., False)
                return STTResult('Да, повтори пожалуйста', .99, 0., False)
        class Safety:
            calls = 0
            async def classify(self, text):
                self.calls += 1
                if self.calls == 1:
                    first_safety.set()
                    await asyncio.wait_for(release_safety.wait(), 2)
                    return InputSafetyDecision('none', 'input_safety_timeout', False)
                return InputSafetyDecision(severity, 'test', True)
        second = None
        try:
            await h.listen()
            h.app.s2s_audio.recognizer = STT()
            safety = Safety(); h.app.safety_classifier = safety
            samples = (.1,) * (512 * 12) + (0.,) * (512 * 8)
            first = await h.feed(samples, 101, wait_for_turn=False)
            assert first['pending']
            await asyncio.wait_for(first_safety.wait(), 2)
            first_completion = tuple(h.app._audio_completions[h.sid])
            second = asyncio.create_task(h.feed(samples, 102, wait_for_turn=wait_for_turn))
            await asyncio.wait_for(second_stt.wait(), 2)
            release_safety.set()
            await settle(first_completion)
            assert h.updates == ['input_safety_retry'] and h.state.voice_retry_count == 1
            assert h.state.scene_menu_phase == 'repeat_listen' and h.state.active_audio_id is None
            # This catalogue does not promise a retry WAV. A replacement
            # capture phase, not a fictitious welcome callback, revokes it.
            h.capture_phase += 1
            assert h.app._catalogue_voice_scope(h.sid) == h.catalogue_owner
            release_stt.set()
            await asyncio.wait_for(second, 2)
            pending = tuple(h.app._audio_completions.get(h.sid, ()))
            if pending: await asyncio.wait_for(asyncio.gather(*pending), 2)
            assert h.app.s2s_audio.recognizer.calls == safety.calls == 2
            assert h.state.mode == expected
            assert len(h.played) == h.narrations and h.state.active_story_id is None
        finally:
            release_safety.set(); release_stt.set()
            if second: await asyncio.wait_for(asyncio.gather(second, return_exceptions=True), 2)
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('text,severity,expected', [
    ('Да, повтори пожалуйста', 'none', 'fixed_story_menu'),
    ('Мне угрожают', 'urgent', 'safety_stop'),
    ('Позови взрослого', 'none', 'safety_stop'),
])
@pytest.mark.parametrize('lifecycle_revoked', [False, True])
def test_transport_phase_revoked_during_safety_await(text, severity, expected, lifecycle_revoked):
    async def run():
        h = CaptureHarness()
        entered, release = asyncio.Event(), asyncio.Event()
        authority = [True]
        class STT:
            calls = 0
            async def transcribe(self, audio, cancelled):
                self.calls += 1
                return STTResult(text, .99, 0., False)
        class Safety:
            calls = 0
            async def classify(self, text):
                self.calls += 1
                entered.set()
                await asyncio.wait_for(release.wait(), 2)
                return InputSafetyDecision(severity, 'test', True)
        try:
            await h.listen()
            h.app.s2s_audio.recognizer = STT()
            h.app.safety_classifier = Safety()
            samples = (.1,) * (512 * 12) + (0.,) * (512 * 8)
            guard = lambda: authority[0]
            guard.lifecycle_current = lambda: not lifecycle_revoked or authority[0]
            await h.feed(samples, 101,
                wait_for_turn=False, _ingress_guard=guard)
            await asyncio.wait_for(entered.wait(), 2)
            authority[0] = False
            assert h.app._catalogue_voice_scope(h.sid) == h.catalogue_owner
            release.set()
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._audio_completions[h.sid])), 2)
            assert h.app.s2s_audio.recognizer.calls == h.app.safety_classifier.calls == 1
            assert h.state.mode == ('fixed_story_menu' if lifecycle_revoked else expected)
            assert len(h.played) == h.narrations and h.state.active_story_id is None
        finally:
            release.set()
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('fragmented', ['speech', 'partial', 'preroll', 'preroll_flushed'])
@pytest.mark.parametrize('text,severity,expected', [
    ('Да, повтори пожалуйста', 'none', 'fixed_story_menu'),
    ('Мне угрожают', 'urgent', 'safety_stop'),
    ('Позови взрослого', 'none', 'safety_stop'),
])
def test_buffered_vad_utterance_keeps_original_phase(text, severity, expected, fragmented):
    async def run():
        h = CaptureHarness()
        entered, release = asyncio.Event(), asyncio.Event()
        class STT:
            calls = 0
            async def transcribe(self, audio, cancelled):
                self.calls += 1
                return STTResult('Да, повтори пожалуйста' if self.calls == 1 else text, .99, 0., False)
        class Safety:
            calls = 0
            async def classify(self, text):
                self.calls += 1
                if self.calls == 1:
                    entered.set(); await asyncio.wait_for(release.wait(), 2)
                    return InputSafetyDecision('none', 'input_safety_timeout', False)
                return InputSafetyDecision(severity, 'test', True)
        try:
            await h.listen()
            h.app.s2s_audio.recognizer = STT()
            h.app.safety_classifier = Safety()
            sid = h.sid
            utterance, silence = (.1,) * (512 * 12), (0.,) * (512 * 8)
            await h.feed(utterance + silence, 201, wait_for_turn=False)
            await asyncio.wait_for(entered.wait(), 2)
            first_completion = tuple(h.app._audio_completions[h.sid])
            # Partial VAD window must retain its origin too, not only active speech.
            before, after = ((utterance[:100], utterance[100:]) if fragmented == 'partial' else
                (((.001,) * (512 * 12), utterance) if fragmented.startswith('preroll') else (utterance, ())))
            await h.feed(before, 202, wait_for_turn=False)
            pipeline = h.app.s2s_audio.ingress[sid]
            origin = pipeline._canonical_private_owner
            assert origin is not None and origin()
            release.set()
            await settle(first_completion)
            assert h.updates == ['input_safety_retry']
            h.capture_phase += 1
            assert not origin()
            assert origin.lifecycle_current()
            assert h.app._catalogue_voice_scope(h.sid) == h.catalogue_owner
            if fragmented == 'preroll_flushed':
                await h.feed((0.,) * (pipeline._pre_roll.maxlen + 512), 203, wait_for_turn=False)
            await h.feed(after + silence, 204, wait_for_turn=False)
            pending = tuple(h.app._audio_completions.get(sid, ()))
            if pending: await asyncio.wait_for(asyncio.gather(*pending), 2)
            assert h.app.s2s_audio.recognizer.calls == h.app.safety_classifier.calls == 2
            fresh_ordinary = fragmented == 'preroll_flushed' and text == 'Да, повтори пожалуйста'
            assert h.state.mode == expected
            assert len(h.played) == h.narrations + int(fresh_ordinary)
            assert h.state.active_story_id is None and pipeline._canonical_private_owner is None
            if text == 'Да, повтори пожалуйста':
                assert h.state.scene_menu_phase == ('narrating' if fresh_ordinary else 'repeat_listen')
            if severity == 'none' and text == 'Да, повтори пожалуйста' and not fresh_ordinary:
                await h.feed(utterance + silence, 205, wait_for_turn=False)
                pending = tuple(h.app._audio_completions.get(sid, ()))
                if pending: await asyncio.wait_for(asyncio.gather(*pending), 2)
                assert h.app.s2s_audio.recognizer.calls == h.app.safety_classifier.calls == 3
                assert h.state.scene_menu_answers['fixed_story_menu'] == 'yes'
                assert h.state.scene_menu_phase == 'narrating' and len(h.played) == h.narrations + 1
        finally:
            release.set(); await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['HandsetPlaced', 'HardwareDisconnected', 'SessionReset'])
def test_partial_vad_owner_is_released_on_terminal_boundary(action):
    async def run():
        h = CaptureHarness()
        try:
            await h.listen()
            sid = h.sid
            await h.feed((.1,) * 100, 301, wait_for_turn=False)
            pipeline = h.app.s2s_audio.ingress[sid]
            assert pipeline._canonical_private_owner is not None
            await h.send(action)
            assert pipeline._canonical_private_owner is None
            assert not pipeline.active and not pipeline._vad_pending
            assert sid not in h.app.s2s_audio.ingress
        finally: await h.close()
    asyncio.run(run())


def test_preroll_rollover_does_not_mix_replacement_phases():
    async def run():
        h = CaptureHarness()
        phase = [0]
        def guard():
            captured = phase[0]
            current = lambda: phase[0] == captured
            current.lifecycle_current = lambda: True
            return current
        class STT:
            calls = 0
            async def transcribe(self, audio, cancelled):
                self.calls += 1
                return STTResult('Да, повтори пожалуйста', .99, 0., False)
        class Safety:
            calls = 0
            async def classify(self, text):
                self.calls += 1
                return InputSafetyDecision('none', 'test', True)
        try:
            await h.listen()
            sid = h.sid
            h.app.s2s_audio.recognizer = STT(); h.app.safety_classifier = Safety()
            await h.feed((.001,) * 6144, 401, wait_for_turn=False, _ingress_guard=guard())
            pipeline = h.app.s2s_audio.ingress[sid]
            origin = pipeline._canonical_private_owner
            assert origin is not None and origin()
            phase[0] = 1
            await h.feed((0.,) * 3072, 402, wait_for_turn=False, _ingress_guard=guard())
            phase[0] = 2
            await h.feed((0.,) * 3072, 403, wait_for_turn=False, _ingress_guard=guard())
            assert pipeline._canonical_private_owner is origin
            assert not origin() and origin.lifecycle_current()
            assert h.app._catalogue_voice_scope(h.sid) == h.catalogue_owner
            assert pipeline._canonical_private_stale_samples == 3072
            await h.feed((.1,) * 6144 + (0.,) * 4096, 404, wait_for_turn=False, _ingress_guard=guard())
            pending = tuple(h.app._audio_completions.get(sid, ()))
            if pending: await asyncio.wait_for(asyncio.gather(*pending), 2)
            assert h.app.s2s_audio.recognizer.calls == h.app.safety_classifier.calls == 1
            assert h.state.mode == 'fixed_story_menu' and h.state.scene_menu_phase == 'repeat_listen'
            assert len(h.played) == h.narrations and h.state.active_story_id is None
        finally: await h.close()
    asyncio.run(run())
