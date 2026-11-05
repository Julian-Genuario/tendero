from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from mock_paypal.server import MockPayPal
from tendero.agent.agent import Agent
from tendero.agent.llm import AnthropicProvider
from tendero.agent.tools import MerchantTools
from tendero.catalog import CartError, Catalog
from tendero.paypal_client import PayPalClient, PayPalError
from tendero.reconcile import reconcile_capture

CAT = Catalog.load()


def test_pricing_and_shipping():
    c = CAT.price_cart([{"sku": "BOMB-ALP", "quantity": 1}])
    assert (c["subtotal"], c["shipping"], c["total"]) == ("12.00", "9.00", "21.00")
    c = CAT.price_cart([{"sku": "mate-cal", "quantity": 2}, {"sku": "BOMB-ALP", "quantity": 1}])
    assert (c["subtotal"], c["shipping"], c["total"]) == ("60.00", "0.00", "60.00")
    c = CAT.price_cart([{"sku": "BOMB-ALP", "quantity": 1}, {"sku": "BOMB-ALP", "quantity": 2}])
    assert c["lines"][0]["quantity"] == 3


@pytest.mark.parametrize("items,msg", [
    ([], "empty"), ([{"sku": "NOPE", "quantity": 1}], "Unknown SKU"),
    ([{"sku": "CASE-LTH", "quantity": 8}], "in stock"), ([{"sku": "BOMB-ALP", "quantity": 0}], "Invalid quantity"),
])
def test_cart_errors(items, msg):
    with pytest.raises(CartError, match=msg):
        CAT.price_cart(items)


def test_order_cap():
    with pytest.raises(CartError, match="per-order limit"):
        CAT.price_cart([{"sku": "MATE-CAL", "quantity": 18}], max_total=Decimal("100"))


def test_find_in_text():
    assert CAT.find_in_text("2 mate gourds and a bombilla") == ["MATE-CAL", "BOMB-ALP"]
    assert CAT.find_in_text("quiero una calabaza y yerba") == ["MATE-CAL", "YERBA-1K"]


def test_reconcile_rules():
    order = {"id": "TN-1", "currency": "USD", "total": "60.00"}
    ok = {"status": "COMPLETED", "custom_id": "TN-1", "amount": {"currency_code": "USD", "value": "60.00"},
          "seller_receivable_breakdown": {"paypal_fee": {"value": "2.58"}, "net_amount": {"value": "57.42"}}}
    r = reconcile_capture(order, ok)
    assert r.ok and r.paypal_fee == "2.58" and r.net_amount == "57.42"
    assert not reconcile_capture(order, {**ok, "amount": {"currency_code": "EUR", "value": "60.00"}}).ok
    assert not reconcile_capture(order, {**ok, "custom_id": "TN-2"}).ok
    assert not reconcile_capture(order, {**ok, "status": "PENDING"}).ok


async def _mock_client(secret="mock-client-secret"):
    mock = MockPayPal(public_base="http://x", webhook_url="http://x/wh", webhook_id="W", deliver=None,
                      approved_fallback_wait=None)

    async def noop(*a):
        return None

    mock.deliver = noop
    return mock, PayPalClient("http://x", "mock-client-id", secret, transport=httpx.ASGITransport(app=mock.app))


async def test_client_auth_error():
    _, pp = await _mock_client(secret="wrong")
    with pytest.raises(PayPalError) as e:
        await pp.token()
    assert e.value.status == 401


async def test_client_order_idempotency_and_capture_rules():
    mock, pp = await _mock_client()
    cart = CAT.price_cart([{"sku": "YERBA-1K", "quantity": 1}])
    kw = dict(reference_id="TN-9", cart=cart, brand_name="B", return_url="http://x/r?o=1", cancel_url="http://x/c?o=1")
    a = await pp.create_order(**kw)
    b = await pp.create_order(**kw)  # same PayPal-Request-Id -> same order
    assert a["id"] == b["id"] and a["status"] == "PAYER_ACTION_REQUIRED"
    with pytest.raises(PayPalError) as e:
        await pp.capture_order(a["id"])
    assert e.value.body["details"][0]["issue"] == "ORDER_NOT_APPROVED"
    mock.orders[a["id"]]["status"] = "APPROVED"
    cap = await pp.capture_order(a["id"])
    assert cap["status"] == "COMPLETED"
    with pytest.raises(PayPalError) as e:
        await pp.capture_order(a["id"])
    assert e.value.body["details"][0]["issue"] == "ORDER_ALREADY_CAPTURED"
    await mock.drain()


async def test_verify_requires_all_headers():
    _, pp = await _mock_client()
    assert await pp.verify_webhook_signature({}, {"id": "x"}, "W") is False


class _FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0)


def _blk(**d):
    return SimpleNamespace(model_dump=lambda mode="json": d)


async def test_anthropic_provider_tool_loop_and_request_shape():
    replies = [
        SimpleNamespace(stop_reason="tool_use", model="claude-sonnet-5-5", usage=None, content=[
            _blk(type="thinking", thinking="", signature="sig=="),
            _blk(type="tool_use", id="tu1", name="list_orders", input={"status": "ANY"}, caller={"type": "direct"}),
        ]),
        SimpleNamespace(stop_reason="end_turn", model="claude-sonnet-5-5", usage=None,
                        content=[_blk(type="text", text="No orders yet.", citations=None)]),
    ]
    fake = SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages(replies)))
    prov = AnthropicProvider("k", client=fake)

    class Tools:
        definitions = [{"name": "list_orders"}]
        cards = []

        async def run(self, name, args):
            return {"orders": []}

    res, history = await Agent(prov, "sys").run_turn([], "orders?", Tools())
    assert res.text == "No orders yet." and res.tool_trace[0]["name"] == "list_orders"
    first, second = fake.beta.messages.calls
    assert first["model"] == "claude-sonnet-5-5" and first["fallbacks"] == "default"
    assert first["betas"] == ["server-side-fallback-2026-07-01"] and first["output_config"] == {"effort": "low"}
    # thinking + tool_use blocks are passed back unchanged; unknown fields are stripped
    assistant = second["messages"][1]["content"]
    assert assistant[0] == {"type": "thinking", "thinking": "", "signature": "sig=="}
    assert assistant[1] == {"type": "tool_use", "id": "tu1", "name": "list_orders", "input": {"status": "ANY"}}
    assert second["messages"][2]["content"][0]["type"] == "tool_result"


async def test_refusal_is_handled():
    fake = SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages([
        SimpleNamespace(stop_reason="refusal", model="m", usage=None, content=[])])))

    class Tools:
        definitions, cards = [], []

    res, _ = await Agent(AnthropicProvider("k", client=fake), "s").run_turn([], "x", Tools())
    assert "shop owner" in res.text
