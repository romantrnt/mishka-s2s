"""
Живой разговор с Мишкой через микрофон - но, в отличие от
microphone_client.py, не через локальные модели в этом же процессе, а
через настоящий сервер (тот же Docker-контейнер, тот же протокол
WebSocket, что и в test_audio.py). VAD/STT/LLM/TTS полностью на сервере
(см. app/api/websocket.py) - клиент только передаёт сырое аудио туда и
проигрывает то, что придёт обратно.

Использование:
    python3 client/microphone_websocket_client.py [ws://host:port]
"""

import asyncio
import base64
import json
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import websockets

from app.audio.player import AudioPlayer

SAMPLE_RATE = 16_000
CHUNK_MS = 32
CHUNK_SIZE = SAMPLE_RATE * CHUNK_MS // 1000


async def main():

    base_url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8000"
    session_id = f"mic_{int(time.time())}"
    url = f"{base_url.rstrip('/')}/ws/{session_id}"

    loop = asyncio.get_running_loop()
    mic_queue: asyncio.Queue = asyncio.Queue()

    # Плеер создаём только при первом ответе сервера: частота дискретизации
    # TTS (24кГц у XTTS-v2) приходит в самом сообщении ("sample_rate"),
    # а не известна клиенту заранее.
    player: AudioPlayer | None = None

    # Ноутбучные колонки + микрофон без аппаратного эхоподавления - без
    # этого флага Мишка слышит собственный голос из динамиков и отвечает
    # сам себе (воспроизведено вживую 07.09 - см. известную проблему в
    # PROGRESS.md). Пока идёт воспроизведение ответа - чанки с микрофона
    # просто не отправляются на сервер, плюс небольшой хвост после
    # окончания воспроизведения на затухание эха в комнате.
    is_speaking = threading.Event()
    ECHO_TAIL_S = 0.4

    def audio_callback(indata, frames, time_info, status):
        if status:
            print(status, file=sys.stderr)

        chunk = indata[:, 0].copy()

        # Колбэк sounddevice выполняется в своём собственном потоке, а не
        # в event loop'e asyncio - кладём чанк в очередь потокобезопасно.
        loop.call_soon_threadsafe(mic_queue.put_nowait, chunk)

    async def send_microphone_audio(ws) -> None:
        """ Отправляет на сервер всё, что накопилось в очереди с микрофона """
        while True:
            chunk = await mic_queue.get()
            if is_speaking.is_set():
                # Мишка сейчас говорит - не шлём собственное эхо на сервер
                # (см. is_speaking выше).
                continue
            await ws.send(json.dumps({
                "type": "audio",
                "audio": base64.b64encode(chunk.tobytes()).decode(),
            }))

    async def unmute_after_playback() -> None:
        """ Снимает is_speaking, когда плеер реально доиграл всё + хвост на эхо """
        if player is not None:
            await loop.run_in_executor(None, player.wait_until_empty)
        await asyncio.sleep(ECHO_TAIL_S)
        is_speaking.clear()

    async def receive_and_play(ws) -> None:
        """ Слушает ответы сервера: печатает текст и проигрывает синтезированную речь """
        nonlocal player

        async for message in ws:
            data = json.loads(message)
            msg_type = data["type"]

            if msg_type == "transcript":
                print(f"[Вы] {data['text']}")

            elif msg_type == "audio":
                is_speaking.set()

                if player is None:
                    player = AudioPlayer(
                        sample_rate=data["sample_rate"],
                        blocksize=1024,
                    )
                    player.start()

                audio = np.frombuffer(
                    base64.b64decode(data["audio"]),
                    dtype=np.float32,
                )
                print(f"[Мишка] {data['text']!r}")
                player.put(audio)

            elif msg_type == "done":
                asyncio.create_task(unmute_after_playback())
                print("[готово, слушаю дальше]")

    print(f"Подключаюсь к {url}...")

    async with websockets.connect(url) as ws:

        print("Подключено. Говорите - Мишка слушает (Ctrl+C для выхода).")

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=CHUNK_SIZE,
            callback=audio_callback,
        ):
            await asyncio.gather(
                send_microphone_audio(ws),
                receive_and_play(ws),
            )

    if player is not None:
        player.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
        print("Остановлено.")
