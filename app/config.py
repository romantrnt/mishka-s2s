from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    # Application
    host: str = "127.0.0.1"
    port: int=8000

    # Audio
    sample_rate: int = 16000
    channels: int = 1
    audio_chunk_ms: int = 32

    # VAD
    vad_threshold: float=0.5
    vad_min_speech_ms: int=150
    vad_silence_ms: int = 400

    # STT
    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "cuda"
    whisper_compute_type: str = "float16"
    whisper_language: str = "ru"
    # Пороги неуверенности/галлюцинации - те же значения, что использует сам
    # оригинальный openai-whisper для той же задачи (compression_ratio_threshold,
    # logprob_threshold, no_speech_threshold в его transcribe()) - не выдуманы
    # с потолка, но и не откалиброваны отдельно на живой невнятной речи детей,
    # см. PROGRESS.md/раздел STT.
    stt_low_confidence_avg_logprob: float = -1.0
    stt_high_no_speech_prob: float = 0.6
    stt_high_compression_ratio: float = 2.4
    # Короче этого (по точным сэмплам start/end от VAD, см.
    # app/mvp/api.py _process_speech_turn) - даже "уверенная" по
    # метрикам расшифровка ненадёжна: Whisper на очень коротких отрезках
    # (кашель/щелчок трубки/вдох) может выдать не пустую строку и не низкую
    # уверенность, а полностью придуманное, но правдоподобное предложение.
    # НЕ передаётся в SpeechToText - длину буфера/сегментов Whisper
    # проверяли 04.09.2026, оба варианта раздуты хвостом VAD-тишины и не
    # годятся для этой проверки (см. PROGRESS.md).
    stt_min_duration_s: float = 0.3
    # Если ребёнок говорит без паузы дольше этого - принудительно "закрываем"
    # отрезок речи и отправляем на распознавание, не дожидаясь тишины от VAD
    # (см. handle_audio_chunk в app/mvp/api.py), иначе буфер рос бы
    # неограниченно при отсутствии паузы/сбое VAD.
    stt_max_speech_s: float = 12.0

    # Ollama
    # Порт 11434 - это порт Ollama по умолчанию.
    ollama_url: str = "http://127.0.0.1:11434"
    # Legacy pipeline setting; the foundation uses LLM_MODEL.
    ollama_model: str = "qwen3:14b"
    # Сколько токенов максимум может сгенерировать LLM за один ответ.
    # Мишка должен отвечать коротко, поэтому лимит небольшой. Уменьшено со
    # 120: при 120 маленькая модель (3B) иногда успевала не только ответить,
    # но и придумать реплику ребёнка и ответить уже ей (диалог сам с собой).
    ollama_num_predict: int = 60
    # Явный размер контекста (в токенах) - без него Ollama использует свой
    # дефолт, и при его достижении начинает вытеснять начало последовательности
    # (системный промпт), что ломает переиспользование кэша между ходами.
    # Значение подобрано "на глаз" 2026-09-02, см. комментарий в app/models/llm.py.
    ollama_num_ctx: int = 4096
    # Понижено с 0.8 (07.09.2026) - на живом тесте модель на этом значении
    # временами "заносило" в странные обороты и нарушения стиля (обращение
    # на "вы" вместо "ты", придуманные обращения вроде "Мышонок" - при том,
    # что промпт явно это запрещает). Ниже температура - менее "живой", но
    # более предсказуемый и управляемый инструкциями ответ. Не панацея (3B
    # модель есть 3B модель), но дешёвая мера предосторожности.
    ollama_temperature: float = 0.6

    # TTS / Voice Cloning
    # Референсный голос Мишки, с которого клонируется голос для TTS (XTTS-v2).
    reference_audio: Path = Path("assets/reference/mishka_24k_mono.wav")
    tts_language: str = "ru"
    # Понижено с 0.65 (11.09.2026) - на слух сравнивались значения
    # 0.3/0.5/0.65/0.8 на живых сэмплах: 0.5 звучит лучше 0.3, не хуже 0.65
    # (местами 0.65 казался чуть хуже), а 0.8 заметно "заедает" (не панацея
    # от нестабильности - см. ту же логику про LLM temperature выше, здесь
    # речь о звуке, не о тексте). Speed 0.9/1.1 тоже сравнивались - хуже
    # дефолта 1.0 в обе стороны, поэтому speed не трогаем.
    tts_temperature: float = 0.5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf_8",
        extra="ignore",
    )

settings = Settings()
