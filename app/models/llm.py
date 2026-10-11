import json
import os
import time
from collections.abc import Iterator

import httpx


def _ns_to_ms(value) -> float | None:
    """ Ollama отдаёт длительности в наносекундах - переводим в мс для логов/метрик """
    return value / 1e6 if value is not None else None


class OllamaClient:
    """ Клиент работы с локальной LLM"""
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3:14b", # legacy client default; foundation uses Settings.llm_model
        num_predict: int = 120, # максимум токенов в одном ответе
        temperature: float = 0.8, # больше = более "живая", разнообразная речь
        # Оценка "на глаз" (2026-09-02): системный промпт + история одного
        # звонка (реплики по 1 предложению, до ~15 минут) не должны
        # подобраться к этому пределу. Точную цифру даст prompt_eval_count
        # из реального прогона (см. stats= в generate()) - тогда можно будет
        # выставить значение осознанно, а не с большим запасом "на всякий".
        # Пока важно, чтобы значение вообще было задано явно: без этого при
        # приближении к дефолтному пределу Ollama начинает "сдвигать"
        # контекст (вытеснять самое начало - т.е. системный промпт), из-за
        # чего кэш промпта слетает и каждый ход снова обрабатывается с нуля.
        num_ctx: int = 4096,
    ):
        self.base_url = (
            base_url or os.getenv(
            "OLLAMA_BASE_URL",
            "http://127.0.0.1:11434",
        )
        )
        self.model = model
        self.num_predict = num_predict
        self.temperature = temperature
        self.num_ctx = num_ctx
        # Reuse one HTTP connection pool across turns in the legacy adapter.
        self._client = httpx.Client(timeout=None)

    def close(self) -> None:
        self._client.close()

    def generate(
        self,
        messages: list[dict[str, str]],
        stats: dict | None = None,
    ) -> Iterator[tuple[str, float | None]]:
        """
        Основной метод генерации + расчет метрики time to first token.

        Если передан stats (обычный dict) - на последнем чанке ответа
        (done=True) в него, помимо прочего, кладутся тайминги самой Ollama
        (prompt_eval_count/duration, eval_count/duration, load_duration).
        Именно prompt_eval_* показывает, действительно ли Ollama
        переиспользует KV-кэш общего префикса (системный промпт + начало
        истории) между ходами одного разговора, или пересчитывает промпт
        заново каждый раз - без этих цифр это нельзя было бы измерить.
        """

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": self.temperature,
                "top_p": 0.9,
                # Штраф за повтор одних и тех же слов/фраз - без него модель
                # маленького размера (3B) иногда зацикливается на одной фразе.
                "repeat_penalty": 1.15,
                "num_predict": self.num_predict,
                "num_ctx": self.num_ctx,
            },
        }

        start = time.perf_counter()
        first_token = True

        with self._client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json=payload,
            ) as response: # Позволяет загружать контент построчно, чтобы не загружать память

                response.raise_for_status()

                for line in response.iter_lines():
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("done"):
                        if stats is not None:
                            stats["llm_prompt_eval_count"] = data.get("prompt_eval_count")
                            stats["llm_prompt_eval_ms"] = _ns_to_ms(data.get("prompt_eval_duration"))
                            stats["llm_eval_count"] = data.get("eval_count")
                            stats["llm_eval_ms"] = _ns_to_ms(data.get("eval_duration"))
                            stats["llm_load_ms"] = _ns_to_ms(data.get("load_duration"))
                        break

                    token = data.get("message", {}).get(
                        "content",
                        "",
                    )

                    if not token:
                        continue

                    if first_token:
                        ttft = time.perf_counter() - start
                        first_token = False
                        yield token, ttft
                    else:
                        yield token, None
