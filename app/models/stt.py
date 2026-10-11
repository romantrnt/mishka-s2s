import time
from dataclasses import dataclass

import numpy as np
from faster_whisper import WhisperModel

# Типовые "галлюцинации" Whisper на тишине/шуме - обрывки субтитров из
# обучающих данных, а не реальная речь ребёнка. vad_filter=True в
# transcribe() отсеивает часть таких случаев, но не все (см. комментарий
# ниже) - этот список ловит оставшееся точным совпадением после
# нормализации. Список конечный и стабильный (это известные артефакты
# Whisper, а не что-то специфичное для наших детей), поэтому не требует
# отдельного этапа с тестами на живой речи, в отличие от порога уверенности
# и retry-логики на невнятную речь.
KNOWN_HALLUCINATIONS: frozenset[str] = frozenset({
    "продолжение следует",
    "спасибо за просмотр",
    "подпишитесь на канал",
    "подписывайтесь на канал",
    "ставьте лайк и подписывайтесь",
    "редактор субтитров",
    "субтитры сделал",
    "субтитры сделала",
    "до новых встреч",
})


@dataclass
class Transcription:
    """ Результат распознавания + сигналы уверенности, на которых оно основано """
    text: str
    latency: float
    duration: float
    # Суммарная длина сегментов речи, которые Whisper нашёл внутри audio
    # через свой собственный vad_filter - ТОЛЬКО для диагностики/логов, не
    # используется в is_uncertain. Проверялось (04.09.2026) как способ
    # отличить короткий обрубок звука от нормальной речи - не сработало:
    # Whisper применяет vad_filter к буферу, который наш VAD и так уже
    # обрезал по границам речи, поэтому внутри почти не остаётся тишины,
    # которую можно было бы исключить - segment.end-segment.start у
    # обрубка 0.25с и у нормальной фразы получались почти одинаковыми
    # (~1-1.2с). Реальная проверка "слишком коротко" теперь делается
    # ВЫЗЫВАЮЩИМ кодом (app/mvp/api.py) по точным сэмплам start/end
    # от самого VAD - см. _process_speech_turn.
    speech_duration: float
    avg_logprob: float | None
    no_speech_prob: float | None
    compression_ratio: float | None
    # True - похоже на неразборчивую речь/галлюцинацию Whisper (пустой
    # результат, слишком короткий отрезок, низкая уверенность, высокая
    # вероятность "не речь" или зацикленный повтор). Отдельно от
    # is_hallucination() ниже - тот ловит точное совпадение с известным
    # списком артефактов, это - более общий сигнал по метрикам самой модели.
    is_uncertain: bool


class SpeechToText:
    """ Класс stt (whisper)"""
    def __init__(
        self,
        model_name: str = "large-v3-turbo", # Ускоренная версия large-v3
        device: str = "cuda", # Для ускорения интеференса берем GPU
        compute_type: str = "float16", # Уменьшение диапазона float позволит модели работать еще быстрее
        low_confidence_avg_logprob: float = -1.0,
        high_no_speech_prob: float = 0.6,
        high_compression_ratio: float = 2.4,
    ):
        self.model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
        ) # Загружаем модель

        self.low_confidence_avg_logprob = low_confidence_avg_logprob
        self.high_no_speech_prob = high_no_speech_prob
        self.high_compression_ratio = high_compression_ratio

    def transcribe(
        self,
        audio: np.ndarray,
        sample_rate: int = 16_000,
    ) -> Transcription:

        start = time.perf_counter()
        duration = len(audio) / sample_rate

        segments, _ = self.model.transcribe(
            audio,
            language="ru", # Русский язык
            beam_size=1, # Убираем перебор, просто берем самый вероятный вариант
            best_of=1, # -/-
            temperature=0.0, # Детерминизм, модель не фантазирует
            condition_on_previous_text=False, # Отключаем контекст предыдущих аудио, уменьшаем вероятность галлюцинаций
            # Наш собственный VAD (см. app/audio/vad.py) режет аудио на
            # отрезки речи ДО того, как они попадут сюда, но иногда
            # пропускает короткий шум/тишину как "речь". Без внутреннего
            # VAD-фильтра Whisper на таких отрезках не возвращает пустую
            # строку, а галлюцинирует - обычно типовыми фразами вроде
            # "Продолжение следует..." (артефакт обучающих субтитров).
            # vad_filter=True - это вторая линия защиты именно от такого.
            vad_filter=True,
        ) # Настройка выходного текста

        # Материализуем один раз - нужно пройтись по сегментам дважды (текст
        # + метрики уверенности), а generator faster-whisper одноразовый.
        segments = list(segments)

        text = " ".join(
            segment.text.strip()
            for segment in segments
        ).strip()

        latency = time.perf_counter() - start

        if segments:
            avg_logprob = sum(s.avg_logprob for s in segments) / len(segments)
            no_speech_prob = max(s.no_speech_prob for s in segments)
            compression_ratio = max(s.compression_ratio for s in segments)
            speech_duration = sum(s.end - s.start for s in segments)
        else:
            avg_logprob = None
            no_speech_prob = None
            compression_ratio = None
            speech_duration = 0.0

        is_uncertain = (
            not text
            or (avg_logprob is not None and avg_logprob < self.low_confidence_avg_logprob)
            or (no_speech_prob is not None and no_speech_prob > self.high_no_speech_prob)
            or (compression_ratio is not None and compression_ratio > self.high_compression_ratio)
        )

        return Transcription(
            text=text,
            latency=latency,
            duration=duration,
            speech_duration=speech_duration,
            avg_logprob=avg_logprob,
            no_speech_prob=no_speech_prob,
            compression_ratio=compression_ratio,
            is_uncertain=is_uncertain,
        )

    @staticmethod
    def is_hallucination(text: str) -> bool:
        """ Похоже ли распознанное на типовой артефакт Whisper, а не на реальную фразу """
        normalized = text.strip().lower().strip(".!?…")
        return normalized in KNOWN_HALLUCINATIONS