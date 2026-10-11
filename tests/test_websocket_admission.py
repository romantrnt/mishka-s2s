import asyncio

from app.foundation.stand import _admission_reason, _reject_websocket_admission


class FakeWebSocket:
    def __init__(self):
        self.calls = []

    async def accept(self):
        self.calls.append(("accept",))

    async def send_json(self, payload):
        self.calls.append(("send_json", payload))

    async def close(self, code, reason):
        self.calls.append(("close", code, reason))


def run(coro):
    return asyncio.run(coro)


def test_admission_reason_mapping():
    assert _admission_reason({"state": "BOOTING"}) == "booting"
    assert _admission_reason({"state": "WARMING_UP"}) == "warming_up"
    assert _admission_reason({"state": "DEGRADED", "reason": "resource_capacity_insufficient"}) == "resource_capacity_insufficient"
    assert _admission_reason({"state": "DEGRADED", "reason": "provider_failure"}) == "degraded"


def test_degraded_admission_handshakes_sends_reason_and_closes_1013():
    ws = FakeWebSocket()
    run(_reject_websocket_admission(ws, code=1013, error_code="runtime_unavailable", reason="resource_capacity_insufficient", state="DEGRADED"))
    assert ws.calls[0] == ("accept",)
    assert ws.calls[1][0] == "send_json"
    assert ws.calls[1][1]["type"] == "admission_error"
    assert ws.calls[1][1]["error"]["reason"] == "resource_capacity_insufficient"
    assert ws.calls[2] == ("close", 1013, "resource_capacity_insufficient")


def test_auth_failure_has_separate_contract():
    ws = FakeWebSocket()
    run(_reject_websocket_admission(ws, code=1008, error_code="auth_failure", reason="invalid_token", state="AUTH_REQUIRED"))
    assert ws.calls[1][1]["error"]["code"] == "auth_failure"
    assert ws.calls[2] == ("close", 1008, "invalid_token")
