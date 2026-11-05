"""Local PayPal sandbox *simulator*.

It is NOT PayPal. It implements the subset of the PayPal REST API that Tendero calls, with
the same request/response shapes, status codes and error names, so the whole flow can be
demoed and tested offline before sandbox credentials exist:

* POST /v1/oauth2/token
* POST /v2/checkout/orders, GET /v2/checkout/orders/{id}, POST /v2/checkout/orders/{id}/capture
* GET /v2/payments/captures/{id}, POST /v2/payments/captures/{id}/refund
* POST /v1/notifications/verify-webhook-signature
* GET /checkoutnow?token=...  (a plain buyer-approval page, clearly labelled as a simulator)

Webhooks are signed with HMAC-SHA256 over ``transmission_id|transmission_time|webhook_id|crc32``
(PayPal uses the same input string with an RSA certificate). Anything not signed by this
simulator fails verification, exactly like a forged webhook fails against real PayPal.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import secrets
import string
import time
import uuid
import zlib
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SIGNING_KEY = b"tendero-mock-paypal-signing-key"
CERT_URL = "https://mock-paypal.local/certs/mock-cert.pem"
FEE_PCT, FEE_FIXED = Decimal("0.0349"), Decimal("0.49")

Deliver = Callable[[str, dict[str, str], bytes], Awaitable[Any]]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid(n: int = 17) -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def canonical(event: dict) -> bytes:
    return json.dumps(event, separators=(",", ":"), sort_keys=True).encode()


def sign(transmission_id: str, ts: str, webhook_id: str, event: dict) -> str:
    msg = f"{transmission_id}|{ts}|{webhook_id}|{zlib.crc32(canonical(event))}".encode()
    return base64.b64encode(hmac.new(SIGNING_KEY, msg, hashlib.sha256).digest()).decode()


def _err(status: int, name: str, message: str, issue: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"name": name, "message": message, "debug_id": uuid.uuid4().hex[:13]}
    if issue:
        body["details"] = [{"issue": issue, "description": message}]
    return JSONResponse(body, status_code=status)


class MockPayPal:
    def __init__(self, *, public_base: str, webhook_url: str, webhook_id: str,
                 client_id: str = "mock-client-id", client_secret: str = "mock-client-secret",
                 webhook_delay: float = 0.8, deliver: Deliver | None = None,
                 approved_fallback_wait: float | None = 5.0):
        self.public_base = public_base.rstrip("/")
        self.webhook_url, self.webhook_id = webhook_url, webhook_id
        self.client_id, self.client_secret = client_id, client_secret
        self.webhook_delay = webhook_delay
        self.deliver = deliver
        self.approved_fallback_wait = approved_fallback_wait
        self.tokens: set[str] = set()
        self.orders: dict[str, dict] = {}
        self.captures: dict[str, dict] = {}
        self.idem: dict[str, Any] = {}
        self.sent: list[dict] = []
        self._tasks: set[asyncio.Task] = set()
        self.app = self._build()

    # --------------------------------------------------------------- webhooks
    def _schedule(self, event_type: str, resource: dict, delay: float | None = None) -> None:
        event = {
            "id": "WH-" + _pid(20),
            "event_version": "1.0",
            "create_time": _now(),
            "resource_type": "capture" if "CAPTURE" in event_type else "checkout-order",
            "event_type": event_type,
            "summary": event_type.replace(".", " ").title(),
            "resource": json.loads(json.dumps(resource)),
        }
        task = asyncio.get_running_loop().create_task(self._send(event, self.webhook_delay if delay is None else delay))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, event: dict, delay: float) -> None:
        await asyncio.sleep(delay)
        tid, ts = str(uuid.uuid4()), _now()
        headers = {
            "Content-Type": "application/json",
            "PAYPAL-AUTH-ALGO": "SHA256withRSA",
            "PAYPAL-CERT-URL": CERT_URL,
            "PAYPAL-TRANSMISSION-ID": tid,
            "PAYPAL-TRANSMISSION-TIME": ts,
            "PAYPAL-TRANSMISSION-SIG": sign(tid, ts, self.webhook_id, event),
        }
        self.sent.append(event)
        body = json.dumps(event).encode()
        if self.deliver:
            await self.deliver(self.webhook_url, headers, body)
        else:  # pragma: no cover - real HTTP delivery when running the server
            import httpx

            async with httpx.AsyncClient(timeout=15) as c:
                try:
                    await c.post(self.webhook_url, content=body, headers=headers)
                except Exception:
                    pass

    async def drain(self) -> None:
        """Test helper: wait for all scheduled webhook deliveries."""
        while True:
            pending = [t for t in self._tasks if not t.done()]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)

    # ------------------------------------------------------------------ build
    def _build(self) -> FastAPI:
        app = FastAPI(title="Local PayPal sandbox simulator", docs_url=None, redoc_url=None)
        m = self

        def authed(req: Request) -> bool:
            auth = req.headers.get("authorization", "")
            return auth.startswith("Bearer ") and auth[7:] in m.tokens

        @app.post("/v1/oauth2/token")
        async def token(req: Request):
            auth = req.headers.get("authorization", "")
            try:
                cid, secret = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)
            except Exception:
                return _err(401, "invalid_client", "Client Authentication failed")
            if (cid, secret) != (m.client_id, m.client_secret):
                return JSONResponse({"error": "invalid_client",
                                     "error_description": "Client Authentication failed"}, 401)
            tok = "A21AA" + secrets.token_urlsafe(24)
            m.tokens.add(tok)
            return {"scope": "https://uri.paypal.com/services/payments/payment", "access_token": tok,
                    "token_type": "Bearer", "app_id": "APP-MOCK", "expires_in": 32400,
                    "nonce": _now() + secrets.token_hex(8)}

        @app.post("/v2/checkout/orders")
        async def create_order(req: Request):
            if not authed(req):
                return _err(401, "AUTHENTICATION_FAILURE", "Authentication failed due to invalid authentication credentials.")
            rid = req.headers.get("paypal-request-id")
            if rid and rid in m.idem:
                return JSONResponse(m.idem[rid], 200)
            body = await req.json()
            pu = (body.get("purchase_units") or [{}])[0]
            amt = pu.get("amount") or {}
            items = pu.get("items") or []
            if body.get("intent") != "CAPTURE" or not amt.get("value"):
                return _err(422, "UNPROCESSABLE_ENTITY", "The requested action could not be performed.", "INVALID_PARAMETER_VALUE")
            item_total = sum(Decimal(i["unit_amount"]["value"]) * int(i["quantity"]) for i in items)
            bd = amt.get("breakdown") or {}
            if items and Decimal(bd.get("item_total", {}).get("value", "0")) != item_total:
                return _err(422, "UNPROCESSABLE_ENTITY", "Item total mismatch", "ITEM_TOTAL_MISMATCH")
            exp = sum(Decimal(v["value"]) for v in bd.values()) if bd else Decimal(amt["value"])
            if exp != Decimal(amt["value"]):
                return _err(422, "UNPROCESSABLE_ENTITY", "Amount mismatch", "AMOUNT_MISMATCH")
            oid = _pid()
            ctx = ((body.get("payment_source") or {}).get("paypal") or {}).get("experience_context") or {}
            order = {
                "id": oid, "intent": "CAPTURE", "status": "PAYER_ACTION_REQUIRED",
                "create_time": _now(), "purchase_units": [pu],
                "payment_source": {"paypal": {}},
                "_ctx": ctx,
                "links": [
                    {"href": f"{m.public_base}/__mock_paypal/v2/checkout/orders/{oid}", "rel": "self", "method": "GET"},
                    {"href": f"{m.public_base}/__mock_paypal/checkoutnow?token={oid}", "rel": "payer-action", "method": "GET"},
                ],
            }
            m.orders[oid] = order
            out = m._public(order)
            if rid:
                m.idem[rid] = out
            return JSONResponse(out, 200)

        @app.get("/v2/checkout/orders/{oid}")
        async def get_order(oid: str, req: Request):
            if not authed(req):
                return _err(401, "AUTHENTICATION_FAILURE", "Authentication failed.")
            if oid not in m.orders:
                return _err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.", "INVALID_RESOURCE_ID")
            return m._public(m.orders[oid])

        @app.post("/v2/checkout/orders/{oid}/capture")
        async def capture(oid: str, req: Request):
            if not authed(req):
                return _err(401, "AUTHENTICATION_FAILURE", "Authentication failed.")
            o = m.orders.get(oid)
            if not o:
                return _err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.", "INVALID_RESOURCE_ID")
            if o["status"] == "COMPLETED":
                return _err(422, "UNPROCESSABLE_ENTITY", "Order already captured.", "ORDER_ALREADY_CAPTURED")
            if o["status"] != "APPROVED":
                return _err(422, "UNPROCESSABLE_ENTITY", "Payer has not yet approved the Order for payment.", "ORDER_NOT_APPROVED")
            pu = o["purchase_units"][0]
            gross = Decimal(pu["amount"]["value"])
            fee = (gross * FEE_PCT + FEE_FIXED).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            cur = pu["amount"]["currency_code"]
            cap_id = _pid()
            cap = {
                "id": cap_id, "status": "COMPLETED",
                "amount": {"currency_code": cur, "value": str(gross)},
                "final_capture": True,
                "seller_protection": {"status": "ELIGIBLE"},
                "seller_receivable_breakdown": {
                    "gross_amount": {"currency_code": cur, "value": str(gross)},
                    "paypal_fee": {"currency_code": cur, "value": str(fee)},
                    "net_amount": {"currency_code": cur, "value": str(gross - fee)},
                },
                "custom_id": pu.get("custom_id"), "invoice_id": pu.get("invoice_id"),
                "create_time": _now(), "update_time": _now(),
                "supplementary_data": {"related_ids": {"order_id": oid}},
                "links": [
                    {"href": f"{m.public_base}/__mock_paypal/v2/payments/captures/{cap_id}", "rel": "self", "method": "GET"},
                    {"href": f"{m.public_base}/__mock_paypal/v2/payments/captures/{cap_id}/refund", "rel": "refund", "method": "POST"},
                ],
            }
            m.captures[cap_id] = cap
            o["status"] = "COMPLETED"
            pu["payments"] = {"captures": [cap]}
            m._schedule("PAYMENT.CAPTURE.COMPLETED", cap)
            return JSONResponse(m._public(o), 201)

        @app.get("/v2/payments/captures/{cid}")
        async def get_capture(cid: str, req: Request):
            if not authed(req):
                return _err(401, "AUTHENTICATION_FAILURE", "Authentication failed.")
            if cid not in m.captures:
                return _err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.", "INVALID_RESOURCE_ID")
            return m.captures[cid]

        @app.post("/v2/payments/captures/{cid}/refund")
        async def refund(cid: str, req: Request):
            if not authed(req):
                return _err(401, "AUTHENTICATION_FAILURE", "Authentication failed.")
            cap = m.captures.get(cid)
            if not cap:
                return _err(404, "RESOURCE_NOT_FOUND", "The specified resource does not exist.", "INVALID_RESOURCE_ID")
            rid = req.headers.get("paypal-request-id")
            if rid and rid in m.idem:
                return JSONResponse(m.idem[rid], 201)
            if cap["status"] == "REFUNDED":
                return _err(422, "UNPROCESSABLE_ENTITY", "Capture has already been fully refunded", "CAPTURE_FULLY_REFUNDED")
            body = await req.json() if (await req.body()) else {}
            amount = (body.get("amount") or cap["amount"])
            ref = {
                "id": _pid(), "status": "COMPLETED",
                "amount": {"currency_code": amount["currency_code"], "value": amount["value"]},
                "note_to_payer": body.get("note_to_payer"),
                "custom_id": cap.get("custom_id"), "invoice_id": cap.get("invoice_id"),
                "create_time": _now(),
                "links": [{"href": f"{m.public_base}/__mock_paypal/v2/payments/captures/{cid}", "rel": "up", "method": "GET"}],
            }
            cap["status"] = "REFUNDED" if Decimal(amount["value"]) >= Decimal(cap["amount"]["value"]) else "PARTIALLY_REFUNDED"
            if rid:
                m.idem[rid] = ref
            m._schedule("PAYMENT.CAPTURE.REFUNDED", ref)
            return JSONResponse(ref, 201)

        @app.post("/v1/notifications/verify-webhook-signature")
        async def verify(req: Request):
            if not authed(req):
                return _err(401, "AUTHENTICATION_FAILURE", "Authentication failed.")
            b = await req.json()
            required = ["auth_algo", "cert_url", "transmission_id", "transmission_sig",
                        "transmission_time", "webhook_id", "webhook_event"]
            if any(not b.get(k) for k in required):
                return _err(400, "VALIDATION_ERROR", "Invalid request - see details", "MISSING_REQUIRED_PARAMETER")
            ok = (
                b["webhook_id"] == m.webhook_id
                and b["cert_url"] == CERT_URL
                and hmac.compare_digest(
                    sign(b["transmission_id"], b["transmission_time"], b["webhook_id"], b["webhook_event"]),
                    b["transmission_sig"],
                )
            )
            return {"verification_status": "SUCCESS" if ok else "FAILURE"}

        @app.get("/checkoutnow", response_class=HTMLResponse)
        async def checkout(token: str):
            o = m.orders.get(token)
            if not o:
                return HTMLResponse("<h1>Unknown order</h1>", 404)
            return HTMLResponse(m._checkout_html(o))

        @app.post("/checkoutnow/{oid}/{action}")
        async def checkout_action(oid: str, action: str):
            o = m.orders.get(oid)
            if not o:
                return HTMLResponse("unknown order", 404)
            ctx = o.get("_ctx") or {}
            if action == "approve" and o["status"] in ("PAYER_ACTION_REQUIRED", "CREATED"):
                o["status"] = "APPROVED"
                o["payer"] = {"name": {"given_name": "Sandbox", "surname": "Buyer"},
                              "email_address": "buyer@example.com", "payer_id": "MOCKPAYER01"}
                # Real PayPal also emits CHECKOUT.ORDER.APPROVED. We send it only if the merchant
                # has not captured within a few seconds (e.g. the buyer closed the tab).
                if m.approved_fallback_wait is not None:
                    t = asyncio.get_running_loop().create_task(m._approved_fallback(oid, m.approved_fallback_wait))
                    m._tasks.add(t)
                    t.add_done_callback(m._tasks.discard)
                return RedirectResponse(f"{ctx.get('return_url')}&token={oid}&PayerID=MOCKPAYER01", 303)
            return RedirectResponse(f"{ctx.get('cancel_url')}&token={oid}", 303)

        return app

    async def _approved_fallback(self, oid: str, wait: float = 5.0) -> None:
        await asyncio.sleep(wait)
        o = self.orders.get(oid)
        if o and o["status"] == "APPROVED":
            self._schedule("CHECKOUT.ORDER.APPROVED", self._public(o), delay=0)

    def _public(self, o: dict) -> dict:
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def _checkout_html(self, o: dict) -> str:
        pu = o["purchase_units"][0]
        rows = "".join(
            f"<tr><td>{int(i['quantity'])} &times; {html.escape(i['name'])}</td>"
            f"<td class=r>{i['unit_amount']['value']}</td></tr>" for i in pu.get("items", [])
        )
        bd = (pu["amount"].get("breakdown") or {})
        ship = bd.get("shipping", {}).get("value", "0.00")
        brand = html.escape((o.get("_ctx") or {}).get("brand_name", "Merchant"))
        cur = pu["amount"]["currency_code"]
        return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Sandbox simulator - approve payment</title>
<style>
body{{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#eef1f5;color:#1c2330}}
.bar{{background:#2b3445;color:#fff;padding:10px 18px;font-size:13px;letter-spacing:.02em}}
.bar b{{color:#ffd166}}
.card{{max-width:420px;margin:28px auto;background:#fff;border-radius:12px;padding:22px 24px;box-shadow:0 2px 10px #0001}}
h1{{font-size:18px;margin:0 0 4px}} .muted{{color:#5b6576;font-size:13px}}
table{{width:100%;border-collapse:collapse;margin:14px 0}} td{{padding:6px 0;border-bottom:1px solid #eef0f3}}
.r{{text-align:right;white-space:nowrap;padding-left:12px}} .tot td{{font-weight:700;border:0;padding-top:10px}}
button{{width:100%;padding:12px;border-radius:24px;border:0;font-size:15px;font-weight:600;cursor:pointer;margin-top:8px}}
.pay{{background:#1f6feb;color:#fff}} .cancel{{background:#fff;color:#5b6576;border:1px solid #d5dae1}}
</style></head><body>
<div class=bar><b>LOCAL SANDBOX SIMULATOR</b> &middot; not PayPal &middot; no real money &middot; same API shapes as the PayPal sandbox</div>
<div class=card>
<h1>Approve payment to {brand}</h1>
<div class=muted>Logged in as sandbox buyer &middot; buyer@example.com</div>
<table>{rows}<tr><td>Shipping</td><td class=r>{ship}</td></tr>
<tr class=tot><td>Total</td><td class=r>{pu['amount']['value']} {cur}</td></tr></table>
<form method=post action="/__mock_paypal/checkoutnow/{o['id']}/approve"><button id=approve class=pay>Approve &amp; pay {pu['amount']['value']} {cur}</button></form>
<form method=post action="/__mock_paypal/checkoutnow/{o['id']}/cancel"><button class=cancel>Cancel and return</button></form>
<p class=muted>Order {o['id']} &middot; intent CAPTURE</p>
</div></body></html>"""
