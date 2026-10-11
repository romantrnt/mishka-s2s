from dataclasses import dataclass
from enum import Enum


class SafetyAction(Enum):
    """ Что делать с текстом после проверки """
    ALLOW = "allow"
    BLOCK = "block"        # опасный контент - перехватываем, заменяем на редирект
    LOG_ONLY = "log_only"  # сигнал бедствия - НЕ перехватываем, пропускаем в LLM как есть, только логируем


@dataclass
class SafetyCheckResult:
    action: SafetyAction
    category: str | None = None
    matched_text: str | None = None
    # True для категорий, которые похожи на намеренную попытку сломать/
    # раскрутить Мишку (джейлбрейк, опасный контент, мат), а не на сбой самой
    # модели (утечка личности, зацикливание и т.п.) - только такие блоки
    # считаются в SafetyGuard для эскалации при повторных попытках за звонок.
    escalates: bool = False
