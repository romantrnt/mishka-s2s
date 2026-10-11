import os
import time

import numpy as np

# XTTS-v2 распространяется по лицензии CPML (некоммерческая). При первой
# загрузке модели библиотека спрашивает согласие с лицензией через input()
# в консоли. Внутри Docker-контейнера отвечать на этот вопрос некому, и
# процесс просто зависнет. Поэтому подтверждаем согласие заранее через
# переменную окружения - строчка должна стоять ДО импорта TTS.
os.environ.setdefault("COQUI_TOS_AGREED", "1")

from TTS.api import TTS  # noqa: E402 (импорт после os.environ - так и должно быть)


class MishkaVoiceTTS:
    """
    TTS с клонированием голоса персонажа (XTTS-v2, библиотека coqui-tts).

    Как это работает:
    Обычный TTS умеет говорить только заранее обученными "дикторскими" голосами.
    XTTS-v2 - модель voice cloning: ей один раз показывают короткую запись
    нужного голоса (референс), и дальше она умеет озвучивать ЛЮБОЙ текст
    этим голосом, даже фразы, которых в референсе не было.

    Почему выбрана именно XTTS-v2:
    - официально поддерживает русский язык;
    - клонирование голоса нужно только по одному короткому wav-файлу,
      без дообучения модели - это и есть "лёгкое" решение voice cloning,
      которое просили в задании;
    - работает полностью локально на GPU, без обращения к внешним API.

    Про задержку:
    "Клонирование" голоса (get_conditioning_latents) - относительно медленная
    операция, но её достаточно сделать один раз при старте сервера. Дальше
    для каждой фразы мы переиспользуем уже готовое описание голоса
    (self._gpt_cond_latent, self._speaker_embedding), а не пересчитываем
    его заново - это и есть основная оптимизация задержки в этом классе.
    """

    # XTTS-v2 всегда генерирует звук с частотой дискретизации 24000 Гц.
    # Это свойство самой модели, изменить его нельзя.
    SAMPLE_RATE = 24_000

    def __init__(
        self,
        reference_audio: str,
        device: str = "cuda",
        language: str = "ru",
        temperature: float = 0.65,
    ):
        self.sample_rate = self.SAMPLE_RATE
        self.language = language
        self.temperature = temperature

        print("Loading XTTS-v2...")

        # При первом запуске веса модели (~2 ГБ) скачиваются из интернета
        # и кешируются на диск. При повторных запусках модель просто
        # загружается с диска, скачивание не повторяется.
        self._tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(device)

        # Обёртка TTS() удобна для простых вызовов (tts.tts_to_file(...)), но
        # каждый такой вызов заново готовит голос из референса, что медленно.
        # Поэтому берём "сырую" модель внутри обёртки и работаем с ней напрямую.
        self._model = self._tts.synthesizer.tts_model

        print(f"Cloning voice from reference file: {reference_audio}")

        # Считаем описание голоса Мишки один раз при старте.
        self._gpt_cond_latent, self._speaker_embedding = (
            self._model.get_conditioning_latents(
                audio_path=[reference_audio],
            )
        )

        print("Voice cloned, XTTS-v2 ready.")

    def synthesize(self, text: str):
        """ Синтезируем фразу голосом Мишки + вычисляем метрики задержки"""

        start = time.perf_counter()

        result = self._model.inference(
            text=text,
            language=self.language,
            gpt_cond_latent=self._gpt_cond_latent,
            speaker_embedding=self._speaker_embedding,
            # Небольшая случайность в генерации, чтобы голос не звучал
            # монотонно-механически. Слишком большое значение вредит
            # разборчивости речи, поэтому берём значение ниже, чем по умолчанию.
            temperature=self.temperature,
        )

        latency = time.perf_counter() - start

        audio = np.asarray(result["wav"], dtype=np.float32)

        duration = len(audio) / self.sample_rate
        rtf = latency / duration if duration > 0 else 0.0  # real-time factor

        return audio, latency, rtf

    def synthesize_stream(self, text: str):
        """
        Потоковый синтез: отдаёт аудио небольшими кусками по мере готовности
        (model.inference_stream), вместо того чтобы ждать, пока сгенерируется
        вся фраза целиком (synthesize() выше). XTTS-v2 - авторегрессионная
        модель, декодирует аудио постепенно - inference_stream просто отдаёт
        уже готовые куски сразу, не дожидаясь конца. Живой замер (07.09.2026):
        время до ПЕРВОГО куска ~0.6с вместо ~2-11с до готовности всей фразы
        через synthesize() - именно это и было главным вкладом в задержку
        ответа (см. ARCHITECTURE.md).

        Как и OllamaClient.generate() для токенов LLM: первый кусок несёт
        время до него (аналог TTFA), остальные - latency=None.
        """

        start = time.perf_counter()
        first = True

        for chunk in self._model.inference_stream(
            text=text,
            language=self.language,
            gpt_cond_latent=self._gpt_cond_latent,
            speaker_embedding=self._speaker_embedding,
            temperature=self.temperature,
        ):
            audio = chunk.detach().cpu().numpy().astype(np.float32)

            if first:
                latency = time.perf_counter() - start
                first = False
                yield audio, latency
            else:
                yield audio, None
