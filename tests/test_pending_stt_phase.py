"""Real VAD/STT ingress must retain its listening-phase owner across awaits."""
import asyncio
import pytest
from test_private_welcome import setup, start, deadline
from app.foundation.s2s import STTResult
from app.foundation.input_safety import InputSafetyDecision


@pytest.mark.parametrize('wait_for_turn', [False, True])
@pytest.mark.parametrize('text,severity,expected', [
    ('Хорошо', 'none', 'personal_welcome_listen'),
    ('Мне угрожают', 'urgent', 'safety_handoff'),
    ('Позови взрослого', 'none', 'safety_handoff'),
    ('Мне грустно', 'concern', 'safety_handoff'),
])
def test_old_phase_stt_is_safety_only_after_retry(text, severity, expected, wait_for_turn):
    async def run():
        h, _ = setup()
        first_safety, release_safety = asyncio.Event(), asyncio.Event()
        second_stt, release_stt = asyncio.Event(), asyncio.Event()
        class STT:
            calls = 0
            async def transcribe(self, audio, cancelled):
                self.calls += 1
                if self.calls == 2:
                    second_stt.set()
                    await release_stt.wait()
                    return STTResult(text, .99, 0., False)
                return STTResult('Хорошо', .99, 0., False)
        class Safety:
            calls = 0
            async def classify(self, text):
                self.calls += 1
                if self.calls == 1:
                    first_safety.set()
                    await release_safety.wait()
                    return InputSafetyDecision('none', 'input_safety_timeout', False)
                return InputSafetyDecision(severity, 'test', True)
        second = None
        try:
            await start(h); await deadline(h); await h.complete()
            h.app.s2s_audio.recognizer = STT()
            safety = Safety(); h.app.safety_classifier = safety
            samples = (.1,) * (512 * 12) + (0.,) * (512 * 8)
            first = await h.app.receive_audio_async(h.boundary.session_id, samples, 101, wait_for_turn=False)
            assert first['pending']
            await asyncio.wait_for(first_safety.wait(), 2)
            second = asyncio.create_task(h.app.receive_audio_async(h.boundary.session_id, samples, 102, wait_for_turn=wait_for_turn))
            await asyncio.wait_for(second_stt.wait(), 2)
            release_safety.set()
            for _ in range(100):
                if h.state.active_audio_id == 'PHRASE_074': break
                await asyncio.sleep(.001)
            assert h.state.active_audio_id == 'PHRASE_074'
            await h.complete()
            assert h.state.mode == 'personal_welcome_listen'
            release_stt.set()
            await asyncio.wait_for(second, 2)
            pending = tuple(h.app._audio_completions.get(h.boundary.session_id, ()))
            if pending: await asyncio.wait_for(asyncio.gather(*pending), 2)
            assert safety.calls == 2  # stale text still inspected for danger/help
            assert h.state.mode == expected
            assert 'PHRASE_007' not in h.played
        finally:
            release_safety.set(); release_stt.set()
            if second: await asyncio.gather(second, return_exceptions=True)
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('text,severity,expected', [
    ('Хорошо', 'none', 'personal_welcome_listen'),
    ('Мне угрожают', 'urgent', 'safety_handoff'),
    ('Позови взрослого', 'none', 'safety_handoff'),
])
@pytest.mark.parametrize('lifecycle_revoked', [False, True])
def test_transport_phase_revoked_during_safety_await(text, severity, expected, lifecycle_revoked):
    async def run():
        h, _ = setup()
        entered, release = asyncio.Event(), asyncio.Event()
        authority = [True]
        class STT:
            async def transcribe(self, audio, cancelled):
                return STTResult(text, .99, 0., False)
        class Safety:
            async def classify(self, text):
                entered.set()
                await release.wait()
                return InputSafetyDecision(severity, 'test', True)
        try:
            await start(h); await deadline(h); await h.complete()
            h.app.s2s_audio.recognizer = STT()
            h.app.safety_classifier = Safety()
            samples = (.1,) * (512 * 12) + (0.,) * (512 * 8)
            guard = lambda: authority[0]
            guard.lifecycle_current = lambda: not lifecycle_revoked or authority[0]
            await h.app.receive_audio_async(h.boundary.session_id, samples, 101,
                wait_for_turn=False, _ingress_guard=guard)
            await asyncio.wait_for(entered.wait(), 2)
            authority[0] = False
            release.set()
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._audio_completions[h.boundary.session_id])), 2)
            assert h.state.mode == ('personal_welcome_listen' if lifecycle_revoked else expected)
            assert 'PHRASE_007' not in h.played
        finally:
            release.set()
            await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('fragmented', ['speech', 'partial', 'preroll', 'preroll_flushed'])
