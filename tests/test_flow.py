"""End-to-end flows through the real FastAPI app + the local PayPal simulator."""
import json
import time

import httpx

from mock_paypal.server import CERT_URL, sign
from tendero.agent.llm import LLMTurn
from conftest import BASE, make_app, new_customer, pay_via_simulator, say


async def test_full_sale_chat_to_verified_payment(client, app):
    cid = await new_customer(client)
    m = await say(client, cid, "Hi! Do you ship to Madrid? I'd like 2 mate gourds and a bombilla")
    assert [t["name"] for t in m["tools"]] == ["build_cart", "store_info"]
    assert "60.00 USD" in m["text"] and "Madrid" in m["text"]
    order = app.state.store.latest_order_for(cid)
    assert order["status"] == "DRAFT" and order["total"] == "60.00"  # free shipping at 60.00

    m = await say(client, cid, "Yes, send me the link")
    assert m["tools"][0]["name"] == "create_payment_link" and m["tools"][0]["ok"]
    card = m["card"]
    assert card["type"] == "pay" and card["total"] == "60.00"
    assert "/__mock_paypal/checkoutnow?token=" in card["url"]
    assert app.state.store.get_order(order["id"])["status"] == "AWAITING_PAYMENT"

    await pay_via_simulator(client, app, card["url"])
    o = app.state.store.get_order(order["id"])
    assert o["status"] == "PAID" and o["paid_via"] == "verified webhook"
    assert o["paypal_fee"] == "2.58" and o["net_amount"] == "57.42"  # 3.49% + 0.49

    msgs = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
    assert msgs[-1]["card"]["type"] == "paid" and "Payment confirmed by PayPal" in msgs[-1]["text"]
    kinds = [e["kind"] for e in app.state.store.events()]
    for k in ["order.draft", "paypal.order.created", "buyer.returned", "paypal.capture",
              "webhook.verified", "reconciled", "customer.notified"]:
        assert k in kinds, k

    m = await say(client, cid, "Did my payment go through?")
    assert "PAID" in m["text"]


async def test_cannot_link_in_same_turn_as_pricing(tmp_path):
    class Greedy:  # an over-eager model that tries to charge without showing the total
        name, label = "greedy", "greedy"

        async def complete(self, system, tools, messages):
            last = messages[-1]["content"]
            if isinstance(last, str):
                return LLMTurn([{"type": "tool_use", "id": "t1", "name": "build_cart",
                                 "input": {"items": [{"sku": "MATE-CAL", "quantity": 1}]}}], "tool_use")
            res = json.loads(last[0]["content"]) if not last[0].get("is_error") else {}
            if "order_id" in res:
                return LLMTurn([{"type": "tool_use", "id": "t2", "name": "create_payment_link",
                                 "input": {"order_id": res["order_id"]}}], "tool_use")
            return LLMTurn([{"type": "text", "text": "ok"}], "end_turn")

    app = make_app(tmp_path, provider=Greedy())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        cid = await new_customer(c)
        m = await say(c, cid, "one gourd, charge me now")
        assert m["tools"][1]["name"] == "create_payment_link" and not m["tools"][1]["ok"]
        assert "has not seen this total" in m["tools"][1]["error"]
        assert app.state.store.latest_order_for(cid)["status"] == "DRAFT"


async def test_forged_webhook_is_rejected(client, app):
    cid = await new_customer(client)
    await say(client, cid, "3 bombillas please")
    await say(client, cid, "yes")
    r = await client.post("/api/demo/forge-webhook", json={})
    body = r.json()
    assert body["http_status"] == 400 and body["order_status_after"] == "AWAITING_PAYMENT"
    assert any(e["kind"] == "webhook.rejected" for e in app.state.store.events())


async def test_tampered_signed_webhook_fails(client, app):
    event = {"id": "WH-X", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"custom_id": "TN-1001"}}
    tid, ts = "tid-1", "2026-11-05T00:00:00Z"
    sig = sign(tid, ts, "MOCK-WEBHOOK-ID", event)
    event["resource"]["amount"] = {"value": "1.00", "currency_code": "USD"}  # tampered after signing
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": CERT_URL,
               "PAYPAL-TRANSMISSION-ID": tid, "PAYPAL-TRANSMISSION-TIME": ts, "PAYPAL-TRANSMISSION-SIG": sig}
    r = await client.post("/webhooks/paypal", content=json.dumps(event), headers=headers)
    assert r.status_code == 400
    r = await client.post("/webhooks/paypal", content=json.dumps(event))  # no PayPal headers at all
    assert r.status_code == 400


