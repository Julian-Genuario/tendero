"""FastAPI app: chat API, PayPal return/cancel URLs, webhook endpoint, merchant console API."""
from __future__ import annotations

import html
import json
import logging
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent.agent import CUSTOMER_SYSTEM, MERCHANT_SYSTEM, Agent
from .agent.llm import AnthropicProvider, LLMProvider, ScriptedProvider
from .agent.tools import CustomerTools, MerchantTools
from .catalog import Catalog
from .config import Settings, load_settings
from .fulfillment import PlanError
from .market import Market
from .payments import PaymentFlowError, Payments
from .paypal_client import PayPalClient, PayPalError
from .store import Store

STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("tendero")


class NewConversation(BaseModel):
    kind: str = "customer"
    customer_name: str | None = None


class ChatIn(BaseModel):
    conversation_id: str
    text: str


class ForgeIn(BaseModel):
    order_id: str | None = None


class MoveIn(BaseModel):
    startDate: str
    endDate: str


def build_provider(s: Settings, catalog: Catalog) -> LLMProvider:
    if s.resolved_llm == "anthropic":
        return AnthropicProvider(s.anthropic_api_key, s.anthropic_model, s.anthropic_effort, s.anthropic_fallbacks)
    return ScriptedProvider(catalog)


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None) -> FastAPI:
    s = settings or load_settings()
    app = FastAPI(title="Tendero", docs_url="/api/docs")
    catalog = Catalog.load()
    s.merchant_name = catalog.store["name"]
    store = Store(s.db_path)

    transport = None
    mock = None
    if s.paypal_mode == "mock":
        from mock_paypal.server import MockPayPal

        async def deliver(url: str, headers: dict, body: bytes):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://internal") as c:
                await c.post("/webhooks/paypal", content=body, headers=headers)

        mock = MockPayPal(public_base=s.public_base_url, webhook_url=f"{s.public_base_url}/webhooks/paypal",
                          webhook_id=s.paypal_webhook_id, client_id=s.paypal_client_id,
                          client_secret=s.paypal_client_secret, webhook_delay=s.mock_webhook_delay,
                          deliver=deliver)
        app.mount("/__mock_paypal", mock.app)
        transport = httpx.ASGITransport(app=app)
        paypal = PayPalClient("http://internal/__mock_paypal", s.paypal_client_id, s.paypal_client_secret,
                              transport=transport)
        if s.paypal_sdk == "apimatic":
            from .paypal_sdk import ASGIBridge, PayPalSDKClient

            paypal = PayPalSDKClient(s.paypal_client_id, s.paypal_client_secret, sandbox=True, rest=paypal,
                                     bridge=ASGIBridge(app, "/__mock_paypal"))
    else:
        if not (s.paypal_client_id and s.paypal_client_secret):
            raise RuntimeError("PAYPAL_MODE=sandbox needs PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET")
        paypal = PayPalClient(s.resolved_paypal_base, s.paypal_client_id, s.paypal_client_secret)
        if s.paypal_sdk == "apimatic":
            from .paypal_sdk import PayPalSDKClient

            paypal = PayPalSDKClient(s.paypal_client_id, s.paypal_client_secret,
                                     sandbox=True, rest=paypal)

    payments = Payments(s, store, paypal, catalog)
    market = Market(catalog, s.channel3_api_key)
    market_cache: dict = {}
    provider = provider or build_provider(s, catalog)
    customer_agent = Agent(provider, CUSTOMER_SYSTEM.format(store=catalog.store["name"], tagline=catalog.store["tagline"]))
    merchant_agent = Agent(provider, MERCHANT_SYSTEM.format(store=catalog.store["name"]))
    app.state.settings, app.state.store, app.state.payments = s, store, payments
    app.state.mock, app.state.provider, app.state.market = mock, provider, market

    # ------------------------------------------------------------------- UI
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    async def config():
        return {
            "store": catalog.store,
            "llm": {"provider": provider.name, "label": provider.label, "demo": provider.name == "scripted"},
            "paypal": {"mode": s.paypal_mode,
                       "label": "PayPal sandbox" if s.paypal_mode == "sandbox" else "Local PayPal sandbox simulator"},
            "demo_tools": s.demo_tools,
            "integrations": {
                "paypal_sdk": getattr(paypal, "sdk_name", "PayPal REST (httpx)"),
                "channel3": market.source,
                "grid": "AG Grid Community + AG Charts Community",
                "scheduler": "Bryntum Scheduler Community",
            },
        }

    # ----------------------------------------------------------------- chat
    @app.post("/api/conversations")
    async def new_conversation(body: NewConversation):
        cid = store.create_conversation(body.kind, body.customer_name)
        return {"conversation_id": cid}

    @app.get("/api/conversations/{cid}/messages")
    async def get_messages(cid: str, after: int = 0):
        if not store.get_conversation(cid):
            raise HTTPException(404, "conversation not found")
        return {"messages": store.messages(cid, after)}

    @app.post("/api/chat")
    async def chat(body: ChatIn):
        conv = store.get_conversation(body.conversation_id)
        if not conv:
            raise HTTPException(404, "conversation not found")
        text = body.text.strip()[:2000]
        if not text:
            raise HTTPException(400, "empty message")
        role = "customer" if conv["kind"] == "customer" else "merchant"
        store.add_message(conv["id"], role, text)
        if conv["kind"] == "customer":
            tools = CustomerTools(payments, conv["id"], conv["customer_name"])
            agent = customer_agent
        else:
            tools = MerchantTools(payments, market)
            agent = merchant_agent
        history = store.get_history(conv["id"])
        try:
            result, history = await agent.run_turn(history, text, tools)
        except Exception as e:  # LLM outage etc. - never leave the customer hanging
            log.exception("agent turn failed")
            store.add_message(conv["id"], "agent", "Sorry, I'm having trouble right now. The shop owner "
                                                   "has been notified and will reply soon.")
            store.add_event(None, "agent.error", f"{type(e).__name__}: {e}", ok=False)
            return {"ok": False}
        store.set_history(conv["id"], history)
        for c in result.cards:
            if c["type"] == "market":
                market_cache["last"] = getattr(tools, "last_market", None) or market_cache.get("last")
        card = result.cards[-1] if result.cards else None
        store.add_message(conv["id"], "agent", result.text, card=card, tools=result.tool_trace)
        for t in result.tool_trace:
            store.add_event(None, "agent.tool", f"{'customer' if role == 'customer' else 'copilot'} agent "
                                                f"called {t['name']}" + ("" if t["ok"] else f" -> refused: {t.get('error', '')[:90]}"),
                            ok=t["ok"])
        return {"ok": True, "model": result.model}

    # --------------------------------------------------------------- PayPal
    @app.get("/paypal/return", response_class=HTMLResponse)
    async def paypal_return(order: str, token: str | None = None, PayerID: str | None = None):
        try:
            o = await payments.handle_return(order, token)
            msg = ("Payment approved. You can close this window - the shop's assistant will "
                   "confirm in the chat as soon as PayPal's signed notification arrives.")
        except (PaymentFlowError, PayPalError) as e:
            o, msg = None, f"We could not complete the payment: {html.escape(str(e))}"
        return HTMLResponse(_return_page(msg, o["id"] if o else order, ok=o is not None))

    @app.get("/paypal/cancel", response_class=HTMLResponse)
    async def paypal_cancel(order: str, token: str | None = None):
        store.add_event(order, "buyer.cancelled", "buyer cancelled on the PayPal page", ok=False)
        return HTMLResponse(_return_page("Payment cancelled. Nothing was charged.", order, ok=False))

    @app.post("/webhooks/paypal")
    async def paypal_webhook(request: Request):
        raw = await request.body()
        status, body = await payments.handle_webhook(dict(request.headers), raw)
        return JSONResponse(body, status_code=status)

    # ------------------------------------------------------------- merchant
    @app.get("/api/merchant/orders")
    async def orders():
        return {"orders": store.list_orders()}

    @app.get("/api/merchant/events")
    async def events(after: int = 0):
        return {"events": store.events(after)}

    @app.post("/api/merchant/sweep")
    async def sweep():
        return await payments.sweep(force=True)

    @app.get("/api/merchant/inventory")
    async def inventory():
        return {"currency": catalog.currency, "rows": catalog.inventory(store.list_orders())}

    @app.get("/api/merchant/fulfillment")
    async def fulfillment_board():
        return payments.fulfillment.board()

    @app.patch("/api/merchant/fulfillment/{task_id}")
    async def move_task(task_id: str, body: MoveIn):
        try:
            t = payments.fulfillment.move(task_id, body.startDate, body.endDate)
        except PlanError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        return {"ok": True, "task": t}

    @app.get("/api/merchant/market")
    async def market_view():
        return market_cache.get("last") or {"currency": catalog.currency, "source": market.source,
                                            "live": market.live, "rows": [], "pending": True}

    @app.post("/api/merchant/market/refresh")
    async def market_refresh():
        market_cache["last"] = await market.check()
        return market_cache["last"]

    # ----------------------------------------------------------- demo tools
    @app.post("/api/demo/forge-webhook")
    async def forge(body: ForgeIn):
        """Simulates an attacker POSTing a fake 'payment completed' webhook to our endpoint."""
        if not s.demo_tools:
            raise HTTPException(404)
        target = store.get_order(body.order_id) if body.order_id else next(
            (o for o in store.list_orders() if o["status"] in ("AWAITING_PAYMENT", "DRAFT")), None)
        oid = target["id"] if target else "TN-0000"
        total = target["total"] if target else "99.00"
        event = {"id": "WH-FORGED-" + uuid.uuid4().hex[:8].upper(), "event_type": "PAYMENT.CAPTURE.COMPLETED",
                 "resource": {"id": "FAKECAPTURE", "status": "COMPLETED", "custom_id": oid,
                              "amount": {"currency_code": "USD", "value": total}}}
        headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://evil.example/cert.pem",
                   "PAYPAL-TRANSMISSION-ID": str(uuid.uuid4()),
                   "PAYPAL-TRANSMISSION-TIME": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "PAYPAL-TRANSMISSION-SIG": "Zm9yZ2VkLXNpZ25hdHVyZQ==", "Content-Type": "application/json"}
        status, resp = await payments.handle_webhook(headers, json.dumps(event).encode())
        return {"target_order": oid, "http_status": status, "response": resp,
                "order_status_after": store.get_order(oid)["status"] if target else None}

    @app.on_event("shutdown")
    async def _close():
        await paypal.aclose()
        await market.aclose()

    return app


def _return_page(msg: str, order_id: str, ok: bool) -> str:
    color = "#1a7f37" if ok else "#9a3412"
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Tendero - payment</title><style>body{{margin:0;font:15px/1.5 system-ui,sans-serif;background:#f6f7f9;color:#1c2330;
display:grid;place-items:center;min-height:100vh}}.c{{background:#fff;padding:26px 28px;border-radius:12px;max-width:380px;
box-shadow:0 2px 10px #0001;text-align:center}}h1{{font-size:18px;color:{color};margin:0 0 8px}}</style></head>
<body><div class=c><h1>{'Payment approved' if ok else 'Payment not completed'}</h1><p>{msg}</p><p style="color:#5b6576">Order {html.escape(order_id)}</p></div>
<script>try{{parent.postMessage({{tendero:'checkout-done',ok:{str(ok).lower()},order:{json.dumps(order_id)}}},'*')}}catch(e){{}}
try{{window.opener&&window.opener.postMessage({{tendero:'checkout-done',ok:{str(ok).lower()}}},'*')}}catch(e){{}}</script></body></html>"""


app = None  # set by ``tendero.main`` for uvicorn
