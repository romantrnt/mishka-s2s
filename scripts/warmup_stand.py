import asyncio
import json
from app.foundation.stand import HardwareIntegrationStand

async def main():
    stand = HardwareIntegrationStand()
    result = await stand.application.runtime_manager.warmup()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