async def test_duplicate_webhook_is_idempotent(client, app):
    cid = await new_customer(client)
    await say(client, cid, "a yerba")
    card = (await say(client, cid, "yes please"))["card"]
    await pay_via_simulator(client, app, card["url"])
    ev = app.state.mock.sent[-1]
    tid, ts = "replay-1", "2026-11-05T00:00:00Z"
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": CERT_URL, "PAYPAL-TRANSMISSION-ID": tid,
               "PAYPAL-TRANSMISSION-TIME": ts, "PAYPAL-TRANSMISSION-SIG": sign(tid, ts, "MOCK-WEBHOOK-ID", ev)}
    r = await client.post("/webhooks/paypal", content=json.dumps(ev), headers=headers)
    assert r.json()["status"] == "duplicate"
    msgs = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"]
    assert sum(1 for x in msgs if x["card"] and x["card"]["type"] == "paid") == 1


async def test_lost_webhook_is_recovered_by_sweep(client, app):
    async def drop(*a, **k):
        return None

    app.state.mock.deliver = drop  # webhook never arrives
    cid = await new_customer(client)
    await say(client, cid, "one leather case")
    card = (await say(client, cid, "ok"))["card"]
    await pay_via_simulator(client, app, card["url"])
    oid = app.state.store.latest_order_for(cid)["id"]
    assert app.state.store.get_order(oid)["status"] == "CAPTURED"
    r = await client.post("/api/merchant/sweep")
    assert oid in r.json()["advanced"]
    o = app.state.store.get_order(oid)
    assert o["status"] == "PAID" and o["paid_via"].startswith("API sweep")


async def test_closed_tab_is_captured_from_order_approved_webhook(client, app):
    cid = await new_customer(client)
    await say(client, cid, "two alfajores")
    card = (await say(client, cid, "sure"))["card"]
    ppid = card["url"].split("token=")[1]
    r = await client.post(f"/__mock_paypal/checkoutnow/{ppid}/approve")  # buyer approves, never returns
    assert r.status_code == 303
    await app.state.mock._approved_fallback(ppid, wait=0)
    await app.state.mock.drain()
    o = app.state.store.latest_order_for(cid)
    assert o["status"] == "PAID"


async def test_amount_mismatch_flags_for_review(client, app):
    cid = await new_customer(client)
    await say(client, cid, "1 gourd")
    await say(client, cid, "yes")
    o = app.state.store.latest_order_for(cid)
    bad_capture = {"id": "CAP1", "status": "COMPLETED", "custom_id": o["id"],
                   "amount": {"currency_code": "USD", "value": "1.00"}}
    o2 = app.state.payments._settle(o, bad_capture, via="test")
    assert o2["status"] == "MISMATCH" and "captured 1.00" in o2["note"]


async def test_merchant_copilot_report_and_refund(client, app):
    cid = await new_customer(client)
    await say(client, cid, "2 mate gourds and a bombilla")
    card = (await say(client, cid, "yes"))["card"]
    await pay_via_simulator(client, app, card["url"])

    mid = (await client.post("/api/conversations", json={"kind": "merchant"})).json()["conversation_id"]
    m = await say(client, mid, "How did we do today? Any issues?")
    assert m["tools"][0]["name"] == "reconciliation_report"
    assert "60.00" in m["text"] and "57.42" in m["text"]

    m = await say(client, mid, "Refund the last order - the gourd arrived cracked")
    assert [t["name"] for t in m["tools"]] == ["list_orders", "refund_order"]
    await app.state.mock.drain()
    o = app.state.store.latest_order_for(cid)
    assert o["status"] == "REFUNDED" and o["refunded_amount"] == "60.00"
    last = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"][-1]
    assert "refund of 60.00 USD" in last["text"]

    m = await say(client, mid, "Refund TN-1001 - again")
    assert not m["tools"][0]["ok"] and "only PAID orders" in m["tools"][0]["error"]


async def test_orders_belong_to_their_conversation(client, app):
    a = await new_customer(client, "A")
    await say(client, a, "1 gourd")
    b = await new_customer(client, "B")
    from tendero.agent.tools import CustomerTools, ToolError

    tools = CustomerTools(app.state.payments, b)
    tools.turn_started_at = time.time() + 10
    try:
        await tools.run("create_payment_link", {"order_id": "TN-1001"})
        assert False, "should not be able to pay someone else's order"
    except ToolError as e:
        assert "not found in this conversation" in str(e)
