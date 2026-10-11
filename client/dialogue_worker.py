import queue
import threading
import time

from app.audio.player import AudioPlayer
from app.dialogue.manager import DialogueManager
from app.dialogue.stream import StreamingDialogue


class DialogueWorker:

    def __init__(
        self,
        pipeline: StreamingDialogue,
        dialogue: DialogueManager,
        player: AudioPlayer,
    ):
        self.pipeline = pipeline
        self.dialogue = dialogue
        self.player = player

        self.queue = queue.Queue()
        self.thread = None

        self.running = False

    def start(self):

        self.running = True

        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
        )

        self.thread.start()

    def submit(self, text: str):

        if text.strip():
            self.queue.put(text)

    def _run(self):

        while self.running:

            text = self.queue.get()

            if text is None:
                break

            try:
                self.process(text)

            except Exception as e:

                print(
                    f"[DIALOGUE ERROR] "
                    f"{type(e).__name__}: {e}",
                    flush=True,
                )

            finally:
                self.queue.task_done()

    def process(self, text: str):
        """ Прогоняет одну реплику пользователя через пайплайн и проигрывает ответ """

        print(f"[USER] {text}", flush=True)

        self.dialogue.add_user_message(text)
        messages = self.dialogue.messages()

        response_start = time.perf_counter()
        first_audio_at = None

        for event in self.pipeline.generate(messages):

            if event["type"] == "audio":

                if first_audio_at is None:
                    first_audio_at = time.perf_counter() - response_start
                    print(f"[LATENCY] time to first audio: {first_audio_at:.3f}s", flush=True)

                print(f"[MISHKA] {event['text']!r}", flush=True)
                self.player.put(event["audio"])

            elif event["type"] == "done":

                response_text = self.pipeline.sanitize_text(event["text"])
                self.dialogue.add_assistant_message(response_text)

                total = time.perf_counter() - response_start
                print(f"[DIALOGUE] response={response_text!r} generation={total:.3f}s", flush=True)

    def stop(self):

        self.running = False

        self.queue.put(None)

        if self.thread is not None:
            self.thread.join()