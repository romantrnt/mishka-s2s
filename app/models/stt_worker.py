import queue
import threading

import numpy as np

from app.models.stt import SpeechToText


class STTWorker:
    """ Выносим распознование речи в фоновый режим, чтобы основная программа не зависала"""
    def __init__(
        self,
        stt: SpeechToText,
        on_result=None,
    ):
        self.stt = stt
        self.on_result = on_result

        self.queue: queue.Queue[
            np.ndarray | None
        ] = queue.Queue()

        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
        )

    def start(self) -> None:
        """ Активируем фоновый процесс"""
        self.thread.start()

    def submit(
        self,
        audio: np.ndarray,
    ) -> None:
        """ Позволяет закинуть аудио на распознавание не дожидаясь конца обработки"""
        self.queue.put(audio)

    def _run(self) -> None:
        """ Основной цикл воркера, он засыпает если очередь пуста
        и просыпается если поступает элемент """

        while True:

            audio = self.queue.get()

            if audio is None:
                break

            try:

                result = self.stt.transcribe(audio)

                # Расчет метрик
                duration = len(audio) / 16_000
                rtf = result.latency / duration

                if (
                    self.on_result is not None
                    and result.text.strip()
                ):
                    self.on_result(result.text)

            except Exception as exc:

                print(
                    f"STT error: {exc}"
                )

            finally:

                self.queue.task_done()

    def stop(self) -> None:
        """ Отправляет маркер None в очередь и жестко блокирует вызвавший поток через .join(),
        дожидаясь, пока воркер завершит обработку текущего аудио и корректно закроется."""
        self.queue.put(None)
        self.thread.join()