from app.safety.fallback import (
    CONFUSED_FALLBACK_PHRASES,
    ESCALATION_PHRASES,
    FallbackPhrases,
    REDIRECT_PHRASES,
    SILENCE_FALLBACK_PHRASES,
    UNCLEAR_ESCALATION_PHRASES,
)
from app.safety.incidents import IncidentLogger
from app.safety.input_filter import InputSafetyFilter
from app.safety.output_filter import OutputSafetyFilter
from app.safety.severity import SafetyCheckResult


class SafetyGuard:
    """ Единая точка входа для всех проверок безопасности: вход, выход, редиректы, лог инцидентов """

    # Столько НАМЕРЕННЫХ блоков (см. SafetyCheckResult.escalates) за один
    # звонок допускаем, прежде чем сменить тон ответа на приглашение
    # позвать взрослого.
    ESCALATION_THRESHOLD = 3

    # Столько НЕРАЗБОРЧИВЫХ фраз ПОДРЯД (см. register_unclear) допускаем,
    # прежде чем предположить, что дело не в ребёнке, а в микрофоне/шуме, и
    # попросить позвать взрослого - вместо того чтобы бесконечно повторять
    # "не расслышал".
    UNCLEAR_ESCALATION_THRESHOLD = 3

    def __init__(self):
        self.input_filter = InputSafetyFilter()
        self.output_filter = OutputSafetyFilter()
        self.redirect_phrases = FallbackPhrases(REDIRECT_PHRASES)
        self.escalation_phrases = FallbackPhrases(ESCALATION_PHRASES)
        self.silence_phrases = FallbackPhrases(SILENCE_FALLBACK_PHRASES)
        self.unclear_escalation_phrases = FallbackPhrases(UNCLEAR_ESCALATION_PHRASES)
        self.confused_phrases = FallbackPhrases(CONFUSED_FALLBACK_PHRASES)
        self.incidents = IncidentLogger()
        self._block_counts: dict[str, int] = {}
        self._unclear_counts: dict[str, int] = {}

    def check_input(self, text: str) -> SafetyCheckResult:
        return self.input_filter.check(text)

    def check_output_chunk(self, text: str) -> SafetyCheckResult:
        return self.output_filter.check(text)

    def register_block(self, session_id: str) -> int:
        """ Считает намеренные блоки за звонок, возвращает счётчик после инкремента """
        count = self._block_counts.get(session_id, 0) + 1
        self._block_counts[session_id] = count
        return count

    def reset_session(self, session_id: str) -> None:
        """ Сбрасывает счётчики - вызывается при начале/окончании звонка """
        self._block_counts.pop(session_id, None)
        self._unclear_counts.pop(session_id, None)

    def pick_redirect(self, session_id: str | None = None) -> str:
        if session_id is not None and self._block_counts.get(session_id, 0) >= self.ESCALATION_THRESHOLD:
            return self.escalation_phrases.pick()
        return self.redirect_phrases.pick()

    def register_unclear(self, session_id: str) -> int:
        """ Считает НЕРАЗБОРЧИВЫЕ фразы ПОДРЯД, возвращает счётчик после инкремента """
        count = self._unclear_counts.get(session_id, 0) + 1
        self._unclear_counts[session_id] = count
        return count

    def reset_unclear(self, session_id: str) -> None:
        """ Сбрасывает счётчик, как только фраза распознана нормально - счётчик
        именно про ПОДРЯД идущие случаи, а не про сумму за весь звонок """
        self._unclear_counts[session_id] = 0

    def pick_silence_fallback(self, session_id: str | None = None) -> str:
        if session_id is not None and self._unclear_counts.get(session_id, 0) >= self.UNCLEAR_ESCALATION_THRESHOLD:
            return self.unclear_escalation_phrases.pick()
        return self.silence_phrases.pick()

    def pick_confused_fallback(self) -> str:
        """ Реплика LLM оказалась бессвязной (не о ребёнке - о собственной генерации) """
        return self.confused_phrases.pick()
