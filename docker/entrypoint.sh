#!/bin/sh
# Точка входа в контейнер mishka. Делает по порядку:
#   1) дожидается локального Ollama и проверяет целевую LLM;
#   2) опционально запускает полный readiness/warmup Silero→RVC;
#   3) запускает API. Legacy XTTS preload намеренно не вызывается.
set -e

echo "=========================================="
echo "Mishka AI - starting container"
echo "=========================================="

# Docker images use the same canonical launcher as RunPod.  This entrypoint is
# retained as a thin compatibility wrapper; it must not select app.main or a
# mock app based on an environment switch.
ROOT="${MISHKA_ROOT:-/workspace/mishka}"
CANONICAL="$ROOT/scripts/runpod_start.sh"
if [ ! -x "$CANONICAL" ]; then
    echo "Canonical launcher is missing: $CANONICAL" >&2
    exit 1
fi
exec "$CANONICAL"
