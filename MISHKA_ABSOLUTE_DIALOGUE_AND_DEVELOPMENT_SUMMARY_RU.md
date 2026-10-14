# Мишка — абсолютный саммари диалога и текущей разработки

Дата: 2026-10-13
Целевой проект: /workspace/mishka на Runpod
Тип документа: фактический рабочий отчёт, не заявление о завершённой production-приёмке.

## 1. Назначение и источники

Этот файл сводит в одно место всю существенную историю запроса, решения пользователя, выполненные изменения, проверки, состояние удалённого сервера и оставшиеся блокеры.

Главный контракт:

/home/khugardin/Documents/Codex/2026-10-12/home-khugardin-documents-codex-2026-10/outputs/MISHKA_ABSOLUTE_PRODUCT_SCENARIO_SPEC_RU.md

Порядок канонических фраз:

/home/khugardin/Documents/Codex/2026-10-12/home-khugardin-documents-codex-2026-10/outputs/MISHKA_PHRASES_PLAIN_RU.md

Главный приоритет: canonical product/scenario/runtime contract выше legacy, fixture, demo и старых runtime решений.

## 2. Что пользователь потребовал

- Реализовать спецификацию end-to-end, а не просто проанализировать её.
- Сначала сопоставить текущую реализацию с контрактом, найти gaps и конфликты, затем переделать систему.
- Не сохранять мнимый legacy и не плодить слои совместимости.
- Не удалять LangGraph как технологию и зависимость.
- Удалять только старые fixture-графы, маршруты и контент, которые заменены canonical production graph.
- Требовать строгую LangGraph authorization для сценариев и переходов.
- Работать по порядку из MISHKA_PHRASES_PLAIN_RU.md.
- Не скрывать отсутствующие зависимости: если чего-то не хватает, спрашивать.
- 106 дикторских фраз пока могут быть заглушками; записи будут добавлены позднее.
- Adult-handoff до отдельной настройки сервера полностью заменить заглушкой.
- Проверить спецификацию целиком, а не только отдельные happy paths.
- После изменений перезапустить сервер.

## 3. Хронология решений диалога

### 3.1. Спецификация вместо legacy

Было зафиксировано, что текущий код может содержать частично готовые компоненты и fixture-сценарии, но они не считаются каноническими автоматически. При конфликте поздние решения владельца и абсолютная спецификация имеют приоритет.

### 3.2. WAV и фразы

Сначала было замечено, что не видны 106 целевых WAV. Пользователь пояснил: дикторские записи будут готовы позже, поэтому требуется подготовить весь остальной контур и временно поставить заглушки.

Позднее пользователь уточнил, что архив с WAV находится в workspace/scen_dataset.zip и попросил сопоставлять записи по транскрипции, имени и metadata.csv. В canonical каталоге заведены все 106 идентификаторов PHRASE_001–PHRASE_106. Реальные записи использованы там, где они были, а PHRASE_078 и PHRASE_099 оставлены тихими WAV-заглушками.

### 3.3. Adult-handoff

Пользователь спросил, что означает отсутствие настроенного подтверждаемого adult-handoff. Было объяснено, что это внешний подтверждаемый контур передачи взрослому: доставка, подтверждение получения, timeout/retry, audit и наблюдаемость, а не просто переход UI в состояние помощи.

Пользователь постановил временно заменить весь контур заглушкой. В development реализован simulated outbox и simulated ACK. В production внешний handoff не притворяется рабочим: система fail-closed, а health/safe остаётся false до реальной настройки.

### 3.4. Legacy

Пользователь отдельно потребовал менять устаревший код, а не оставлять рядом старые куски. В результате старые fixture-графы, маршруты и контент удалены, application/runtime переподключены к одному canonical graph. Зависимость LangGraph сохранена.

### 3.5. Перезапуск

