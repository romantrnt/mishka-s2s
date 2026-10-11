"""
Простой тестовый клиент: отправляет .wav файл на сервер частями через API,
имитируя поток аудио от телефона (VAD/STT теперь полностью на сервере).

Использование:
    python3 client/test_audio.py path/to/audio.wav
"""

import asyncio
import base64
import json
import sys

import numpy as np
import soundfile as sf
import websockets

SAMPLE_RATE = 16_000
CHUNK_MS = 32
CHUNK_SIZE = SAMPLE_RATE * CHUNK_MS // 1000


def load_as_16k_mono(path: str) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=False)

    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    if sr != SAMPLE_RATE:
        # Простой линейный ресемплинг, чтобы не тащить лишние зависимости
        duration = len(audio) / sr
        target_len = int(duration * SAMPLE_RATE)
        x_old = np.linspace(0, duration, num=len(audio))
        x_new = np.linspace(0, duration, num=target_len)
        audio = np.interp(x_new, x_old, audio).astype("float32")

    return audio.astype("float32")


async def main():

    if len(sys.argv) < 2:
        print("Usage: python3 client/test_audio.py path/to/audio.wav")
        return

    audio = load_as_16k_mono(sys.argv[1])

    url = "ws://127.0.0.1:8000/ws/test_audio_session"

    async with websockets.connect(url) as ws:

        print("connected, streaming audio...")

        for start in range(0, len(audio), CHUNK_SIZE):

            chunk = audio[start:start + CHUNK_SIZE]

            if len(chunk) < CHUNK_SIZE:
                chunk = np.pad(chunk, (0, CHUNK_SIZE - len(chunk)))

            await ws.send(json.dumps({
                "type": "audio",
                "audio": base64.b64encode(chunk.tobytes()).decode(),
            }))

            await asyncio.sleep(CHUNK_MS / 1000)

        # Немного тишины в конце, чтобы VAD зафиксировал окончание речи
        silence = np.zeros(CHUNK_SIZE, dtype="float32")
        for _ in range(20):
            await ws.send(json.dumps({
                "type": "audio",
                "audio": base64.b64encode(silence.tobytes()).decode(),
            }))
            await asyncio.sleep(CHUNK_MS / 1000)

        print("waiting for response...")

        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            print(data["type"], data.get("text", ""))

            if data["type"] == "done":
                break

if __name__ == "__main__":
    asyncio.run(main())
