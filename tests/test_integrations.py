"""Sponsor integrations: APIMatic PayPal Server SDK, Bryntum fulfillment board, AG Grid views,
Channel3 market prices."""
import json
from datetime import datetime

import httpx
import pytest
import requests

from conftest import BASE, make_app, new_customer, pay_via_simulator, say
from tendero.catalog import Catalog
from tendero.fulfillment import SHOP_TZ, Fulfillment, PlanError
from tendero.market import Channel3Client, Market, summarize
from tendero.paypal_client import PayPalError
from tendero.paypal_sdk import PayPalSDKClient
from tendero.store import Store

CAT = Catalog.load()


def at(y, mo, d, h, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=SHOP_TZ).timestamp()


async def sell(client, app, text="2 mate gourds and a bombilla"):
    cid = await new_customer(client)
    await say(client, cid, text)
    card = (await say(client, cid, "yes"))["card"]
    await pay_via_simulator(client, app, card["url"])
    return cid, app.state.store.latest_order_for(cid)


# ------------------------------------------------------------------ APIMatic SDK
class Recorder(requests.adapters.BaseAdapter):
    """requests seam recommended by the APIMatic context plugin's python-testing skill."""

    def __init__(self, replies):
        super().__init__()
        self.replies, self.seen = replies, []

    def send(self, request, **kw):
        self.seen.append(request)
        path = request.url.split("paypal.com", 1)[1]
        status, body = self.replies.get(path, (200, {}))
        r = requests.Response()
        r.status_code, r._content, r.url, r.request = status, json.dumps(body).encode(), request.url, request
        r.headers["Content-Type"] = "application/json"
        return r

    def close(self):
        pass


def sdk_with(replies):
    rec = Recorder({"/v1/oauth2/token": (200, {"access_token": "T", "token_type": "Bearer", "expires_in": 3600}),
                    **replies})
    c = PayPalSDKClient("id", "secret", rest=None)
    s = requests.Session()
    s.mount("https://", rec)
    from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient
    c.sdk = PaypalServersdkClient(config=c.sdk.config.clone_with(http_client_instance=s))
    return c, rec


async def test_sdk_create_order_request_shape():
    cart = CAT.price_cart([{"sku": "MATE-CAL", "quantity": 2}, {"sku": "BOMB-ALP", "quantity": 1}])
    c, rec = sdk_with({"/v2/checkout/orders": (201, {"id": "PP1", "status": "PAYER_ACTION_REQUIRED",
                                                    "links": [{"rel": "payer-action", "href": "https://x/approve"}]})})
    out = await c.create_order(reference_id="TN-1001", cart=cart, brand_name="Taller", return_url="https://r",
                               cancel_url="https://c")
    assert out["id"] == "PP1" and out["links"][0]["rel"] == "payer-action"  # wire JSON kept as-is
    req = rec.seen[-1]
    assert req.url == "https://api-m.sandbox.paypal.com/v2/checkout/orders"
    assert req.headers["PayPal-Request-Id"] == "create-TN-1001" and req.headers["Authorization"] == "Bearer T"
    body = json.loads(req.body)
    pu = body["purchase_units"][0]
    assert body["intent"] == "CAPTURE" and pu["custom_id"] == "TN-1001" and pu["amount"]["value"] == "60.00"
    assert pu["amount"]["breakdown"]["item_total"]["value"] == "60.00"
    assert [i["sku"] for i in pu["items"]] == ["MATE-CAL", "BOMB-ALP"] and pu["items"][0]["quantity"] == "2"
    ctx = body["payment_source"]["paypal"]["experience_context"]
    assert ctx["user_action"] == "PAY_NOW" and ctx["return_url"] == "https://r"


async def test_sdk_errors_become_paypal_errors():
    c, _ = sdk_with({"/v2/checkout/orders/PP9/capture": (422, {
        "name": "UNPROCESSABLE_ENTITY", "debug_id": "d1",
        "details": [{"issue": "ORDER_NOT_APPROVED"}]})})
    with pytest.raises(PayPalError) as e:
        await c.capture_order("PP9")
    assert e.value.status == 422 and e.value.body["details"][0]["issue"] == "ORDER_NOT_APPROVED"


async def test_default_backend_is_apimatic_sdk_and_survives_early_webhook(client, app):
    assert isinstance(app.state.payments.paypal, PayPalSDKClient)
    _, o = await sell(client, app)
    kinds = [e["kind"] for e in app.state.store.events(0) if e["order_id"] == o["id"]]
    # with the in-process simulator the signed webhook lands before the capture response returns
    assert kinds.index("reconciled") < kinds.index("paypal.capture")
    assert o["status"] == "PAID" and o["paypal_fee"] == "2.58"


async def test_rest_backend_still_works(tmp_path):
    app = make_app(tmp_path, paypal_sdk="rest")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        assert not isinstance(app.state.payments.paypal, PayPalSDKClient)
        _, o = await sell(c, app, "a yerba")
        assert o["status"] == "PAID"


# ---------------------------------------------------------- Bryntum fulfillment
def order(oid, lines):
    return {"id": oid, "cart": {"lines": [{"sku": s, "quantity": q, "name": CAT.products[s].name} for s, q in lines]}}


