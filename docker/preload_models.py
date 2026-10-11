"""
Скрипт для "прогрева" тяжёлых ML-моделей при старте контейнера.

Зачем он нужен: веса моделей (Whisper, XTTS-v2) весят гигабайты и при первой
загрузке скачиваются из интернета. Если этого не сделать заранее, первый
реальный разговор с Мишкой будет ждать несколько минут загрузки моделей.
Здесь мы один раз их загружаем и кешируем на диск (см. volumes в
docker-compose.yaml), чтобы дальше FastAPI-приложение поднималось быстро.
"""

import os

# XTTS-v2 распространяется по лицензии CPML и при первой загрузке спрашивает
# согласие через input(). В контейнере отвечать некому - подтверждаем заранее.
os.environ.setdefault("COQUI_TOS_AGREED", "1")

import torch
from faster_whisper import WhisperModel
from TTS.api import TTS


print("=" * 60)
print("PRELOADING MODELS")
print("=" * 60)

print()
print("Loading Whisper: large-v3-turbo")

whisper_model = WhisperModel(
    "large-v3-turbo",
    device="cuda",
    compute_type="float16",
)

print("Whisper loaded.")

print()
print("Warming up Whisper on Mishka's reference audio...")

# Загрузка весов - это ещё не полный прогрев: реальная компиляция/выбор
# CUDA-ядер (ctranslate2/cuDNN) происходит только на первом настоящем
# вызове transcribe(). Без этого шага первый разговор пользователя
# получал бы эти же лишние секунды задержки "живьём".
segments, _ = whisper_model.transcribe(
    "assets/reference/mishka_24k_mono.wav",
    language="ru",
)
list(segments)

print("Whisper warm-up complete.")

print()
print("Loading XTTS-v2...")

tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cuda")

print("XTTS-v2 loaded.")


print()
print("Warming up voice cloning on Mishka's reference voice...")

model = tts.synthesizer.tts_model

gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(
    audio_path=["assets/reference/mishka_24k_mono.wav"],
)

with torch.inference_mode():
    model.inference(
        text="Привет.",
        language="ru",
        gpt_cond_latent=gpt_cond_latent,
        speaker_embedding=speaker_embedding,
    )

print("Voice cloning warm-up complete.")

print()
print("CUDA CHECK")
print(f"PyTorch: {torch.__version__}")
print(f"CUDA: {torch.version.cuda}")
print(f"GPU: {torch.cuda.get_device_name(0)}")

print()
print("=" * 60)
print("ALL MODELS PRELOADED")
print("=" * 60)
