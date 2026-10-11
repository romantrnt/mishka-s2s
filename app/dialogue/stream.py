import queue
import re
import threading
import time

from app.models.llm import OllamaClient
from app.models.tts import MishkaVoiceTTS
from app.safety import SafetyAction, SafetyGuard


class StreamingDialogue:

    # Жёсткий предел на число реплик-предложений за один ответ. Системный
    # промпт и так просит короткий ответ (одно предложение), но маленькая
    # модель (3B) иногда его игнорирует и вместо одной реплики генерирует
    # целый диалог сама с собой (вопрос + придуманный ответ ребёнка + ещё
    # вопрос). Без этого предела все "лишние" предложения честно улетали бы
    # в TTS и озвучивались. Уменьшено с 2 до 1 (02.09.2026, "быстрые
    # улучшения диалога") - более короткий, "телефонный" темп ответа.
    MAX_SENTENCES_PER_TURN = 1

    def __init__(
        self,
        llm: OllamaClient,
        tts: MishkaVoiceTTS,
        safety: SafetyGuard,
    ):
        self.llm = llm # Класс модели LLM
        self.tts = tts # Класс модели tts
        self.safety = safety # Входной/выходной фильтр, редиректы, лог инцидентов

    @staticmethod
    def sanitize_text(text: str) -> str:
        """
        Оставляем только русский текст, цифры, пробелы и базовую пунктуацию.
        Это дополнительный слой защиты перед TTS.
        """

        # Символы с реальным смыслом при озвучке - заменяем словом ДО
        # общей вырезки, иначе теряется смысл молча (например, "23°C"
        # превращалось в голое "23", актуально для будущего сценария с
        # погодой - см. PROGRESS.md, 11.09.2026).
        text = text.replace("°", " градусов ")
        text = text.replace("%", " процентов ")

        # Удаляем латиницу
        text = re.sub(
            r"[A-Za-z]+",
            "",
            text,
        )

        # Удаляем китайские / японские / корейские
        # и другие некириллические символы.
        text = re.sub(
            r"[^\u0400-\u04FF0-9\s.,!?;:—–()\"«»…\-]",
            "",
            text,
        )

        # Убираем лишние пробелы
        text = re.sub(
            r"\s+",
            " ",
            text,
        )

        # Убираем пробелы перед пунктуацией
        text = re.sub(
            r"\s+([,.!?;:])",
            r"\1",
            text,
        )

        text = text.strip()

        # После вырезки латиницы/символов в начале может остаться
        # "осиротевшая" пунктуация (например, "Wow, круто!" превращалось
        # в ", круто!") - убираем её, иначе TTS озвучивает мусорный
        # обрывок перед настоящим текстом.
        text = re.sub(r"^[,.!?;:\-—–\s]+", "", text)
        # Схлопываем повторяющуюся пунктуацию, оставшуюся после вырезки
        # (например, ": ." из "test@test.com").
        text = re.sub(r"[,.!?;:]{2,}", lambda m: m.group(0)[-1], text)

        return text.strip()

    @staticmethod
    def extract_chunk(buffer: str):
        """ Нарезаем текс по порциям, чтобы не отправлять текст по словам"""
        match = re.search(
            r"^(.+?[.!?…])(?:\s|$)",
            buffer,
        )

        if match:
            chunk = match.group(1).strip()
            remainder = buffer[match.end():]
            return chunk, remainder

        if len(buffer) >= 80: # Если llm вернула текст без знаком препинания, то режем его вручную

            cut = buffer.rfind(
                " ",
                40,
                80,
            )

            if cut != -1:
                return (
                    buffer[:cut].strip(),
                    buffer[cut:].lstrip(),
                )

        return None, buffer

    @staticmethod
    def _looks_like_gibberish(text: str) -> bool:
        """
        Небольшая (3B) модель иногда вместо связной реплики выдаёт
        бессвязный обрывок - не хардкод-артефакт с конкретным текстом (как
        KNOWN_HALLUCINATIONS у STT), а шум генерации, замеченный на
        непривычных именах собственных (живой пример 07.09.2026: "Га-га!"
        в ответ на "Гарри Поттер"). Нельзя перечислить все имена/названия,
        которые ребёнок может назвать - ловим по ФОРМЕ ответа, а не по
        конкретным словам: обычная реплика по промпту - короткое, но
        полноценное предложение (до 12-15 слов), а не голое междометие.
        """
        letters = re.sub(r"[^Ѐ-ӿ]", "", text)
        if len(letters) < 6:
            return True
        words = re.findall(r"[Ѐ-ӿ]+", text)
        return not any(len(w) >= 4 for w in words)

    @staticmethod
    def _trim_incomplete_tail(text: str) -> str:
        """
        Обрезаем незаконченное слово в самом хвосте буфера - если LLM
        оборвалась на полуслове из-за лимита num_predict, а не по знаку
        препинания, слышно будет обрубленное слово. Обрезаем до последнего
        целого слова, если это возможно.
        """
        if text and text[-1] not in ".!?…":
            cut = text.rfind(" ")
            if cut > 0:
                return text[:cut].strip()
        return text

    def generate(self, messages, session_id: str = "unknown"):
        """ Генерация сообщения"""

        # Две очереди
        text_queue = queue.Queue()
        event_queue = queue.Queue()

        full_text = []
        # llm_worker пишет сюда метрики LLM (TTFT + тайминги самой Ollama).
        # Обычный локальный var тут не сработал бы - llm_worker выполняется в
        # отдельном потоке, а читаем мы это значение уже после его завершения.
        metrics = {
            "llm_ttft": None,
            "llm_prompt_eval_count": None,
            "llm_prompt_eval_ms": None,
            "llm_eval_count": None,
            "llm_eval_ms": None,
            "llm_load_ms": None,
        }

        def llm_worker():
            """
            Поток генерации текста
            Итерируется по потоку токенов из self.llm.generate(messages)
            Накапливает токены в buffer и в цикле while True пытается вырезать готовые предложения через extract_chunk
            Помещает очищенные предложения в text_queue.В конце (когда LLM закончила генерировать) отправляет остатки буфера и маркер завершения None
            """
            sentences_sent = 0
            stopped_early = False

            try:

                buffer = ""

                for token, ttft in self.llm.generate(
                    messages,
                    stats=metrics,
                ):
                    if ttft is not None:
                        metrics["llm_ttft"] = ttft

                    if stopped_early:
                        # Фильтр уже сработал и заменил ответ на редирект -
                        # дальше не вычитываем: не продолжаем генерацию
                        # потенциально небезопасного текста, даже если он не
                        # озвучивается.
                        break

                    if sentences_sent >= self.MAX_SENTENCES_PER_TURN:
                        # Сверх лимита реплик больше не озвучиваем, но
                        # ДОЧИТЫВАЕМ поток до конца, а не обрываем соединение
                        # раньше времени - иначе Ollama не успевает прислать
                        # финальный чанк (done=True) со статистикой
                        # (prompt_eval_count и т.п.), без которой нельзя
                        # проверить, переиспользуется ли кэш промпта между
                        # ходами (см. stats= в OllamaClient.generate). LLM и
                        # TTS работают в разных потоках параллельно - это не
                        # должно ощутимо увеличивать задержку ответа, TTS и
                        # так синтезирует дольше, чем модель дочитывает
                        # оставшиеся ~40 токенов.
                        continue

                    print("[LLM TOKEN]", token, flush=True)
                    full_text.append(token)
                    buffer += token

                    while sentences_sent < self.MAX_SENTENCES_PER_TURN:

                        chunk, remainder = (
                            self.extract_chunk(buffer)
                        )
                        if chunk is None:
                            break

                        buffer = remainder
                        chunk = self.sanitize_text(
                            chunk
                        )

                        if not chunk:
                            continue

                        if self._looks_like_gibberish(chunk):
                            # Модель "споткнулась" на генерации (см.
                            # _looks_like_gibberish) - подменяем на тёплую
                            # фразу-заминку и дальше эту реплику не
                            # генерируем, как и при BLOCK ниже.
                            chunk = self.safety.pick_confused_fallback()
                            full_text.clear()
                            full_text.append(chunk)
                            text_queue.put(chunk)
                            sentences_sent += 1
                            stopped_early = True
                            break

                        result = self.safety.check_output_chunk(chunk)

                        if result.action == SafetyAction.BLOCK:
                            # Реплика нарушает выходной фильтр (утечка
                            # личности бота, мед. совет, зацикливание и
                            # т.п.) - заменяем на тёплый редирект и дальше
                            # эту реплику не генерируем (модель, судя по
                            # всему, "съехала" с темы).
                            self.safety.incidents.log(
                                session_id, "output", "block",
                                result.category, chunk,
                            )
                            if result.escalates:
                                self.safety.register_block(session_id)
                            chunk = self.safety.pick_redirect(session_id)
                            full_text.clear()
                            full_text.append(chunk)
                            text_queue.put(chunk)
                            sentences_sent += 1
                            stopped_early = True
                            break

                        text_queue.put(chunk)
                        sentences_sent += 1

                if (
                    not stopped_early
                    and sentences_sent < self.MAX_SENTENCES_PER_TURN
                    and buffer.strip()
                ):
                    tail = self._trim_incomplete_tail(buffer.strip())
                    chunk = self.sanitize_text(tail)

                    if chunk:

                        if self._looks_like_gibberish(chunk):
                            chunk = self.safety.pick_confused_fallback()
                            full_text.clear()
                            full_text.append(chunk)
                        else:
                            result = self.safety.check_output_chunk(chunk)

                            if result.action == SafetyAction.BLOCK:
                                self.safety.incidents.log(
                                    session_id, "output", "block",
                                    result.category, chunk,
                                )
                                if result.escalates:
                                    self.safety.register_block(session_id)
                                chunk = self.safety.pick_redirect(session_id)
                                full_text.clear()
                                full_text.append(chunk)

                        text_queue.put(chunk)
                        sentences_sent += 1

            except Exception as exc:

                print(
                    f"[LLM ERROR] "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

            finally:

                if sentences_sent == 0:
                    # Ни одной реплики не ушло в TTS за весь ход (пустой
                    # ответ после фильтрации, сбой Ollama и т.п.) - лучше
                    # заготовленная фраза, чем полная тишина в трубке.
                    fallback = self.safety.pick_silence_fallback()
                    self.safety.incidents.log(
                        session_id, "output", "fallback",
                        "empty_turn", "",
                    )
                    full_text.clear()
                    full_text.append(fallback)
                    text_queue.put(fallback)

                text_queue.put(None)

        def tts_worker():
            """
            Поток синтеза речи
            Извлекает текстовые предложения из text_queue
            Если видит маркер None, завершает работу
            Вызывает self.tts.synthesize(text), получая аудио-массив, метрики задержки (latency) и скорость генерации (rtf)
            Упаковывает всё в словарь-событие и отправляет в финальную event_queue. В конце также шлет None
            """
            try:
                while True:
                    text = text_queue.get()
                    if text is None:
                        break
                    try:
                        # Стримим аудио кусками по мере готовности (см.
                        # MishkaVoiceTTS.synthesize_stream) вместо того,
                        # чтобы ждать всю фразу целиком - именно ожидание
                        # полной фразы было главным вкладом в задержку
                        # ответа (см. ARCHITECTURE.md, ~2-11с vs ~0.6с до
                        # первого куска, живой замер 07.09.2026). Текст и
                        # tts_latency (аналог TTFA) несёт только ПЕРВЫЙ
                        # кусок каждого предложения - как token/ttft у
                        # OllamaClient.generate().
                        start = time.perf_counter()
                        total_samples = 0
                        is_first = True

                        for audio, latency in self.tts.synthesize_stream(text):
                            total_samples += len(audio)
                            event_queue.put({
                                "type": "audio",
                                "text": text if is_first else "",
                                "audio": audio,
                                "tts_latency": latency if is_first else 0.0,
                            })
                            is_first = False

                        total_latency = time.perf_counter() - start
                        duration = total_samples / self.tts.sample_rate
                        rtf = total_latency / duration if duration > 0 else 0.0

                        print(
                            "[TTS GENERATED]",
                            text,
                            f"total={total_latency:.3f}s rtf={rtf:.3f}",
                            flush=True
                        )
                    finally:
                        text_queue.task_done()

            except Exception as exc:

                print(
                    f"[TTS ERROR] "
                    f"{type(exc).__name__}: {exc}",
                    flush=True,
                )

            finally:

                event_queue.put(None)

        llm_thread = threading.Thread(
            target=llm_worker,
            name="LLMWorker",
            daemon=True,
        )
        tts_thread = threading.Thread(
            target=tts_worker,
            name="TTSWorker",
            daemon=True,
        )
        llm_thread.start()
        tts_thread.start()

        while True:
            event = event_queue.get()

            if event is None:
                break
            yield event

        llm_thread.join()
        tts_thread.join()

        yield {
            "type": "done",
            "text": "".join(full_text),
            **metrics,
        }