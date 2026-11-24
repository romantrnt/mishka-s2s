"""Native POST forms must keep an Origin while external referrers stay hidden."""
import base64

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
import pytest

from app.foundation.remote_access import PrivateStandGateway

TOKEN = "test_form_policy_" + "f" * 40
ORIGIN = "https://phone.example.test"


@pytest.mark.parametrize("path,expected", [
    ("/console", "same-origin"), ("/console/operator", "same-origin"),
    ("/console/other", "no-referrer"), ("/console/operator.js", "no-referrer"),
])
def test_only_console_documents_preserve_origin_for_native_logout(path, expected):
    app = FastAPI()

    @app.get("/{path:path}")
    async def document(path: str):
        response = HTMLResponse('<form method="post" action="/stand/logout"><button>Выйти</button></form>',
                                headers={"Referrer-Policy": "no-referrer"})
        # Existing inner operator policy must not compete with the gateway.
        response.raw_headers.append((b"referrer-policy", b"strict-origin"))
        return response

    gateway = PrivateStandGateway(app, token=TOKEN, allowed_origins=[ORIGIN])
    basic = "Basic " + base64.b64encode(f"mishka:{TOKEN}".encode()).decode()
    with TestClient(gateway, base_url=ORIGIN, follow_redirects=False) as client:
        page = client.get(path, headers={"Authorization": basic})
        assert page.headers.get_list("referrer-policy") == [expected]
        assert client.post("/stand/logout", headers={"Origin": "null"}).status_code == 403
        assert client.post("/stand/logout", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/stand/logout", headers={"Origin": ORIGIN}).status_code == 303
        assert client.get("/stand/state").status_code == 401


def test_console_json_response_keeps_no_referrer_policy():
    app = FastAPI()

    @app.get("/console")
    async def response():
        return JSONResponse({"status": "not a document"})

    gateway = PrivateStandGateway(app, token=TOKEN, allowed_origins=[ORIGIN])
    basic = "Basic " + base64.b64encode(f"mishka:{TOKEN}".encode()).decode()
    with TestClient(gateway, base_url=ORIGIN) as client:
        assert client.get("/console", headers={"Authorization": basic}).headers["referrer-policy"] == "no-referrer"
