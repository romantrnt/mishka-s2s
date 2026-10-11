import time

import numpy as np
import sounddevice as sd
import torch

from app.audio.buffer import AudioBuffer
from app.audio.player import AudioPlayer
from app.audio.vad import (
    VADConfig,
    VoiceActivityDetector,
)
from app.dialogue.manager import DialogueManager
from app.dialogue.prompt import SYSTEM_PROMPT
from app.dialogue.stream import StreamingDialogue
from app.models.llm import OllamaClient
from app.models.stt import SpeechToText
from app.models.stt_worker import STTWorker
from app.models.tts import MishkaVoiceTTS

from client.dialogue_worker import DialogueWorker


SAMPLE_RATE = 16_000
CHUNK_MS = 32
CHUNK_SIZE = SAMPLE_RATE * CHUNK_MS // 1000
vad = VoiceActivityDetector(
    VADConfig(
        sample_rate=SAMPLE_RATE,
        threshold=0.5,
        min_silence_ms=400,
        speech_pad_ms=30,
    )
)

buffer = AudioBuffer()
stt = SpeechToText(
    model_name="large-v3-turbo",
    device="cuda",
    compute_type="float16",
)
llm = OllamaClient(
    model="qwen3:14b",
)
tts = MishkaVoiceTTS(
    reference_audio="assets/reference/mishka_24k_mono.wav",
    device="cuda",
)
print("Warming up TTS...")
tts.synthesize(
    "Привет."
)
print("TTS warm.")

# Частота воспроизведения должна совпадать с частотой, которую отдаёт TTS,
# иначе голос будет звучать быстрее/медленнее и с неправильным тоном.
player = AudioPlayer(
    sample_rate=tts.sample_rate,
    blocksize=1024,
)
dialogue = DialogueManager(
    system_prompt=SYSTEM_PROMPT,
)


pipeline = StreamingDialogue(
    llm=llm,
    tts=tts,
)


dialogue_worker = DialogueWorker(
    pipeline=pipeline,
    dialogue=dialogue,
    player=player,
)

def on_transcription(text: str):
    if not text.strip():
        return

    dialogue_worker.submit(text)

stt_worker = STTWorker(
    stt,
    on_result=on_transcription,
)

def audio_callback(
    indata: np.ndarray,
    frames: int,
    time_info,
    status,
):

    if status:
        print(status)

    chunk = indata[:, 0].copy()

    audio = torch.from_numpy(chunk)

    event = vad.process(audio)

    timestamp = time.perf_counter()

    if event and "start" in event:
        buffer.clear()
        buffer.add(chunk)

    elif event and "end" in event:

        buffer.add(chunk)
        audio = buffer.get()
        stt_worker.submit(audio)

        buffer.clear()
    elif vad.vad.triggered:

        buffer.add(chunk)
def main():
    stt_worker.start()
    dialogue_worker.start()

    player.start()
    print("Listening...")
    print("Press Ctrl+C to stop.")


    try:

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=CHUNK_SIZE,
            callback=audio_callback,
        ):

            while True:
                time.sleep(1)

    except KeyboardInterrupt:

        print()
        print("Stopping...")

    finally:
        print("Stopping audio...")
        player.stop()
        print("Stopping dialogue worker...")
        dialogue_worker.stop()
        print("Stopping STT worker...")
        stt_worker.stop()
        print("Stopped.")

if __name__ == "__main__":
    main()