Проверка показала, что после последних изменений старый процесс ещё работал. Он был остановлен. Первый restart в production_candidate завершился ожидаемой ошибкой: MISHKA_LIGHTING_DEVICE отсутствует, поэтому physical hardware mode не может стартовать.

Файл .env не редактировался. Для продолжения headless-разработки сервер запущен с временными shell overrides:
- MISHKA_HARDWARE_MODE=fake
- MISHKA_SCENARIO_ENGINE=canonical
- MISHKA_LANGGRAPH_STRICT=1

Эти overrides не записаны в .env.

## 4. Что реализовано

### 4.1. Canonical LangGraph

Добавлены:
- app/scenarios/canonical_graph.py
- app/foundation/canonical_contract.py
- content/scenarios/canonical_product_v1.json

Scenario document содержит scenario_id canonical_product, version 1, contract canonical_product/v1, langgraph_authorization=true и 31 canonical state node.

Топология:
ingress → normalize_event → session_guard → safety_precheck → route_mode → state node → policy_validate → emit_effects → persist_snapshot → await_event

Authorization boundary:
session_guard → safety_precheck → policy_validate

Graph загружает и валидирует scenario document fail-closed, проверяет version, обязательные поля, state_version и допустимые transitions.

### 4.2. Application/runtime

app/foundation/application.py существенно переписан и очищен от старой route/fixture-механики.

Реализовано:
- canonical-only receive_async;
- strict LangGraph gate;
- effects с effect_id;
- persist snapshot и event contract;
- физический lifecycle IDLE, WAITING_RFID, ACTIVE, ENDED;
- inert behavior после soft_end/session_closed до нового старта;
- repeated RFID без сброса mode/RAM;
- unknown RFID как anonymous visual flow;
- malformed profile как anonymous;
- opaque profile_ref/child_id;
- удаление RFID и сырых идентификаторов из telemetry/snapshot/publisher.

### 4.3. Safety и handset

- Глобальная кнопка 5 имеет priority и не ломается из-за safety_handoff/session_closed.
- request_adult из любой mode переводит в safety_handoff.
- Safety fail-closed, fallback PHRASE_091.
- Handset-down обработан во всех режимах.
- Safety marker сохраняется.
- Игра после handset-down может продолжаться visual/button контуром.
- Поднятие трубки в game_active сохраняет игру и открывает voice channel.
- RfidRemoved в safety сохраняет safety mode.
- Audio play разрешён только при поднятой трубке.
- После закрытия stray handset/audio/button события игнорируются до нового старта.

### 4.4. Игры

- Выбор game catalog entry кнопкой.
- Profile game mastery bounded уровнем 0–9.
- Стартовый уровень учитывает максимум каталога и профиль.
- Timeout выдаёт game_retry hint/downgrade.
- Exit confirmation через substate game_exit_confirmation.
- Кнопка 7 подтверждает выход, 6 отменяет.
- Для retry/hint и game level добавлена lighting.

### 4.5. Fixed stories

Добавлен content/stories/fixed_manifest.json с пятью entries:
- fixed_aibolit_v1
- fixed_squirrel_v1
- fixed_lukomorye_v1
- fixed_soft_paws_star_v1
- fixed_neznayka_v1

Rights/hash проверяются fail-closed. Сейчас все пять production entries заблокированы. Для development добавлен только разрешённый placeholder:
content/audio/placeholders/fixed_story_unavailable.wav

Исправлены story menu, pause/resume, exit и checkpoint transitions.

### 4.6. Персональная сказка

Для кнопки 4:
- темы выбираются кнопками: динозавры, космос, лес, дружелюбные машины, море, назад;
- classify_intent распознаёт русские theme words;
- добавлен StoryBrief pipeline;
- валидированный development result превращается в placeholder effect;
- invalid generation выдаёт PHRASE_080 и PHRASE_081;
- nested pipeline outputs передаются в effects;
- запрещены protected names/IP и persona forbidden words;
- проверяются title/text schema, safety_tags, длина, возраст и длительность;
- boolean duration отклоняется;
- future DOB отклоняется;
- age band fallback: 3-5/profile_missing_default.

