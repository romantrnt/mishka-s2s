# MISHKA + Max JSON: воспроизводимый synthetic-only запуск

Этот launcher запускает неизменённый Max и настоящий MISHKA Stand в одном процессе приложения. Max обслуживает свежую синтетическую SQLite-базу по HTTPS с проверкой локального CA. MISHKA получает готовую JSON-карту через `PhoneUbuntuClient` и существующий приватный `MaxJsonBoundary`.

Это проверка интеграционного пути, **не production-запуск**. Во всех сообщениях launcher `production_ready=false`. Никаких реальных карточек, пациентов, ридеров или голосовых моделей запуск не использует.

## Требования

- Ubuntu/Linux с POSIX PTY. Для Windows нужен Ubuntu в WSL; Windows-native launcher не поддерживается. Физическое оборудование и USB passthrough этим запуском не проверяются.
- Python 3.12 и уже установленные Python-зависимости MISHKA для mock-пути, включая FastAPI, httpx, NumPy и зависимости сценарного runtime. Используйте подготовленную `.venv`, если она есть. Launcher ничего не устанавливает и модели не скачивает.
- `openssl` в PATH для генерации одноразового локального CA и TLS-сертификата через исходный `phone_local_demo.initialize`.
- Полная неизменённая папка Max `src` с `phone_local_demo.py`, `phone_demo_database.py`, `phone_ubuntu_client.py` и их зависимостями. Её путь всегда задаётся явно через `--max-src`; launcher не ищет другой checkout и не загружает исходники.

Перед запуском проверьте доступность окружения, а не считайте эти требования выполненными автоматически:

```bash
python3 --version
openssl version
cd /path/to/mishka_json_integration
.venv/bin/python scripts/run_max_json_demo.py --help
```

Для отдельного лёгкого окружения без torch и моделей:

```bash
python3.12 -m venv .venv-json-demo
.venv-json-demo/bin/python -m pip install -r requirements-json-demo.txt
.venv-json-demo/bin/python scripts/run_max_json_demo.py --max-src /path/to/max_files/src --assume-control-ready
```

Файл requirements-json-demo.txt предназначен только для synthetic-интеграции. Обычный requirements.txt оставлен для общего приложения и содержит тяжёлые зависимости production-пути. Launcher сам ничего не устанавливает.

## Обычный запуск: честно закрытый admission

```bash
cd /path/to/mishka_json_integration
.venv/bin/python scripts/run_max_json_demo.py \
  --max-src /path/to/max_files/src
```

Профиль жёстко задан как `mock`, identity mode как `max_json`, hardware как `simulator` (можно явно выбрать `--hardware-mode fake`). `.env` launcher не загружает и production-профиль или physical-режим в CLI выбрать нельзя.

Mock warmup не подтверждает реальные STT/LLM/TTS/RVC и поэтому оставляет lifecycle в `DEGRADED`. Без специального тестового флага:

- `control_readiness="actual"`, `control_admits_users=false`;
- фактический in-process `/ready` возвращает 503;
- Max может обработать синтетический скан, но boundary не допускает его как готовый приватный контекст;
- `lift` возвращает `runtime_unavailable` и не начинает разговор;
- `production_ready=false`.

Это ожидаемый результат, а не скрытая ошибка, которую launcher обходит.

## Положительный JSON → handset путь: только явный TEST-ONLY

Чтобы продемонстрировать сценарное принятие карты и снятие трубки без настоящего model warmup:

```bash
.venv/bin/python scripts/run_max_json_demo.py \
  --max-src /path/to/max_files/src \
  --assume-control-ready
```

`--assume-control-ready` явно меняет только control-plane lifecycle admission внутри этого demo-процесса. Launcher печатает `control_readiness="TEST-ONLY assumed"` и сохраняет исходный `startup_lifecycle` с `DEGRADED`. Отчёты runtime/content readiness он не подменяет: фактический `/ready` остаётся 503, `software_runtime_usable=false`, `production_ready=false`.

Не используйте этот флаг для допуска пользователей, acceptance реальной модели, проверки аудио или продакшена.

## Команды

Вводите по одной команде в строке:

| Команда | Действие |
| --- | --- |
| `demo-1` | Активная синтетическая карточка Альфа |
| `demo-2` | Активная синтетическая карточка Бета с признаком wheelchair в исходном JSON |
| `unknown` | Неизвестная синтетическая метка; очищает контекст |
| `archive` | Неактивная синтетическая карточка (`demo-3` у Max); очищает контекст |
| `lift` | `Stand.inject("handset", {"state":"up"}, boundary.session_id)`, если admission разрешён |
| `hangup` | Тот же `Stand.inject` со `state="down"`; завершает персонализированную сессию и очищает контекст с обеих сторон |
| `status` | Ограниченный статус без имени, возраста, UID, credentials и полной JSON-карты |
| `help` | Список команд |
| `quit` | Корректно остановить все компоненты |

Например:

```text
demo-1
status
lift
hangup
demo-2
unknown
demo-1
archive
quit
```

С `--assume-control-ready` первый `demo-1` должен дать `mode="profile_ready"` и `context_present=true`, затем `lift` даёт `handset="up"` и `profile_status="identified"`. После `hangup`, `unknown` и `archive` контекст должен отсутствовать.

