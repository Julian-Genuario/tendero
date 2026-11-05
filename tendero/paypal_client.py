"""Thin async client for the PayPal REST APIs Tendero uses.

* OAuth 2.0 client-credentials token (cached until shortly before expiry)
* Orders v2: create / get / capture
* Payments v2: get capture / refund capture
* Notifications: verify-webhook-signature

The same client talks to the real sandbox (https://api-m.sandbox.paypal.com) or to the
local simulator in ``mock_paypal`` - only ``base_url`` changes.
"""
from __future__ import annotations

import time
from typing import Any

import httpx


class PayPalError(RuntimeError):
    def __init__(self, status: int, body: Any, op: str):
        self.status, self.body, self.op = status, body, op
        debug_id = body.get("debug_id") if isinstance(body, dict) else None
        name = body.get("name") if isinstance(body, dict) else None
        super().__init__(f"PayPal {op} failed: HTTP {status} {name or ''} debug_id={debug_id}")


WEBHOOK_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}


class PayPalClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._cid, self._secret = client_id, client_secret
        self._token: str | None = None
        self._token_exp = 0.0
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------- auth
    async def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = await self._http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(self._cid, self._secret),
            headers={"Accept": "application/json"},
        )
        body = _json(r)
        if r.status_code != 200:
            raise PayPalError(r.status_code, body, "oauth2/token")
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    async def _call(self, method: str, path: str, op: str, json: Any = None,
                    request_id: str | None = None, prefer_full: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {await self.token()}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id  # idempotency key
        if prefer_full:
            headers["Prefer"] = "return=representation"
        r = await self._http.request(method, path, json=json, headers=headers)
        body = _json(r)
        if r.status_code >= 400:
            raise PayPalError(r.status_code, body, op)
        return body

    # ----------------------------------------------------------------- orders
    async def create_order(self, *, reference_id: str, cart: dict, brand_name: str,
                           return_url: str, cancel_url: str) -> dict:
        cur = cart["currency"]
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": reference_id,
                    "custom_id": reference_id,
                    "invoice_id": reference_id,
                    "description": f"{brand_name} order {reference_id}",
                    "amount": {
                        "currency_code": cur,
                        "value": cart["total"],
                        "breakdown": {
                            "item_total": {"currency_code": cur, "value": cart["subtotal"]},
                            "shipping": {"currency_code": cur, "value": cart["shipping"]},
                        },
                    },
                    "items": [
                        {
                            "name": line["name"][:127],
                            "sku": line["sku"],
                            "quantity": str(line["quantity"]),
                            "unit_amount": {"currency_code": cur, "value": line["unit_price"]},
                            "category": "PHYSICAL_GOODS",
                        }
                        for line in cart["lines"]
                    ],
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "brand_name": brand_name[:127],
                        "user_action": "PAY_NOW",
                        "shipping_preference": "GET_FROM_FILE",
                        "return_url": return_url,
                        "cancel_url": cancel_url,
                    }
                }
            },
        }
        return await self._call("POST", "/v2/checkout/orders", "create_order", payload,
                                request_id=f"create-{reference_id}")

    async def get_order(self, order_id: str) -> dict:
        return await self._call("GET", f"/v2/checkout/orders/{order_id}", "get_order")

    async def capture_order(self, order_id: str) -> dict:
        return await self._call("POST", f"/v2/checkout/orders/{order_id}/capture", "capture_order",
                                {}, request_id=f"capture-{order_id}")

    # --------------------------------------------------------------- payments
    async def get_capture(self, capture_id: str) -> dict:
        return await self._call("GET", f"/v2/payments/captures/{capture_id}", "get_capture")

    async def refund_capture(self, capture_id: str, *, amount: str | None, currency: str,
                             note: str, request_id: str) -> dict:
        body: dict[str, Any] = {"note_to_payer": note[:255]}
        if amount:
            body["amount"] = {"value": amount, "currency_code": currency}
        return await self._call("POST", f"/v2/payments/captures/{capture_id}/refund",
                                "refund_capture", body, request_id=request_id)

    # --------------------------------------------------------------- webhooks
    async def verify_webhook_signature(self, headers: dict[str, str], event: dict,
                                       webhook_id: str) -> bool:
        """POST /v1/notifications/verify-webhook-signature. True only for SUCCESS."""
        lower = {k.lower(): v for k, v in headers.items()}
        payload = {k: lower.get(h, "") for k, h in WEBHOOK_HEADERS.items()}
        if not all(payload.values()):
            return False  # missing PayPal transmission headers: never even ask
        payload["webhook_id"] = webhook_id
        payload["webhook_event"] = event
        body = await self._call("POST", "/v1/notifications/verify-webhook-signature",
                                "verify_webhook_signature", payload, prefer_full=False)
        return body.get("verification_status") == "SUCCESS"


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:500]}


def approve_link(order: dict) -> str | None:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link["href"]
    return None


def first_capture(order: dict) -> dict | None:
    for pu in order.get("purchase_units", []):
        caps = (pu.get("payments") or {}).get("captures") or []
        if caps:
            return caps[0]
    return None
