import asyncio
import json

from app.foundation.api import build_application


async def main() -> int:
    application,_,_,_=build_application()
    readiness=await application.runtime_manager.readiness()
    if not readiness.get("prerequisites_ready", readiness.get("ready")):
        print(json.dumps({"warmup":False,"readiness":readiness},ensure_ascii=False,indent=2))
        return 2
    result=await application.runtime_manager.warmup()
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get("ok") else 3


if __name__=="__main__": raise SystemExit(asyncio.run(main()))
