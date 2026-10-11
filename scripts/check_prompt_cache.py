"""
Диагностика: переиспользует ли Ollama KV-кэш системного промпта между
репликами ОДНОГО звонка, или пересчитывает промпт заново на каждый ход.

Идея: system_prompt - константа, не меняется ни между ходами, ни между
звонками (см. app/dialogue/manager.py). Если кэш общего префикса реально
работает, prompt_eval_count (число токенов промпта, которые Ollama
обработала заново в этом ходу) должен резко упасть, начиная со 2-й реплики
разговора - остаётся пересчитать только новый хвост (последний ответ +
последний вопрос ребёнка), а не весь системный промпт заново.

Если prompt_eval_count держится примерно на одном уровне от хода к ходу -
кэш не работает, и дело в конфигурации (num_ctx/num_parallel/keep_alive),
не в длине самого текста промпта.

Использование:
    python3 scripts/check_prompt_cache.py
    python3 scripts/check_prompt_cache.py path/to/other_latency.jsonl

Важно: скрипт печатает ВСЕ строки файла по порядку и предполагает, что там
записан один тестовый звонок. Если в logs/latency.jsonl уже накопилось
несколько звонков - очистите/переименуйте файл перед прогоном, иначе строки
разных звонков перемешаются в одной таблице.
"""

import json
import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "logs" / "latency.jsonl"


def load_rows(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def fmt(value, suffix="") -> str:
    return f"{value:.0f}{suffix}" if isinstance(value, (int, float)) else "n/a"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH

    if not path.exists():
        print(f"Файл {path} пока не создан; prompt-cache ещё нечего проверять.")
        return

    rows = load_rows(path)

    if not rows:
        print(f"В файле {path} пока нет ни одной записи.")
        print("Сначала прогоните разговор через реальный WebSocket (несколько реплик подряд).")
        return

    header = (
        f"{'#':>3} {'prompt_eval_count':>18} {'prompt_eval_ms':>15} "
        f"{'eval_count':>11} {'eval_ms':>9} {'load_ms':>9} {'total_ms':>9}"
    )
    print(f"Прочитано {len(rows)} реплик из {path}\n")
    print(header)
    print("-" * len(header))

    for i, row in enumerate(rows, start=1):
        print(
            f"{i:>3} "
            f"{fmt(row.get('llm_prompt_eval_count')):>18} "
            f"{fmt(row.get('llm_prompt_eval_ms'), 'ms'):>15} "
            f"{fmt(row.get('llm_eval_count')):>11} "
            f"{fmt(row.get('llm_eval_ms'), 'ms'):>9} "
            f"{fmt(row.get('llm_load_ms'), 'ms'):>9} "
            f"{fmt(row.get('total_ms'), 'ms'):>9}"
        )

    counts = [
        row.get("llm_prompt_eval_count")
        for row in rows
        if row.get("llm_prompt_eval_count") is not None
    ]

    print()
    if len(counts) >= 2:
        first, rest = counts[0], counts[1:]
        if all(c <= first * 0.3 for c in rest):
            print(
                f"Похоже, кэш РАБОТАЕТ: prompt_eval_count упал с {first} "
                f"до {min(rest)}-{max(rest)} на последующих репликах."
            )
        else:
            print(
                f"Похоже, кэш НЕ переиспользуется: prompt_eval_count не падает "
                f"заметно после первой реплики ({first} -> {rest})."
            )
    else:
        print("Недостаточно реплик для вывода (нужно минимум 2 хода одного разговора).")


if __name__ == "__main__":
    main()
