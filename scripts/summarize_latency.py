"""
Читает logs/latency.jsonl (по одной JSON-записи на реплику, пишет
app/metrics/latency.py) и печатает сводную таблицу задержек:
среднее и медиану по каждому этапу пайплайна.

Использование:
    python3 scripts/summarize_latency.py
    python3 scripts/summarize_latency.py path/to/other_latency.jsonl
"""

import json
import statistics
import sys

DEFAULT_PATH = "logs/latency.jsonl"

# Какие поля показываем в таблице и как их подписать.
FIELDS = [
    ("vad_ms", "VAD"),
    ("stt_ms", "STT"),
    ("llm_ttft_ms", "LLM (time to first token)"),
    ("llm_prompt_eval_ms", "LLM prompt eval (обработка промпта)"),
    ("llm_eval_ms", "LLM eval (генерация токенов ответа)"),
    ("tts_ttfa_ms", "TTS / Voice Cloning (time to first audio)"),
    ("total_ms", "Полная задержка (конец речи -> первый звук ответа)"),
]


def load_rows(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH

    rows = load_rows(path)

    if not rows:
        print(f"В файле {path} пока нет ни одной записи.")
        print("Сначала прогоните пару реплик через API (см. client/test_audio.py).")
        return

    print(f"Посчитано по {len(rows)} репликам из {path}\n")

    print(f"{'Этап':<45} {'среднее, мс':>12} {'медиана, мс':>12} {'учтено':>8}")
    print("-" * 80)

    for field, label in FIELDS:
        # Пропускаем реплики, где эта метрика не была измерена (None).
        values = [row[field] for row in rows if row.get(field) is not None]

        if not values:
            print(f"{label:<45} {'n/a':>12} {'n/a':>12} {0:>8}")
            continue

        mean = statistics.mean(values)
        median = statistics.median(values)

        print(f"{label:<45} {mean:>12.1f} {median:>12.1f} {len(values):>8}")


if __name__ == "__main__":
    main()
