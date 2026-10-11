"""
Сценарный движок на LangGraph - маршрутизация двух служебных кнопок
("старт диалога" и "погода") в соответствующее действие.

СКОУП (см. также app/scenarios/buttons.py):
- Обрабатываются только эти две кнопки. Логика самих игровых сценариев
  в эту версию не входит.
- Погода - ЗАГЛУШКА: дата/день недели/время года считаются по-настоящему
  (это не требует внешнего API), а температура/погодные условия берутся
  фиксированной заглушкой с явной пометкой в коде (подключение реального
  API погоды - следующий шаг, не реализовано).

Живая проверка (12.09.2026): граф собирается и оба маршрута
(start_dialogue/weather) отрабатывают корректно.
"""

from datetime import datetime
from typing import Literal, TypedDict

from langgraph.graph import END, StateGraph


SEASON_BY_MONTH = {
    12: "зима", 1: "зима", 2: "зима",
    3: "весна", 4: "весна", 5: "весна",
    6: "лето", 7: "лето", 8: "лето",
    9: "осень", 10: "осень", 11: "осень",
}

WEEKDAY_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]


class ScenarioState(TypedDict):
    button_id: str
    reply_text: str | None


def start_dialogue_node(state: ScenarioState) -> ScenarioState:
    """
    Кнопка старта диалога. Само приветствие (случайная фраза из
    greetings.py) уже реализовано и живёт в app/mvp/api.py
    (handset_up) - этот узел описывает то же действие внутри графа, а не
    дублирует реализацию. Реальное подключение графа к websocket.py -
    следующий шаг.
    """
    return {**state, "reply_text": "__START_DIALOGUE__"}


def weather_node(state: ScenarioState) -> ScenarioState:
    """
    Заглушка погоды - см. docstring модуля. Число/день недели/время года
    настоящие, температура и описание погоды - фиксированная заглушка.
    """
    now = datetime.now()
    day_name = WEEKDAY_RU[now.weekday()]
    season = SEASON_BY_MONTH[now.month]

    # ЗАГЛУШКА - заменить на реальный вызов API погоды на следующем шаге.
    stub_temperature_c = 15
    stub_condition = "облачно"
    stub_advice = "возьми с собой дождевик на всякий случай"

    reply_text = (
        f"Сегодня {now.day} число, {day_name}, на дворе {season}. "
        f"На улице примерно {stub_temperature_c} градусов, {stub_condition}. "
        f"Мой совет: {stub_advice}."
    )

    return {**state, "reply_text": reply_text}


def route_button(state: ScenarioState) -> Literal["start_dialogue", "weather"]:
    if state["button_id"] == "weather":
        return "weather"
    return "start_dialogue"


def build_graph():
    """ Собирает и компилирует граф. Вызывать один раз при старте пайплайна,
    как и остальные модели/клиенты в app/core/pipeline.py """

    graph = StateGraph(ScenarioState)

    graph.add_node("start_dialogue", start_dialogue_node)
    graph.add_node("weather", weather_node)

    graph.set_conditional_entry_point(
        route_button,
        {
            "start_dialogue": "start_dialogue",
            "weather": "weather",
        },
    )

    graph.add_edge("start_dialogue", END)
    graph.add_edge("weather", END)

    return graph.compile()
