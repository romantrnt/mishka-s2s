import random

# Черновые формулировки - уточним с нейропсихологом/заказчиком, как и
# остальные фразы Мишки. Несколько вариантов нужны, чтобы приветствие не
# звучало каждый раз дословно одинаково при повторных звонках.
GREETING_PHRASES = [
    "Привет! Это Здоровяк Мишка. Как у тебя дела?",
    "Привет-привет! Мишка на связи. Что расскажешь?",
    "Здравствуй! Это Мишка. Как твоё настроение сегодня?",
]


class GreetingPhrases:
    """ Пул фраз приветствия: выбирает случайную, не повторяя последнюю подряд """

    def __init__(self, phrases: list[str]):
        self._phrases = phrases
        self._last: str | None = None

    def pick(self) -> str:
        choices = [p for p in self._phrases if p != self._last] or self._phrases
        phrase = random.choice(choices)
        self._last = phrase
        return phrase
