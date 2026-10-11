"""
Логика физических кнопок трубки и формат команд серверу->трубке.

ВАЖНО: это описание протокола/поведения, БЕЗ привязки к реальному
железу - у трубки свой микрокомпьютер, который должен уметь превращать
эти команды в реальное зажигание светодиодов. Это отдельная задача
команды, отвечающей за корпус и прошивку трубки. Здесь только: какая
кнопка что значит в каждой фазе звонка, и как выглядит JSON-команда,
которую сервер отправил бы трубке.

Логика самих игровых сценариев (контент, переключение между играми) в
эту версию не входит - описаны только две служебные кнопки: старт
диалога и погода.
"""

from dataclasses import dataclass
from enum import Enum


class ButtonMode(str, Enum):
    """ Режим подсветки панели кнопок - общий на всю панель, не на кнопку """
    NUMBERS = "numbers"
    COLORS = "colors"


class CallPhase(str, Enum):
    """
    Фаза звонка - от неё зависит назначение спец-кнопок (см.
    BUTTON_ID_START/BUTTON_ID_WEATHER ниже). Известно точно: обе
    спец-кнопки во время игры "забираются" под игровой ввод, а не
    остаются в своей обычной роли; сама игровая логика (что именно на
    них показывать во время игры) в эту версию не входит.
    """
    IDLE = "idle"          # трубка поднята, диалог ещё не начат
    DIALOGUE = "dialogue"  # идёт разговор с Мишкой (не игра)
    GAME = "game"          # идёт игра - логика вне этой версии, см. docstring выше


# Кнопка старта диалога - её физический номер подтверждён заказчиком.
BUTTON_ID_START = 7

# Номер кнопки погоды не был назван явно в обсуждении с заказчиком -
# placeholder, скорректировать, когда/если он появится.
BUTTON_ID_WEATHER = "weather"


@dataclass
class ButtonCommand:
    """ Одна команда на смену состояния одной кнопки """
    button_id: int | str
    mode: ButtonMode
    active: bool
    value: str | int | None = None  # цифра или цвет, если active=True

    def to_json(self) -> dict:
        return {
            "button_id": self.button_id,
            "mode": self.mode.value,
            "active": self.active,
            "value": self.value,
        }


def build_phase_command(phase: CallPhase) -> dict:
    """
    Собирает JSON-команду для сервер->трубка на смену фазы звонка.

    Правила:
    - IDLE: кнопка старта горит цифрой (готова к нажатию), погода тоже
      доступна.
    - DIALOGUE: кнопка старта выключена (нельзя перезапустить диалог
      поверх уже идущего), погода по-прежнему доступна в любой момент.
    - GAME: обе спец-кнопки "забираются" под игру - что именно на них
      показывать, определяется игровой логикой (вне этой версии, см.
      docstring модуля), поэтому здесь они просто помечаются неактивными
      как спец-функции, БЕЗ попытки прописать игровое поведение за них.
    """

    if phase == CallPhase.IDLE:
        # The idle/choice phase exposes the complete physical contract.
        # Buttons 1-5 are scenario actions, 6 starts Mishka/LLM dialogue,
        # and 7 requests the child's schedule plus weather.  Omitting 2-6
        # here made the UI legend disagree with the commands sent to hardware.
        buttons = [
            ButtonCommand(button_id, ButtonMode.NUMBERS, active=True, value=button_id)
            for button_id in range(1, 8)
        ]
    elif phase == CallPhase.DIALOGUE:
        buttons = [
            ButtonCommand(BUTTON_ID_START, ButtonMode.NUMBERS, active=False),
            ButtonCommand(BUTTON_ID_WEATHER, ButtonMode.NUMBERS, active=True, value=None),
        ]
    else:  # CallPhase.GAME - логика вне этой версии, см. docstring модуля
        buttons = [
            ButtonCommand(BUTTON_ID_START, ButtonMode.NUMBERS, active=False),
            ButtonCommand(BUTTON_ID_WEATHER, ButtonMode.NUMBERS, active=False),
        ]

    return {
        "type": "set_buttons",
        "phase": phase.value,
        "buttons": [b.to_json() for b in buttons],
    }