Карта приходит только через очередь synthetic capture runner самого Max. Launcher не отправляет UID или JSON в MISHKA, не имеет своего HTTPS resolver, не добавляет routes для JSON/authorization и не открывает HTTP-порт MISHKA. Имеющиеся ASGI routes используются внутри процесса только для read-only `/ready`; публично доступен лишь Max demo HTTPS на loopback со случайным свободным портом и локальным TLS.

## Автоматизированный smoke и shutdown

```bash
printf 'demo-1\nlift\nhangup\nquit\n' | \
  .venv/bin/python scripts/run_max_json_demo.py \
    --max-src /path/to/max_files/src --assume-control-ready

MAX_JSON_TEST_SRC=/path/to/max_files/src \
  .venv/bin/python -m pytest tests/test_max_json_launcher.py -q
```

Тесты проходят реальный Max TLS/SQLite → JSON → actual Stand/PTY путь, handset с тем же `session_id`, закрытый admission по умолчанию, unknown/archive, известный duplicate case и shutdown. Отдельные subprocess-проверки покрывают `--help` без сторонних пакетов, `quit`, EOF, SIGTERM и Ctrl+C, включая незавершённую строку stdin. Физический capture и загрузчики настоящих моделей в in-process тесте запрещены заглушками, которые падают при вызове.

При штатном завершении выводится `event="shutdown"` с `client_closed`, `stand_closed`, `capture_stopped`, `server_stopped`, `context_cleared`, равными true. Единственным владельцем `poll/patient/clear/close` после подключения остаётся `MaxJsonBoundary`. Сначала завершается lifespan Stand, затем останавливается HTTPS server Max. Свежая временная папка с синтетическими SQLite/TLS/credentials удаляется при выходе из контекста. `kill -9`, сбой ОС и выключение питания не дают выполнить cleanup; не используйте их как проверку штатного shutdown.

Launcher не печатает credentials, UID, медицинские поля или полную карточку. Не публикуйте содержимое его временной папки. Не подставляйте существующую базу: CLI вообще не принимает путь базы и каждый запуск создаёт новый demo-каталог.

## Известные границы поведения

- Быстрое повторное чтение той же карточки сохраняет текущий дефект Max: `scan_started` очищает контекст, затем duplicate suppression не выдаёт новый `patient_ready`. Launcher показывает `duplicate_suppressed_by_max` и не исправляет это обходным resolver или повторной выдачей карты.
- Новый скан во время персонализированного разговора очищает старую персонализацию; текущая консервативная политика MISHKA может закрыть сессию. Сначала используйте `hangup`, затем новую карту.
- Max сохраняет свой 120-секундный предел жизни карточки. MISHKA дополнительно применяет существующий ready-to-lift deadline и серверную дату истечения. Launcher не продлевает эти сроки.
- Синтетическая база создаётся заново при каждом запуске. Переиспользование данных, enrollment и физический scan не поддерживаются.

## Что ещё блокирует настоящий серверный запуск

1. Production сейчас не разрешён. Этот launcher не снимает ограничение и не является инструкцией развёртывания production-сервера.
2. Нужно проверить целевую Ubuntu/WSL-среду: Python/пакеты, доступность PTY, права и реальное подключение нужного оборудования. Наличие такого сервера, GPU или устройств здесь не предполагается.
3. Для настоящего Max нужны отдельно проверенные provisioning, credential, trusted CA, разрешённый endpoint и работающий ридер. Demo CA, ключи и SQLite нельзя переносить в production.
4. Нужны actual readiness и E2E warmup настоящих STT/LLM/TTS/RVC на целевом железе, подтверждённые лимиты памяти/задержки и корректный отказ при сбое. TEST-ONLY admission ничего из этого не проверяет.
5. Нужны настоящие согласованные и доступные media: approved записанные фразы, истории и физически проверенные аудиовход/аудиовыход/handset/свет. Mock/PTY и успешное получение JSON не подтверждают медиаготовность.

Не меняйте Max, frozen ветку или main ради этого smoke. Launcher работает с отдельной интеграционной копией и ничего не пушит.

## Миграция конфигурации JSON-only

- `MISHKA_IDENTITY_MODE` принимает только `guest_only` или `max_json`.
- `MISHKA_PROFILE_READY_TTL_S` заменяет прежний ready-to-lift TTL; по умолчанию 60 с. Это срок ожидания снятия трубки, не новый 120-секундный срок клиента.
- Удалены `MISHKA_IDENTITY_PROVIDER`, `MISHKA_RFID_LOOKUP_TIMEOUT_S`, `MISHKA_RFID_READY_TTL_S`, `MISHKA_RFID_DEBOUNCE_S`. Старые переменные вызывают явную ошибку, а не игнорируются.
- `MISHKA_HISTORY_DB` указывает только session/history SQLite. Прежний `MISHKA_PROFILE_DB` не поддерживается. Существующие sessions/history можно использовать только после обычной проверки/резервного копирования пользователем; приложение больше не читает таблицу profiles.
- Старые сохранённые sessions переводят WAITING_RFID в WAITING_PROFILE и отбрасывают rfid_tag при чтении. Это совместимость истории сессий, не перенос разрешений или профилей.
- `max_json + clinical` отклоняется. Клиническая персонализация, patient history linking и расписание этим стыком не подтверждены.
- `/mock/rfid`, `/stand/rfid`, UID-события UART/WS и fixture resolver удалены. Изменить оператора на synthetic через HTTP невозможно.