### 4.7. Bounded dialogue и LLM

Разрешённые topics:
greetings, clinic, weather, games, stories, feelings, questions

Запрещённые topics:
diagnosis, medicine, secret, personal_data

bounded_dialogue_turn:
- работает только при поднятой трубке;
- принимает transient text;
- использует bounded S2S;
- сохраняет только валидированные chunks или fallback;
- не может сам менять canonical state.

Тест mock на «Привет» дал bounded_dialogue_completed и chunk «Тестовая реплика.».

### 4.8. Adult-handoff placeholder

Development SimulatedAdultHandoff пишет структурированный outbox и simulated ACK. После ACK добавляется nested PHRASE_089 effect.

В production adult_handoff=None. Это намеренный fail-closed режим. Нельзя считать, что взрослый получил обращение, пока не появится внешний transport и подтверждение.

### 4.9. Schedule/weather/date

Typed schedule validator сохраняет room, floor, start_at, minutes_until, status и priority. Время проверяется как HH:MM.

TypedInfoRenderer показывает room/floor/minutes только при наличии. Schedule и weather независимы: outage weather не блокирует schedule. Anonymous schedule блокируется privacy policy. Date renderer использует русские названия месяцев.

### 4.10. Voice assets

Добавлены:
- content/audio/phrase_catalog_ru.txt
- content/audio/voice_manifest.json
- scripts/build_voice_manifest.py
- PHRASE_001.wav … PHRASE_106.wav
- placeholder для fixed story

Проверено:
- ровно 106 transcript lines;
- manifest совпадает с каталогом 1:1 по порядку;
- 104 записи реальные;
- PHRASE_078 и PHRASE_099 placeholders;
- реальные WAV mono PCM16, 22050 Hz;
- placeholders mono PCM16, 24000 Hz;
- runtime читает объявленный sample rate.

RecordedPhraseResolver:
- production допускает только approved;
- development допускает pending и placeholders;
- readiness остаётся incomplete до approval.

Manifest сейчас:
- recorded_count=106;
- placeholder_count=2;
- approved_count=0;
- status=incomplete_pending_recording_or_approval.

### 4.11. Stand, audio и browser

- HeadlessAudioOutput получил on_asset_play.
- Physical headless читает canonical WAV и декодирует mono PCM16.
- Relative asset paths разрешаются.
- Для fixed stories/placeholders отправляется AudioCompleted.
- UART simulator обогащает audio_play asset_path и content_kind.
- Добавлены /stand/audio/phrase/{phrase_id} и /stand/audio/completed.
- Browser получил playPhraseAsset и playStoryAsset.
- speechSynthesis fallback сообщает audio completed, а не имитирует button 7.
- story_wav принимает dev placeholder response IDs.
- Воспроизведение блокируется при опущенной трубке.

### 4.12. Lighting

Добавлен app/foundation/lighting.py и расширен LED contract.

Покрыты presets:
READY, ROOT_MENU для 1–7, LISTENING, HELP для 1/7, SAFE_STOP, SUCCESS, ERROR_RECOVERABLE с return button, THINKING, PAUSED для 6/7, RETRY/HINT.

### 4.13. Privacy и telemetry

RfidPresented publisher payload ограничен profile_resolved и unknown_tag. Физический RFID tag убран из SessionRecord, publisher и raw diagnostics. Raw frame заменяется на [rfid payload redacted].

### 4.14. Profiles

Удалён automatic DEFAULT_PROFILES seed из production SQLiteProfileProvider. Новые production profiles должны provision-иться care system. Текущая база содержит 2 профиля и 59 history records, но это уже существующие данные, а не новый автоматический seed.

### 4.15. Console/API

