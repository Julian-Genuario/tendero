"""Tools the agents can call. Every tool is executed by our server, never by the model.

Guardrails live here (not in the prompt), so they hold no matter which LLM is plugged in:
* prices and totals come only from the catalog (``build_cart``)
* a payment link can't be created in the same turn the cart was priced: the customer has
  to see the total and answer first
* per-order cap (MAX_ORDER_TOTAL); orders belong to the conversation that created them
* order status is read from our ledger, where PAID is only set by a verified webhook
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from ..catalog import CartError, fmt, money
from ..market import Market, MarketError
from ..payments import PaymentFlowError, Payments
from ..paypal_client import PayPalError


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required if required is not None else list(properties),
            "additionalProperties": False,
        },
    }


CUSTOMER_TOOLS = [
    _tool("search_catalog",
          "Search the shop's catalog. Returns SKUs, names, unit prices (USD) and stock. Use an empty "
          "query to list everything. Always use this instead of guessing what the shop sells.",
          {"query": {"type": "string", "description": "Free text, e.g. 'gourd' or ''"}}),
    _tool("build_cart",
          "Create or replace the customer's draft order with exact SKUs and quantities. The server "
          "prices it from the catalog (subtotal, shipping, total) and returns an order_id. Show the "
          "returned total to the customer and ask them to confirm before any payment link.",
          {"items": {"type": "array", "items": {
              "type": "object",
              "properties": {"sku": {"type": "string"}, "quantity": {"type": "integer"}},
              "required": ["sku", "quantity"], "additionalProperties": False}}}),
    _tool("create_payment_link",
          "Create a PayPal checkout (Orders v2, intent CAPTURE) for a draft order the customer has "
          "explicitly confirmed. Returns the approval link; the chat shows it as a pay button.",
          {"order_id": {"type": "string"}}),
    _tool("get_order_status",
          "Read an order's payment status from the shop ledger. PAID means PayPal sent a webhook "
          "whose signature PayPal verified and whose amount matched. Never tell a customer an "
          "order is paid unless this tool says PAID.",
          {"order_id": {"type": "string"}}),
    _tool("store_info", "Shipping, returns policy and opening hours of the shop.", {}),
]

ORDER_STATUSES = ["ANY", "DRAFT", "AWAITING_PAYMENT", "APPROVED", "CAPTURED", "PAID", "MISMATCH",
                  "REFUNDED", "FAILED"]

MERCHANT_TOOLS = [
    _tool("list_orders",
          "List orders from the ledger, newest first, optionally filtered by status.",
          {"status": {"type": "string", "enum": ORDER_STATUSES}}),
    _tool("reconciliation_report",
          "Run the PayPal reconciliation sweep and return the money picture: gross collected, PayPal "
          "fees, net, pending payments, mismatches needing review, refunds and rejected (forged) "
          "webhooks.", {}),
    _tool("refund_order",
          "Refund a PAID order through PayPal (Payments v2 refund). amount='' refunds in full.",
          {"order_id": {"type": "string"}, "reason": {"type": "string"},
           "amount": {"type": "string", "description": "e.g. '12.00', or '' for a full refund"}}),
    _tool("show_orders",
          "Filter and sort the owner's orders grid (AG Grid) on screen, and return the matching orders. "
          "Use it whenever the owner asks to see, show or find orders.",
          {"status": {"type": "string", "enum": ORDER_STATUSES},
           "min_total": {"type": "string", "description": "e.g. '50.00', or '' for no minimum"},
           "sort": {"type": "string", "enum": ["newest", "total_desc", "total_asc"]}}),
    _tool("fulfillment_plan",
          "Upcoming workshop work for paid orders (quality check, packing, courier pickup), as booked on "
          "the fulfillment board. Times are Buenos Aires time.", {}),
    _tool("market_price_check",
          "Compare catalog prices with comparable products on sale across the web (Channel3 product "
          "search): low, median and high market price per product. sku='' checks the whole catalog. "
          "Report only; never change prices.",
          {"sku": {"type": "string", "description": "a catalog SKU, or '' for all products"}}),
]


class ToolError(Exception):
    pass


class CustomerTools:
    definitions = CUSTOMER_TOOLS

    def __init__(self, payments: Payments, conversation_id: str, customer_name: str | None = None):
        self.p, self.cid, self.customer_name = payments, conversation_id, customer_name
        self.turn_started_at = time.time()
        self.cards: list[dict] = []

    async def run(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"t_{name}", None)
        if not fn:
            raise ToolError(f"unknown tool {name}")
        try:
            return await fn(**args)
        except (CartError, PaymentFlowError, PayPalError) as e:
            raise ToolError(str(e)) from e
        except TypeError as e:
            raise ToolError(f"bad arguments: {e}") from e

    def _own_order(self, order_id: str) -> dict:
        o = self.p.store.get_order(order_id.strip().upper())
        if not o or o["conversation_id"] != self.cid:
            raise ToolError(f"order {order_id} not found in this conversation")
        return o

    async def t_search_catalog(self, query: str = "") -> dict:
        return {"currency": self.p.catalog.currency, "products": self.p.catalog.search(query)}

    async def t_build_cart(self, items: list[dict]) -> dict:
        cart = self.p.catalog.price_cart(items, self.p.s.max_order_total)
        existing = self.p.store.latest_order_for(self.cid, ("DRAFT",))
        if existing:
            o = self.p.store.update_order(existing["id"], cart=cart, total=cart["total"])
            # treat a modified cart as new: the customer must see the new total
            self.p.store._exec("UPDATE orders SET created_at=? WHERE id=?", (time.time(), o["id"]))
        else:
            o = self.p.store.create_order(self.cid, self.customer_name, cart)
            self.p.store.add_event(o["id"], "order.draft", f"draft {cart['total']} {cart['currency']} built by agent")
        return {"order_id": o["id"], **cart, "next_step": "Show this total and ask the customer to confirm."}

    async def t_create_payment_link(self, order_id: str) -> dict:
        o = self._own_order(order_id)
        if o["status"] == "DRAFT" and o["created_at"] >= self.turn_started_at:
            raise ToolError("The customer has not seen this total yet. Show the total from build_cart "
                            "and wait for their explicit confirmation before creating a payment link.")
        o = await self.p.create_payment_link(o["id"])
        card = {"type": "pay", "order_id": o["id"], "url": o["approve_url"], "total": o["total"],
                "currency": o["currency"], "paypal_order_id": o["paypal_order_id"]}
        self.cards.append(card)
        return {"order_id": o["id"], "paypal_order_id": o["paypal_order_id"], "approve_url": o["approve_url"],
                "total": o["total"], "currency": o["currency"],
                "note": "The chat already shows a 'Pay with PayPal' button. Payment is confirmed "
                        "automatically when PayPal's signed webhook arrives."}

    async def t_get_order_status(self, order_id: str) -> dict:
        o = self._own_order(order_id)
        meaning = {
            "DRAFT": "not confirmed yet, no payment link",
            "AWAITING_PAYMENT": "payment link sent, customer has not paid yet",
            "APPROVED": "customer approved in PayPal, capturing now",
            "CAPTURED": "PayPal captured the money, waiting for the signed webhook to confirm",
            "PAID": "paid and verified (signed PayPal webhook, amount matched)",
            "MISMATCH": "a payment arrived but does not match the order; the owner will review",
            "REFUNDED": "refunded through PayPal",
            "FAILED": "PayPal declined the payment",
        }
        return {"order_id": o["id"], "status": o["status"], "meaning": meaning.get(o["status"], ""),
                "total": o["total"], "currency": o["currency"]}

    async def t_store_info(self) -> dict:
        st = self.p.catalog.store
        return {k: st[k] for k in ("name", "tagline", "shipping_policy", "returns_policy", "hours")}


class MerchantTools:
    definitions = MERCHANT_TOOLS

    def __init__(self, payments: Payments, market: Market | None = None):
        self.p, self.market = payments, market
        self.cards: list[dict] = []
        self.last_market: dict | None = None

    async def run(self, name: str, args: dict) -> dict:
        fn = getattr(self, f"t_{name}", None)
        if not fn:
            raise ToolError(f"unknown tool {name}")
        try:
            return await fn(**args)
        except (PaymentFlowError, PayPalError, MarketError) as e:
            raise ToolError(str(e)) from e
        except TypeError as e:
            raise ToolError(f"bad arguments: {e}") from e

    async def t_list_orders(self, status: str = "ANY") -> dict:
        rows = self.p.store.list_orders(None if status in ("", "ANY") else status)
        return {"orders": [
            {"order_id": o["id"], "status": o["status"], "total": o["total"], "currency": o["currency"],
             "customer": o["customer_name"], "items": [f"{l['quantity']}x {l['sku']}" for l in o["cart"]["lines"]],
             "paypal_fee": o["paypal_fee"], "net": o["net_amount"], "note": o["note"]}
            for o in rows[:25]]}

    async def t_reconciliation_report(self) -> dict:
        sweep = await self.p.sweep()
        orders = self.p.store.list_orders()
        z = Decimal("0")
        paid = [o for o in orders if o["status"] == "PAID"]
        refunded = [o for o in orders if o["status"] == "REFUNDED"]
        pending = [o for o in orders if o["status"] in ("AWAITING_PAYMENT", "APPROVED", "CAPTURED")]
        mism = [o for o in orders if o["status"] == "MISMATCH"]
        settled = paid + refunded
        gross = sum((money(o["total"]) for o in paid), z)
        fees = sum((money(o["paypal_fee"] or 0) for o in settled), z)
        net = sum((money(o["net_amount"] or 0) for o in paid), z)
        rejected = [e for e in self.p.store.events() if e["kind"] == "webhook.rejected"]
        top: dict[str, int] = {}
        for o in paid:
            for l in o["cart"]["lines"]:
                top[l["name"].split(" (")[0]] = top.get(l["name"].split(" (")[0], 0) + l["quantity"]
        return {
            "currency": self.p.catalog.currency,
            "paid_orders": len(paid), "gross_collected": fmt(gross), "paypal_fees": fmt(fees),
            "net_collected": fmt(net),
            "pending_orders": [{"order_id": o["id"], "status": o["status"], "total": o["total"]} for o in pending],
            "mismatches": [{"order_id": o["id"], "issue": o["note"]} for o in mism],
            "refunded_orders": [{"order_id": o["id"], "amount": o["refunded_amount"]} for o in refunded],
            "rejected_webhooks": len(rejected),
            "units_sold": top,
            "sweep_advanced": sweep["advanced"],
        }

    async def t_refund_order(self, order_id: str, reason: str, amount: str = "") -> dict:
        res = await self.p.refund(order_id.strip().upper(), reason or "Refund", amount or None)
        return {"order_id": order_id, **res,
                "note": "Refund requested at PayPal; the order flips to REFUNDED when the signed "
                        "PAYMENT.CAPTURE.REFUNDED webhook is verified."}

    async def t_show_orders(self, status: str = "ANY", min_total: str = "", sort: str = "newest") -> dict:
        if status not in ORDER_STATUSES:
            raise ToolError(f"unknown status {status}")
        if sort not in ("newest", "total_desc", "total_asc"):
            raise ToolError(f"unknown sort {sort}")
        try:
            floor = money(min_total) if min_total else None
        except Exception as e:
            raise ToolError(f"min_total must be an amount like '50.00', got {min_total!r}") from e
        rows = self.p.store.list_orders(None if status == "ANY" else status)
        if floor is not None:
            rows = [o for o in rows if money(o["total"]) >= floor]
        if sort != "newest":
            rows.sort(key=lambda o: money(o["total"]), reverse=sort == "total_desc")
        view = {"type": "grid_view", "status": status, "min_total": fmt(floor) if floor is not None else "",
                "sort": sort, "count": len(rows)}
        self.cards.append(view)
        return {"shown_on_screen": True, "matching": len(rows),
                "orders": [{"order_id": o["id"], "status": o["status"], "total": o["total"],
                            "customer": o["customer_name"]} for o in rows[:25]]}

    async def t_fulfillment_plan(self) -> dict:
        self.cards.append({"type": "board"})
        return {"timezone": "Buenos Aires", "orders": self.p.fulfillment.upcoming()}

    async def t_market_price_check(self, sku: str = "") -> dict:
        if not self.market:
            raise ToolError("market price check is not configured")
        res = await self.market.check(sku.strip())
        self.last_market = res
        self.cards.append({"type": "market", "live": res["live"]})
        return res
