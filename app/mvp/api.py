"""Deprecated mock-only HTTP adapter; never a production launcher.

Optional FastAPI adapter; the deterministic core remains dependency-free.
"""
from __future__ import annotations

from .service import MishkaMVPService


def create_app(service: MishkaMVPService | None = None):
    try:
        from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    except ImportError as exc:  # helpful error for users who only run core tests
        raise RuntimeError("Mock HTTP API needs requirements-mock.txt. Run `make install`.") from exc

    service = service or MishkaMVPService()
    app = FastAPI(title="Mishka deterministic MVP", version="1.0.0-mock")
    app.state.service = service

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "mock", "legacy_voice_pipeline": "optional"}

    @app.get("/ready")
    def ready():
        content_errors = service.runtime.content.data.errors
        return {"ready": True, "mode": "mock", "content_errors": content_errors}

    @app.post("/mock/rfid")
    def mock_rfid(payload: dict):
        return service.process(str(payload.get("session_id", "mock-default")), {"type": "rfid", "tag_id": payload.get("tag_id")})

    @app.post("/mock/handset")
    def mock_handset(payload: dict):
        return service.process(str(payload.get("session_id", "mock-default")), {"type": "handset", "state": payload.get("state", "up")})

    @app.post("/mock/button")
    def mock_button(payload: dict):
        return service.process(str(payload.get("session_id", "mock-default")), {"type": "button", "button_id": payload.get("button_id")})

    @app.post("/mock/text")
    def mock_text(payload: dict):
        return service.process(str(payload.get("session_id", "mock-default")), {"type": "text", "text": payload.get("text", "")})

    @app.get("/mock/session/{session_id}")
    def mock_session(session_id: str):
        snapshot = service.snapshot(session_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="session not found")
        return snapshot

    @app.post("/session/reset")
    def reset(payload: dict):
        return service.reset(str(payload.get("session_id", "mock-default")))

    @app.websocket("/ws/{session_id}")
    async def websocket_endpoint(websocket: WebSocket, session_id: str):
        await websocket.accept()
        try:
            while True:
                payload = await websocket.receive_json()
                await websocket.send_json(service.process(session_id, payload))
        except WebSocketDisconnect:
            service.process(session_id, {"type": "handset", "state": "down"})

    return app


try:
    app = create_app()
except RuntimeError:
    # Allows importing MVP core without installing FastAPI; uvicorn reports the helpful error on use.
    app = None