Console переведён на canonical terminology:
- старые fixture/dev labels удалены;
- старые story names удалены;
- AUTHENTICATED UI state удалён;
- stale ENDING conditions удалены;
- добавлены phrase/story audio actions;
- отображается canonical lifecycle.

## 5. Фактический inventory файлов

### Изменённые tracked files

- .env.example
- app/foundation/adapters.py
- app/foundation/api.py
- app/foundation/application.py
- app/foundation/config.py
- app/foundation/console/console.css
- app/foundation/console/console.js
- app/foundation/console/index.html
- app/foundation/domain.py
- app/foundation/e2e_probe.py
- app/foundation/events.py
- app/foundation/hardware.py
- app/foundation/led_contract.py
- app/foundation/runtime.py
- app/foundation/s2s.py
- app/foundation/schedule_weather.py
- app/foundation/stand.py
- app/foundation/tts_engine.py
- app/scenarios/buttons.py
- scripts/smoke_test.py
- tests/test_hardware_stand.py
- tests/test_silero_tts.py

### Новые canonical files

- app/foundation/canonical_contract.py
- app/foundation/canonical_services.py
- app/foundation/input_adapters.py
- app/foundation/lighting.py
- app/scenarios/canonical_graph.py
- content/scenarios/canonical_product_v1.json
- content/stories/fixed_manifest.json
- content/audio/phrase_catalog_ru.txt
- content/audio/voice_manifest.json
- scripts/build_voice_manifest.py
- scripts/verify_canonical_contract.py
- tests/test_canonical_graph.py
- tests/test_lighting.py

### Новые аудио assets

- content/audio/voice/PHRASE_001.wav … PHRASE_106.wav
- content/audio/placeholders/fixed_story_unavailable.wav

### Удалённые legacy files

- app/foundation/content.py
- app/scenarios/graph.py
- app/scenarios/langgraph_runtime.py
- app/scenarios/langgraph_tree.py
- content/games/games.json
- content/responses/phrases.json
- content/scenarios/dev_core.json
- content/scenarios/fixture_core.json
- content/scenarios/main_demo.json
- content/stories/stories.json
- tests/test_foundation.py
- tests/test_langgraph_runtime.py
- tests/test_p1_10_p1_11.py
- tests/test_p1_13_p1_14.py
- tests/test_s2s_full_acceptance.py
- tests/test_schedule_weather.py

LangGraph не удалён: requirements.txt сохраняет langgraph==1.2.2.

## 6. Выполненные проверки

### Canonical contract

Команда:
.venv/bin/python scripts/verify_canonical_contract.py

Результат:
canonical contract acceptance: OK

### Canonical smoke

Команда:
.venv/bin/python scripts/smoke_test.py

Результат:
canonical smoke: OK

### Targeted pytest

Команда:
.venv/bin/python -m pytest -q tests/test_canonical_graph.py tests/test_lighting.py tests/test_foundation_api.py

Результат:
18 passed in 2.24s

### Syntax

.venv/bin/python -m py_compile $(find app -name '*.py' -print)

Результат: успешно.

### Diff hygiene

git diff --check прошёл чисто.

### Voice order

106 transcript lines совпадают с manifest assets строго по порядку.

### Fixed stories

У всех пяти production rights result=false; dev placeholder проверен отдельно.

### Browser endpoints

Проверены /stand/audio/completed, /stand/audio/phrase/{phrase_id} и placeholder story endpoint.

### Полный исторический pytest

Последний полный запуск:
166 passed, 43 failed

Эти 43 failure относятся преимущественно к старым тестам, которые ждут удалённые fixture contracts/старые states, production default profile seed, прежний S2S policy, старый TTS normalization или старую weather module. Нельзя выдавать этот результат за полную acceptance. Каждый такой тест ещё нужно либо мигрировать на canonical contract, либо удалить, если он проверяет удалённое legacy-поведение.

## 7. Сервер после restart