def test_fulfillment_plan_respects_hours_sequence_and_courier_days(tmp_path):
    store = Store(str(tmp_path / "f.sqlite3"))
    f = Fulfillment(store, clock=lambda: at(2026, 11, 6, 15, 5))  # Friday 15:05
    t = {x["kind"]: x for x in f.plan(order("TN-1", [("MATE-CAL", 2), ("BOMB-ALP", 1)]))}
    assert (t["qa"]["start"], t["qa"]["end"]) == ("2026-11-06T15:15:00", "2026-11-06T15:45:00")
    assert (t["pack"]["start"], t["pack"]["end"]) == ("2026-11-06T15:45:00", "2026-11-06T16:15:00")
    assert t["pickup"]["start"] == "2026-11-06T17:00:00"
    assert f.plan(order("TN-1", [("MATE-CAL", 2)])) == list(store.fulfillment_tasks("TN-1"))  # idempotent
    # a second order (nothing to inspect) uses the free bench slot before TN-1's packing, same pickup
    t2 = {x["kind"]: x for x in f.plan(order("TN-2", [("YERBA-1K", 1)]))}
    assert "qa" not in t2 and (t2["pack"]["start"], t2["pack"]["end"]) == ("2026-11-06T15:15:00", "2026-11-06T15:35:00")
    assert t2["pickup"]["start"] == "2026-11-06T17:00:00"
    # a third one has to wait until the bench is free again
    t4 = {x["kind"]: x for x in f.plan(order("TN-4", [("YERBA-1K", 2)]))}
    assert t4["pack"]["start"] == "2026-11-06T16:15:00"
    # paid Friday evening: packing spills to Saturday morning, courier only comes Monday
    f.clock = lambda: at(2026, 11, 6, 18, 40)
    t3 = {x["kind"]: x for x in f.plan(order("TN-3", [("ALF-12", 3)]))}
    assert t3["pack"]["start"] == "2026-11-07T09:00:00" and t3["pickup"]["start"] == "2026-11-09T17:00:00"
    board = f.board()
    assert {r["id"] for r in board["resources"]} == {"qa", "bench", "courier"}
    assert len(board["events"]) == 9 and len(board["dependencies"]) == 5


def test_fulfillment_moves_are_validated(tmp_path):
    store = Store(str(tmp_path / "f.sqlite3"))
    f = Fulfillment(store, clock=lambda: at(2026, 11, 9, 10, 0))  # Monday
    t = {x["kind"]: x for x in f.plan(order("TN-1", [("CASE-LTH", 1)]))}
    with pytest.raises(PlanError, match="17:00"):
        f.move(t["pickup"]["id"], "2026-11-09T15:00:00", "2026-11-09T15:30:00")
    with pytest.raises(PlanError, match="Saturday|Monday to Friday"):
        f.move(t["pickup"]["id"], "2026-11-14T17:00:00", "2026-11-14T17:30:00")
    with pytest.raises(PlanError, match="after"):
        f.move(t["pack"]["id"], "2026-11-09T09:00:00", "2026-11-09T09:20:00")
    with pytest.raises(PlanError, match="hours"):
        f.move(t["pack"]["id"], "2026-11-09T19:00:00", "2026-11-09T19:20:00")
    moved = f.move(t["pickup"]["id"], "2026-11-10T17:00:00", "2026-11-10T17:30:00")
    assert moved["start"] == "2026-11-10T17:00:00"
    assert any(e["kind"] == "fulfillment.moved" for e in store.events())


async def test_paid_order_is_booked_customer_told_and_refund_unbooks(client, app):
    cid, o = await sell(client, app)
    tasks = app.state.store.fulfillment_tasks(o["id"])
    assert [t["kind"] for t in tasks] == ["qa", "pack", "pickup"]
    last = (await client.get(f"/api/conversations/{cid}/messages")).json()["messages"][-1]
    assert "Planned dispatch:" in last["text"] and "Buenos Aires time" in last["text"]
    board = (await client.get("/api/merchant/fulfillment")).json()
    assert len(board["events"]) == 3 and board["dependencies"]
    pick = next(e for e in board["events"] if e["kind"] == "pickup")
    r = await client.patch(f"/api/merchant/fulfillment/{pick['id']}",
                           json={"startDate": pick["startDate"][:11] + "12:00:00",
                                 "endDate": pick["startDate"][:11] + "12:30:00"})
    assert r.status_code == 409 and "17:00" in r.json()["error"]

    mid = (await client.post("/api/conversations", json={"kind": "merchant"})).json()["conversation_id"]
    m = await say(client, mid, "What do we ship next?")
    assert m["tools"][0]["name"] == "fulfillment_plan" and o["id"] in m["text"] and m["card"]["type"] == "board"
    await say(client, mid, "Refund the last order - wrong colour")
    await app.state.mock.drain()
    assert app.state.store.fulfillment_tasks(o["id"]) == []


