import asyncio
import json
import websockets


async def main():

    url = "ws://127.0.0.1:8000/ws/test_session"

    async with websockets.connect(url) as ws:

        print("connected")

        await ws.send(
            json.dumps(
                {
                    "type": "ping"
                }
            )
        )

        response = await ws.recv()

        print(response)


asyncio.run(main())