Pod:
- vyl4le92ga5tgi
- SSH 194.14.47.19:19830
- project /workspace/mishka

Процесс:
- .venv/bin/python scripts/run_stand.py
- PID при последней фиксации: 3070630
- port 8000
- health/live=true
- warmup complete
- canonical LangGraph strict active
- hardware backend: pty_uart_simulator
- runtime profile: production_candidate

health/ready показывает:
- lifecycle READY/live/ready=true;
- top-level content ready=false;
- contract canonical_product/v1;
- state_nodes=31;
- required voice=106;
- approved voice=0;
- all five fixed stories=false;
- provider=langgraph;
- strict=true.

health/safe показывает:
- safe=false;
- adult handoff configured=false;
- simulated=false в production candidate;
- fail_closed=true.

.env не менялся. Обнаруженный ..env.swp — пользовательский swap-файл, его нельзя удалять или редактировать автоматически.

## 8. Оставшиеся блокеры

### Voice approval

PHRASE_078 и PHRASE_099 пока тихие placeholders. Кроме того, manifest ещё не approved. Для закрытия readiness нужны настоящие записи/QA/approval metadata и повторная проверка.

### Fixed story rights

Все пять fixed stories заблокированы до получения и проверки rights/hash. До этого production playback должен оставаться закрытым.

### Реальный adult-handoff

Нужны внешний transport, адресат, auth, delivery ACK, timeout/retry, audit и operator acceptance.

### Physical hardware

Сервер временно использует fake hardware mode. Для физической приёмки требуется MISHKA_LIGHTING_DEVICE и persistent physical configuration в .env, после чего нужен restart.

### Старые тесты

Нужно принять решение по всем 43 failure полного historical pytest: миграция на canonical tests или удаление тестов, проверяющих удалённый legacy.

### Definition of Done

Текущий статус корректно называть canonical development/production-candidate. Это не окончательная production-ready приёмка, пока не закрыты voice approval, rights, physical hardware, adult-handoff и полный acceptance suite.

## 9. Что не делалось

- Не переписывался буквально каждый файл без связи с контрактом.
- Не менялся независимый код только ради массового diff.
- LangGraph не удалялся.
- .env не редактировался автоматически.
- .env.swp не удалялся.
- Внешний adult-handoff не выдавался за уже работающий.
- Ложная production readiness не заявлялась.
- Commit не создавался.
- Destructive reset рабочего дерева не выполнялся.

Изменения были широкими там, где контракт действительно поменялся: application/runtime, graph authorization, content, voice, safety, privacy, hardware/stand, console, configuration и acceptance tests.

## 10. Git status и итоговая оценка

Рабочее дерево оставлено незакоммиченным для дальнейшей проверки. На момент фиксации было 22 изменённых tracked-файла, удалённые legacy files, новые canonical manifests/graph/services/lighting/adapters/scripts/tests и 106 WAV assets. Пользовательский ..env.swp оставлен нетронутым.

Итог:
- legacy fixture graph заменён;
- LangGraph сохранён и стал обязательной authorization boundary;
- canonical scenarios и transitions заведены;
- 106 phrase identifiers доступны runtime;
- две согласованные WAV-заглушки заведены;
- adult-handoff placeholder работает fail-closed;
- safety, privacy, handset, stories, games, schedule/weather, personal story, lighting и browser/stand wiring сведены в единую структуру;
- canonical contract, smoke и targeted tests проходят;
- сервер после изменений перезапущен.

Честная формулировка состояния:

canonical implementation complete for the current development scope; production acceptance blocked by voice approval/placeholders, fixed-story rights, real adult-handoff, physical hardware configuration, and migration/removal of stale historical tests.

Этот документ нужно обновить после появления настоящих PHRASE_078/PHRASE_099, approval voice manifest, настройки adult-handoff, выдачи fixed-story rights и зелёного полного acceptance suite.