# ------------------------------------------------------------------ AG Grid views
async def test_copilot_filters_the_orders_grid(client, app):
    await sell(client, app)
    cid = await new_customer(client, "B")
    await say(client, cid, "3 bombillas please")
    await say(client, cid, "yes")  # awaiting payment, 36.00 + 9.00 shipping... priced by the catalog
    mid = (await client.post("/api/conversations", json={"kind": "merchant"})).json()["conversation_id"]
    m = await say(client, mid, "Show me unpaid orders over $20")
    assert m["tools"][0]["name"] == "show_orders" and m["tools"][0]["input"] == {
        "status": "AWAITING_PAYMENT", "min_total": "20", "sort": "newest"}
    assert m["card"] == {"type": "grid_view", "status": "AWAITING_PAYMENT", "min_total": "20.00",
                         "sort": "newest", "count": 1}
    assert "Showing 1 order(s)" in m["text"]
    m = await say(client, mid, "Which orders are biggest?")
    assert m["card"]["sort"] == "total_desc" and m["card"]["count"] == 2


async def test_inventory_view_counts_sold_and_reserved(client, app):
    await sell(client, app)  # 2 gourds + 1 bombilla PAID
    cid = await new_customer(client, "B")
    await say(client, cid, "one leather case")
    await say(client, cid, "yes")  # reserved while awaiting payment
    rows = {r["sku"]: r for r in (await client.get("/api/merchant/inventory")).json()["rows"]}
    assert (rows["MATE-CAL"]["sold"], rows["MATE-CAL"]["available"], rows["MATE-CAL"]["revenue"]) == (2, 16, "48.00")
    assert (rows["CASE-LTH"]["reserved"], rows["CASE-LTH"]["available"], rows["CASE-LTH"]["level"]) == (1, 6, "ok")
    assert rows["ALF-12"]["level"] == "ok" and rows["YERBA-1K"]["sold"] == 0


# ------------------------------------------------------------------- Channel3
async def test_channel3_client_request_and_parsing():
    seen = {}

    def handler(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(200, json={"products": [
            {"id": "a", "title": "Gourd A", "offers": [
                {"domain": "a.example", "url": "u1", "price": {"price": 30, "currency": "USD"},
                 "availability": "InStock", "condition": "new"},
                {"domain": "b.example", "url": "u2", "price": {"amount": 25, "currency": "USD"},
                 "availability": "InStock", "condition": "new"}]},
            {"id": "b", "title": "Gourd B used", "offers": [
                {"domain": "c.example", "url": "u3", "price": {"price": 5, "currency": "USD"},
                 "availability": "InStock", "condition": "used"}]},
            {"id": "c", "title": "Gourd C EUR", "offers": [
                {"domain": "d.example", "url": "u4", "price": {"price": 20, "currency": "EUR"},
                 "availability": "InStock", "condition": "new"}]},
        ]})

    c3 = Channel3Client("k-123", transport=httpx.MockTransport(handler))
    m = Market(CAT, client=c3)
    res = await m.check("MATE-CAL")
    req = seen["req"]
    assert req.url.path == "/v1/search" and req.headers["x-api-key"] == "k-123"
    body = json.loads(req.content)
    assert body["query"] == CAT.market_queries["MATE-CAL"] and body["filters"]["availability"] == ["InStock"]
    row = res["rows"][0]
    assert res["live"] and row["offers"] == 1 and row["low"] == "25.00"  # used + EUR offers ignored
    assert row["comparables"][0]["domain"] == "b.example"
    await m.aclose()


async def test_market_sample_mode_is_labelled_and_copilot_reports_it(client, app):
    res = await app.state.market.check()
    assert not res["live"] and "sample" in res["source"]
    rows = {r["sku"]: r for r in res["rows"]}
    assert rows["MATE-CAL"]["median"] == "26.50" and rows["MATE-CAL"]["position"] == "in line"
    assert rows["ALF-12"]["offers"] == 2  # the EUR offer is not comparable
    assert summarize([], "USD") == {"comparables": []}
    mid = (await client.post("/api/conversations", json={"kind": "merchant"})).json()["conversation_id"]
    m = await say(client, mid, "Are our prices competitive?")
    assert m["tools"][0]["name"] == "market_price_check" and "sample data" in m["text"]
    view = (await client.get("/api/merchant/market")).json()
    assert len(view["rows"]) == 5 and view["live"] is False


async def test_config_reports_integrations(client):
    cfg = (await client.get("/api/config")).json()["integrations"]
    assert "APIMatic" in cfg["paypal_sdk"] and "sample" in cfg["channel3"]
    assert "Bryntum" in cfg["scheduler"] and "AG Grid" in cfg["grid"]


async def test_copilot_checks_a_single_product_price(client, app):
    mid = (await client.post("/api/conversations", json={"kind": "merchant"})).json()["conversation_id"]
    m = await say(client, mid, "How does our yerba price compare with the market?")
    assert m["tools"][0] == {"name": "market_price_check", "input": {"sku": "YERBA-1K"}, "ok": True}
    assert "above market" in m["text"] and m["card"] == {"type": "market", "live": False}
    m = await say(client, mid, "price check for NOPE-1")
    assert m["tools"][0]["input"] == {"sku": ""}  # unknown product -> whole catalog, never a guess
