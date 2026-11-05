import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tendero.app import create_app  # noqa: E402
from tendero.config import load_settings  # noqa: E402

BASE = "http://localhost:8000"


def make_app(tmp_path, provider=None, **overrides):
    s = load_settings(
        db_path=str(tmp_path / "t.sqlite3"),
        paypal_mode="mock",
        paypal_client_id="mock-client-id",
        paypal_client_secret="mock-client-secret",
        paypal_webhook_id="MOCK-WEBHOOK-ID",
        public_base_url=BASE,
        llm_provider="scripted",
        mock_webhook_delay=0.0,
        **overrides,
    )
    app = create_app(s, provider=provider)
    app.state.mock.approved_fallback_wait = None
    return app


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        yield c


async def new_customer(client, name="Lucía (Madrid)"):
    r = await client.post("/api/conversations", json={"kind": "customer", "customer_name": name})
    return r.json()["conversation_id"]


async def say(client, cid, text):
    r = await client.post("/api/chat", json={"conversation_id": cid, "text": text})
    assert r.status_code == 200, r.text
    msgs = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
    return msgs[-1]


async def pay_via_simulator(client, app, approve_url):
    """Acts as the buyer on the simulator page, then follows the redirect to our return URL."""
    oid = approve_url.split("token=")[1]
    r = await client.post(f"/__mock_paypal/checkoutnow/{oid}/approve")
    assert r.status_code == 303
    ret = r.headers["location"]
    r2 = await client.get(ret.replace(BASE, ""))
    assert r2.status_code == 200
    await app.state.mock.drain()
    return oid
