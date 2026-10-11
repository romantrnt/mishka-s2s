import numpy as np

class AudioBuffer:
    """
    Класс, который частично решает проблемы потоковой записи аудио,
    сначала мы записываем все куски в один список и объединяет их только по команде.
    Таким образом оптимизируя нагрузку на память. Используется вместе с WebSockets
    """
    def __init__(self):
        self._chunks: list[np.ndarray] = []

    def add(self, chunk: np.ndarray) -> None:
        """ Добавляем новый фрагмент в список фрагментов, copy() нужен для безопасности данных внутри класса.
        Если внешний объект изменится, то данные внутри класса все равно будут прежними"""
        self._chunks.append(chunk.copy())

    def get(self) -> np.ndarray:
        """ Собирает все накопленные фрагменты в один монолитный массив"""
        if not self._chunks:
            return np.empty(0, dtype=np.float32) # Если данных нет, то возвращаем пустой массив float32
        return np.concatenate(self._chunks)

    def clear(self) -> None:
        """ Удаляет фрагменты из списка"""
        self._chunks.clear()

    def __len__(self) -> int:
        """ Суммарное количество аудио-сэмплов (фреймов) во всех накопленных массивах """
        return sum(len(chunk) for chunk in self._chunks)
