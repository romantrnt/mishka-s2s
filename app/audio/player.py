import queue
import threading

import numpy as np
import sounddevice as sd


class AudioPlayer:
    def __init__(
        self,
        sample_rate: int = 48000,
        blocksize: int = 1024,
    ):
        self.sample_rate = sample_rate # Частота дискретизации (48кГц), высокое качество
        self.blocksize = blocksize # Размер блока данных, отправляемого за один раз
        self.queue = queue.Queue() # Создание очереди
        self.stream = None
        self.thread = None
        self.finished = threading.Event() # Флаг для системы, когда очередь станет пустой и процесс закончится

    def start(self):
        """ Функция запуска"""
        self.stream = sd.OutputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self.blocksize,
        ) # Создается моноканал, работающий с float32
        self.stream.start() # Начинаем аудио-стрим
        self.thread = threading.Thread(
            target=self._playback_worker,
            daemon=True,
        )
        self.thread.start()

    def _playback_worker(self):
        """ Сердце плеера"""
        try:
            while True:
                audio = self.queue.get() # Ждем пока в очереди не появиться аудио-запись
                if audio is None:
                    self.queue.task_done()
                    break # Если из очереди возвращается None, заканчиваем цикл

                audio = np.asarray(
                    audio,
                    dtype=np.float32,
                ) # Превращаем в массив, с определенным типом данных
                self.stream.write(
                    audio.reshape(-1, 1)
                ) # Для sounddevice обязательно, чтобы подавалась матрица
                self.queue.task_done()

        finally:
            self.finished.set() # при любом выходе из цикла взводит флаг, сообщая, что поток успешно завершен.

    def put(self, audio):
        """ Добавляет кусок аудио в очередь"""
        self.queue.put(audio)

    def wait_until_empty(self):
        """ Блокирует воспроизведение пока очередь не освободится"""
        self.queue.join()

    def stop(self):
        """ Останавливает весь процесс"""
        self.wait_until_empty()
        self.queue.put(None)
        self.finished.wait()

        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None