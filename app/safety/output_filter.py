import re

from app.safety.input_filter import DANGEROUS_PATTERNS
from app.safety.severity import SafetyAction, SafetyCheckResult

# Черновые паттерны - формулировки уточним после ответа заказчика.
SELF_DISCLOSURE_PATTERN = re.compile(
    r"нейросет\w*|искусственн\w*\s+интеллект|языков\w*\s+модел\w*|промпт\w*|алгоритм\w*",
    re.IGNORECASE,
)
MEDICAL_ADVICE_PATTERN = re.compile(
    r"тебе\s+нужно\s+принять|прими\s+таблетк\w*|это\s+диагноз|дозировк\w*",
    re.IGNORECASE,
)
PERSONAL_DATA_REQUEST_PATTERN = re.compile(
    r"скажи\s+свой\s+адрес|назови\s+номер\s+телефона|пришли\s+фото",
    re.IGNORECASE,
)
DIALOGUE_LABEL_PATTERN = re.compile(
    r"^\s*(ребён[оа]к|мишка)\s*:",
    re.IGNORECASE,
)

# Черновой минимальный список - не претендует на полноту (обходится
# заменой букв на символы, пробелами между буквами и т.д.), но ловит
# прямые попытки заставить Мишку выругаться или повторить за ребёнком.
PROFANITY_PATTERN = re.compile(
    r"\bху[ийя]\w*|\bпизд\w*|\bеб[ао]\w*|\bёб\w*|\bбля\w*|\bсук[а-я]\b",
    re.IGNORECASE,
)


class OutputSafetyFilter:
    """
    Проверка одной реплики (предложения) Мишки перед отправкой в TTS.

    Это бэкстоп, а не только вторая линия: входной фильтр ловит только
    известные формулировки провокации, а этот фильтр проверяет то, что
    модель РЕАЛЬНО собирается сказать - независимо от того, как именно
    ребёнок к этому подвёл (формулировка, ролевая игра, "повтори за мной").
    """

    def check(self, text: str) -> SafetyCheckResult:

        if DIALOGUE_LABEL_PATTERN.match(text):
            return SafetyCheckResult(SafetyAction.BLOCK, category="dialogue_label", matched_text=text)

        # Те же категории опасного контента, что и на входе - на случай,
        # если провокация была сформулирована не так, как в блок-листе
        # входного фильтра, но модель всё равно сгенерировала опасный ответ.
        for category, pattern in DANGEROUS_PATTERNS.items():
            if pattern.search(text):
                return SafetyCheckResult(SafetyAction.BLOCK, category=category, matched_text=text, escalates=True)

        if PROFANITY_PATTERN.search(text):
            return SafetyCheckResult(SafetyAction.BLOCK, category="profanity", matched_text=text, escalates=True)

        if SELF_DISCLOSURE_PATTERN.search(text):
            return SafetyCheckResult(SafetyAction.BLOCK, category="self_disclosure", matched_text=text)

        if MEDICAL_ADVICE_PATTERN.search(text):
            return SafetyCheckResult(SafetyAction.BLOCK, category="medical_advice", matched_text=text)

        if PERSONAL_DATA_REQUEST_PATTERN.search(text):
            return SafetyCheckResult(SafetyAction.BLOCK, category="personal_data_request", matched_text=text)

        if self._has_repeated_words(text):
            return SafetyCheckResult(SafetyAction.BLOCK, category="repetition", matched_text=text)

        return SafetyCheckResult(SafetyAction.ALLOW)

    @staticmethod
    def _has_repeated_words(text: str) -> bool:
        """ Три и более одинаковых слова подряд - модель зациклилась """
        words = text.lower().split()
        streak = 1
        for prev, curr in zip(words, words[1:]):
            if prev == curr:
                streak += 1
                if streak >= 3:
                    return True
            else:
                streak = 1
        return False
