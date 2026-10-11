from dataclasses import dataclass, field


@dataclass
class DialogueManager:
    """ Класс для сохранения и управлением контекста внутри диалога"""
    system_prompt: str # Системный-промпт
    history: list[dict[str, str]] = field(default_factory=list) # Формат сохранения истории диалога

    def add_user_message(self, text: str) -> None:
        """ Добавление сообщения пользователя"""
        self.history.append(
            {
                "role": "user",
                "content": text,
            }
        )

    def add_assistant_message(self, text: str) -> None:
        """ Добавление сообщения ИИ"""
        self.history.append(
            {
                "role": "assistant",
                "content": text,
            }
        )

    def messages(self) -> list[dict[str, str]]:
        """ Извлечение истории диалога (полностью)"""
        return [
            {
                "role": "system",
                "content": self.system_prompt,
            },
            *self.history,
        ]

    def reset(self) -> None:
        """ Удаление истории"""
        self.history.clear()