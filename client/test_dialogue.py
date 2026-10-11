import asyncio
import json
import websockets


async def main():

    url = "ws://127.0.0.1:8000/ws/test"

    async with websockets.connect(url) as ws:

        await ws.send(json.dumps({
            "type":"text",
            "text":"Привет Мишка"
        }))


        while True:

            msg = await ws.recv()

            data=json.loads(msg)

            print(data["type"])

            if data["type"]=="audio":
                print(
                    "TEXT:",
                    data["text"]
                )

            if data["type"]=="done":
                break

if __name__ == "__main__":
    asyncio.run(main())
