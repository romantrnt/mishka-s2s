from dataclasses import dataclass

import torch
from silero_vad import VADIterator, load_silero_vad


@dataclass
class VADConfig:
    """ Конфигурация silero-vad"""
    sample_rate: int = 16_000 # Дискретизация
    threshold: float = 0.5 # Порог обработки речи (детекции)
    min_silence_ms: int = 400 # Срабатывание окончания речи
    speech_pad_ms: int = 30 # Добавление времени перед и после записи


class VoiceActivityDetector:
    """ """
    def __init__(self, config: VADConfig):
        self.config = config # Настройки

        torch.set_num_threads(1)  # Использование одного потока для меньшей нагрузки на CPU

        model = load_silero_vad() # Загружаем модель

        self.vad = VADIterator(
            model=model,
            threshold=config.threshold,
            sampling_rate=config.sample_rate,
            min_silence_duration_ms=config.min_silence_ms,
            speech_pad_ms=config.speech_pad_ms,
        ) # Специальный потоковый класс от Silero.
        # Он хранит состояние (state) внутри себя и ожидает на вход аудио небольшими порциями (чанками) строго фиксированного размера

    def process(self, audio: torch.Tensor) -> dict[str, int] | None:
        """ Основной процесс записи.
        Логика: если в результате есть 'start' то запись начата, если 'end' -
        окончена. Значение - позиция в сэмплах (см. VADIterator.__call__) -
        ВАЖНО отдавать её наружу как есть, а не сворачивать в строку: это
        единственный точный сигнал о том, ГДЕ реально была речь, в отличие
        от факта "буфер накапливался, пока was triggered=True" - triggered
        остаётся True весь hangover (min_silence_duration_ms), поэтому сам
        по себе не годится для измерения длительности речи."""
        if audio.ndim > 1:
            audio = audio.squeeze()

        audio = audio.float()

        return self.vad(audio)

    def reset(self) -> None:
        """ Сброс состояния нейросети перед началом нового диалога"""
        self.vad.reset_states()