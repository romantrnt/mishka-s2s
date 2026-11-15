from __future__ import annotations

import re, time
from .domain import Intent

class RuleBasedIntentClassifier:
    def classify(self, text, request_id, cancelled=None):
        started=time.perf_counter(); value="unknown"; lower=text.lower()
        rules=(("want_exit",r"пока|выход|стоп"),("want_game",r"игр"),("want_story",r"сказк"),("mood_positive",r"хорош|рад"),("mood_negative",r"груст|плох"),("mood_neutral",r"нормаль"))
        for candidate, pattern in rules:
            if re.search(pattern, lower): value=candidate; break
        return Intent(value, .95 if value != "unknown" else .2, request_id, "rule_based", (time.perf_counter()-started)*1000)

MockIntentClassifier = RuleBasedIntentClassifier










class RecordingAudio:
    private_pcm_supported = True
    def __init__(self): self.commands=[]; self.token=0
    def play(self, response_id, asset_id, token):
        if token == self.token: self.commands.append({"response_id":response_id,"asset_id":asset_id,"token":token})
    def play_owned(self, handle, asset_id, offset_ms=0):
        if handle.token == self.token:
            self.commands.append({
                "response_id": handle.response_id, "asset_id": asset_id, "token": handle.token,
                "session_id": handle.session_id, "generation": handle.generation,
                "playback_id": handle.playback_id, "playback_lease": handle.playback_lease,
                "offset_ms": int(offset_ms),
            })
    def play_pcm_owned(self, handle, audio):
        from .owned_speech import OWNED_PCM_IDS
        if handle.response_id not in OWNED_PCM_IDS:
            raise RuntimeError("private_pcm_invalid_owner")
        if handle.token == self.token:
            self.commands.append({"response_id": handle.response_id, "token": handle.token,
                "session_id": handle.session_id, "generation": handle.generation,
                "playback_id": handle.playback_id, "playback_lease": handle.playback_lease,
                "content_kind": "private_pcm"})
    def play_text(self, response_id, text, token):
        if token == self.token:
            self.commands.append({"response_id":response_id,"resolved_text":text,"content_kind":"action","token":token})
    def cancel(self, token): self.token=token; self.commands.clear()
class RecordingLED:
    def __init__(self):
        from .lighting import FakeLightingOutput
        self.output = FakeLightingOutput()
        self.commands = []

    def set_element(self, element_id, state, transition_ms=0):
        self.output.set_element(element_id, state, transition_ms)

    def set_elements(self, commands):
        self.output.set_elements(commands)

    def clear_all(self, transition_ms=0):
        self.output.clear_all(transition_ms)

    def readiness_wave(self, total_duration_ms=1200):
        from .lighting import LightingController
        return LightingController(self.output).readiness_wave(total_duration_ms)

    def pattern(self, pattern_id, color, duration_ms, token):
        self.commands.append({"pattern":pattern_id,"color":color,"duration_ms":duration_ms,"token":token})

    def pattern_map(self, pattern_id, colors, duration_ms, token):
        self.commands.append({"pattern":pattern_id,"colors":colors,"duration_ms":duration_ms,"token":token})
