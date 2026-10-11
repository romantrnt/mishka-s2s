"""
Здесь один раз при старте сервера (при первом импорте этого модуля)
загружаются все тяжёлые модели: STT, LLM-клиент и TTS. Дальше все сессии
(все разговоры со всеми "телефонами") переиспользуют одни и те же объекты
stt/llm/tts - грузить модель заново на каждый разговор было бы слишком
медленно и заняло бы слишком много видеопамяти.

Единственное, что создаётся заново под каждую сессию - это VAD и история
диалога (см. create_vad/create_session), потому что у них есть внутреннее
состояние конкретного разговора, которое нельзя шарить между сессиями.
"""

from app.audio.vad import VADConfig, VoiceActivityDetector
from app.config import settings
from app.dialogue.manager import DialogueManager
from app.dialogue.prompt import SYSTEM_PROMPT
from app.dialogue.stream import StreamingDialogue
from app.models.llm import OllamaClient
from app.models.tts import MishkaVoiceTTS
from app.models.stt import SpeechToText
from app.safety import SafetyGuard
from app.scenarios.graph import build_graph


stt = SpeechToText(
    model_name=settings.whisper_model,
    device=settings.whisper_device,
    compute_type=settings.whisper_compute_type,
    low_confidence_avg_logprob=settings.stt_low_confidence_avg_logprob,
    high_no_speech_prob=settings.stt_high_no_speech_prob,
    high_compression_ratio=settings.stt_high_compression_ratio,
)

llm = OllamaClient(
    base_url=settings.ollama_url,
    model=settings.ollama_model,
    num_predict=settings.ollama_num_predict,
    num_ctx=settings.ollama_num_ctx,
    temperature=settings.ollama_temperature,
)

print("OLLAMA URL:", llm.base_url)

tts = MishkaVoiceTTS(
    reference_audio=str(settings.reference_audio),
    device=settings.whisper_device,
    language=settings.tts_language,
    temperature=settings.tts_temperature,
)


safety = SafetyGuard()

# Граф на служебные кнопки (старт диалога, погода) - см. app/scenarios/.
# Собирается один раз при старте, как и остальные тяжёлые объекты выше -
# сам граф лёгкий, но так проще держать одну точку правды на процесс.
scenario_graph = build_graph()

pipeline = StreamingDialogue(
    llm=llm,
    tts=tts,
    safety=safety,
)


def create_session():
    return DialogueManager(
        system_prompt=SYSTEM_PROMPT
    )


def create_vad() -> VoiceActivityDetector:
    """ Создает VAD для новой сессии (модель внутри переиспользуется, поэтому дешево) """
    return VoiceActivityDetector(
        VADConfig(
            sample_rate=settings.sample_rate,
            threshold=settings.vad_threshold,
            min_silence_ms=settings.vad_silence_ms,
            speech_pad_ms=30,
        )
    )