@pytest.mark.parametrize('text,severity,expected', [
    ('Хорошо', 'none', 'personal_welcome_listen'),
    ('Мне угрожают', 'urgent', 'safety_handoff'),
    ('Позови взрослого', 'none', 'safety_handoff'),
])
def test_buffered_vad_utterance_keeps_original_phase(text, severity, expected, fragmented):
    async def run():
        h, _ = setup()
        entered, release = asyncio.Event(), asyncio.Event()
        class STT:
            calls = 0
            async def transcribe(self, audio, cancelled):
                self.calls += 1
                return STTResult('Хорошо' if self.calls == 1 else text, .99, 0., False)
        class Safety:
            calls = 0
            async def classify(self, text):
                self.calls += 1
                if self.calls == 1:
                    entered.set(); await release.wait()
                    return InputSafetyDecision('none', 'input_safety_timeout', False)
                return InputSafetyDecision(severity, 'test', True)
        try:
            await start(h); await deadline(h); await h.complete()
            h.app.s2s_audio.recognizer = STT()
            h.app.safety_classifier = Safety()
            sid = h.boundary.session_id
            utterance, silence = (.1,) * (512 * 12), (0.,) * (512 * 8)
            await h.app.receive_audio_async(sid, utterance + silence, 201, wait_for_turn=False)
            await asyncio.wait_for(entered.wait(), 2)
            # Partial VAD window must retain its origin too, not only active speech.
            before, after = ((utterance[:100], utterance[100:]) if fragmented == 'partial' else
                (((.001,) * (512 * 12), utterance) if fragmented.startswith('preroll') else (utterance, ())))
            await h.app.receive_audio_async(sid, before, 202, wait_for_turn=False)
            pipeline = h.app.s2s_audio.ingress[sid]
            release.set()
            for _ in range(100):
                if h.state.active_audio_id == 'PHRASE_074': break
                await asyncio.sleep(.001)
            assert h.state.active_audio_id == 'PHRASE_074'
            await h.complete()
            if fragmented == 'preroll_flushed':
                await h.app.receive_audio_async(sid, (0.,) * (pipeline._pre_roll.maxlen + 512), 203, wait_for_turn=False)
            await h.app.receive_audio_async(sid, after + silence, 204, wait_for_turn=False)
            pending = tuple(h.app._audio_completions.get(sid, ()))
            if pending: await asyncio.wait_for(asyncio.gather(*pending), 2)
            assert h.app.safety_classifier.calls == 2
            fresh_ordinary = fragmented == 'preroll_flushed' and text == 'Хорошо'
            assert h.state.mode == ('exhibition_menu_tutorial_prompt' if fresh_ordinary else expected)
            assert ('PHRASE_007' in h.played) == fresh_ordinary
            assert pipeline._canonical_private_owner is None
            if severity == 'none' and text == 'Хорошо' and not fresh_ordinary:
                await h.app.receive_audio_async(sid, utterance + silence, 205, wait_for_turn=False)
                pending = tuple(h.app._audio_completions.get(sid, ()))
                if pending: await asyncio.wait_for(asyncio.gather(*pending), 2)
                assert h.played[-1] == 'PHRASE_007'
        finally:
            release.set(); await h.close()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['HandsetPlaced', 'HardwareDisconnected', 'SessionReset'])
def test_partial_vad_owner_is_released_on_terminal_boundary(action):
    async def run():
        h, _ = setup()
        try:
            await start(h); await deadline(h); await h.complete()
            sid = h.boundary.session_id
            await h.app.receive_audio_async(sid, (.1,) * 100, 301, wait_for_turn=False)
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
        h, _ = setup()
        phase = [0]
        def guard():
            captured = phase[0]
            current = lambda: phase[0] == captured
            current.lifecycle_current = lambda: True
            return current
        class STT:
            async def transcribe(self, audio, cancelled):
                return STTResult('Хорошо', .99, 0., False)
        class Safety:
            async def classify(self, text):
                return InputSafetyDecision('none', 'test', True)
        try:
            await start(h); await deadline(h); await h.complete()
            sid = h.boundary.session_id
            h.app.s2s_audio.recognizer = STT(); h.app.safety_classifier = Safety()
            await h.app.receive_audio_async(sid, (.001,) * 6144, 401, wait_for_turn=False, _ingress_guard=guard())
            pipeline = h.app.s2s_audio.ingress[sid]
            origin = pipeline._canonical_private_owner
            phase[0] = 1
            await h.app.receive_audio_async(sid, (0.,) * 3072, 402, wait_for_turn=False, _ingress_guard=guard())
            phase[0] = 2
            await h.app.receive_audio_async(sid, (0.,) * 3072, 403, wait_for_turn=False, _ingress_guard=guard())
            assert pipeline._canonical_private_owner is origin
            assert pipeline._canonical_private_stale_samples == 3072
            await h.app.receive_audio_async(sid, (.1,) * 6144 + (0.,) * 4096, 404, wait_for_turn=False, _ingress_guard=guard())
            pending = tuple(h.app._audio_completions.get(sid, ()))
            if pending: await asyncio.wait_for(asyncio.gather(*pending), 2)
            assert h.state.mode == 'personal_welcome_listen'
            assert 'PHRASE_007' not in h.played
        finally: await h.close()
    asyncio.run(run())
