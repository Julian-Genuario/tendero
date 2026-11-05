"""Payment lifecycle: link -> approval -> capture -> verified webhook -> reconciliation -> notify.

Order status machine::

    DRAFT --create_payment_link--> AWAITING_PAYMENT --buyer approves--> APPROVED
      --capture--> CAPTURED (money moved, waiting for the signed webhook)
      --verified PAYMENT.CAPTURE.COMPLETED + amounts match--> PAID
                                          + amounts differ --> MISMATCH (merchant review)
    PAID --refund--> REFUNDED            any --capture denied--> FAILED

Golden rule: an order only becomes PAID after a webhook whose signature PayPal itself
confirmed via /v1/notifications/verify-webhook-signature (or, if the webhook never arrives,
after the sweep re-reads the capture straight from the PayPal API). Nothing the customer,
the LLM, or an unsigned HTTP request says can mark an order as paid.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .catalog import Catalog, fmt, money
from .config import Settings
from .paypal_client import PayPalClient, PayPalError, approve_link, first_capture
from .reconcile import reconcile_capture
from .store import Store

log = logging.getLogger("tendero.payments")


class PaymentFlowError(RuntimeError):
    pass


class Payments:
    def __init__(self, settings: Settings, store: Store, paypal: PayPalClient, catalog: Catalog):
        self.s, self.store, self.paypal, self.catalog = settings, store, paypal, catalog

    # ------------------------------------------------------------ payment link
    async def create_payment_link(self, order_id: str) -> dict:
        order = self.store.get_order(order_id)
        if not order:
            raise PaymentFlowError(f"order {order_id} not found")
        if order["status"] == "AWAITING_PAYMENT" and order["approve_url"]:
            return order  # idempotent: re-send the same link
        if order["status"] != "DRAFT":
            raise PaymentFlowError(f"order {order_id} is {order['status']}, cannot create a new link")
        # Re-price from the catalog right before charging: never trust a stale/edited cart.
        cart = self.catalog.price_cart(order["cart"]["lines"], self.s.max_order_total)
        base = self.s.public_base_url.rstrip("/")
        pp = await self.paypal.create_order(
            reference_id=order_id,
            cart=cart,
            brand_name=self.s.merchant_name,
            return_url=f"{base}/paypal/return?order={order_id}",
            cancel_url=f"{base}/paypal/cancel?order={order_id}",
        )
        url = approve_link(pp)
        if not url:
            raise PaymentFlowError("PayPal did not return a payer-action link")
        self.store.add_event(order_id, "paypal.order.created",
                             f"Orders v2 {pp['id']} created for {cart['total']} {cart['currency']}")
        return self.store.update_order(order_id, status="AWAITING_PAYMENT", paypal_order_id=pp["id"],
                                       approve_url=url, cart=cart, total=cart["total"])

    # --------------------------------------------------------------- capture
    async def capture(self, order: dict, trigger: str) -> dict:
        if order["status"] in ("CAPTURED", "PAID", "REFUNDED", "MISMATCH"):
            return order
        try:
            pp = await self.paypal.capture_order(order["paypal_order_id"])
        except PayPalError as e:
            if isinstance(e.body, dict) and any(
                d.get("issue") == "ORDER_ALREADY_CAPTURED" for d in e.body.get("details", [])
            ):
                pp = await self.paypal.get_order(order["paypal_order_id"])
            else:
                self.store.add_event(order["id"], "paypal.capture.error", str(e), ok=False)
                raise
        cap = first_capture(pp)
        if not cap:
            raise PaymentFlowError("capture response had no capture object")
        self.store.add_event(order["id"], "paypal.capture",
                             f"capture {cap['id']} {cap['status']} via {trigger}; waiting for signed webhook")
        if cap["status"] in ("DECLINED", "FAILED"):
            self._notify_customer(order, "Your payment was declined by PayPal. You can try again with "
                                         "the same link or another funding source.")
            return self.store.update_order(order["id"], status="FAILED", capture_id=cap["id"])
        return self.store.update_order(order["id"], status="CAPTURED", capture_id=cap["id"])

    async def handle_return(self, order_id: str, paypal_token: str | None) -> dict:
        order = self.store.get_order(order_id)
        if not order or (paypal_token and paypal_token != order["paypal_order_id"]):
            raise PaymentFlowError("unknown order on return URL")
        self.store.add_event(order_id, "buyer.returned", "buyer approved the payment on PayPal")
        if order["status"] == "AWAITING_PAYMENT":
            order = self.store.update_order(order_id, status="APPROVED")
        return await self.capture(order, trigger="return_url")

    # --------------------------------------------------------------- webhooks
    async def handle_webhook(self, headers: dict[str, str], raw_body: bytes) -> tuple[int, dict]:
        try:
            event = json.loads(raw_body)
        except Exception:
            return 400, {"error": "invalid json"}
        etype, eid = event.get("event_type", "?"), event.get("id", "?")
        try:
            verified = await self.paypal.verify_webhook_signature(headers, event, self.s.paypal_webhook_id)
        except PayPalError as e:
            self.store.add_event(None, "webhook.verify.error", f"{etype} {eid}: {e}", ok=False)
            return 503, {"error": "verification unavailable, PayPal will retry"}
        if not verified:
            self.store.add_event(None, "webhook.rejected",
                                 f"{etype} {eid}: signature verification FAILED - ignored", ok=False)
            return 400, {"error": "signature verification failed"}
        if not self.store.record_webhook(eid, etype, True, raw_body.decode("utf-8", "replace")):
            self.store.add_event(None, "webhook.duplicate", f"{etype} {eid} already processed")
            return 200, {"status": "duplicate"}
        resource = event.get("resource") or {}
        order = self._order_for_resource(etype, resource)
        self.store.add_event(order["id"] if order else None, "webhook.verified",
                             f"{etype} {eid}: signature verified by PayPal (verify-webhook-signature)")
        if not order:
            self.store.add_event(None, "webhook.unmatched", f"{etype} {eid}: no matching order", ok=False)
            return 200, {"status": "unmatched"}
        if etype == "CHECKOUT.ORDER.APPROVED":
            if order["status"] == "AWAITING_PAYMENT":
                order = self.store.update_order(order["id"], status="APPROVED")
            await self.capture(order, trigger="webhook CHECKOUT.ORDER.APPROVED")
        elif etype == "PAYMENT.CAPTURE.COMPLETED":
            self._settle(order, resource, via="verified webhook")
        elif etype in ("PAYMENT.CAPTURE.DENIED", "PAYMENT.CAPTURE.DECLINED"):
            self.store.update_order(order["id"], status="FAILED")
            self._notify_customer(order, "PayPal declined the payment. Nothing was charged.")
        elif etype == "PAYMENT.CAPTURE.REFUNDED":
            self._refunded(order, resource)
        return 200, {"status": "processed", "order": order["id"]}

    def _order_for_resource(self, etype: str, resource: dict) -> dict | None:
        if etype.startswith("CHECKOUT.ORDER"):
            return self.store.order_by_paypal_id(resource.get("id", ""))
        if etype == "PAYMENT.CAPTURE.REFUNDED":
            for link in resource.get("links", []):
                if link.get("rel") == "up":
                    cap_id = link["href"].rstrip("/").split("/")[-1]
                    o = self.store.order_by_capture_id(cap_id)
                    if o:
                        return o
            cid = resource.get("custom_id") or resource.get("invoice_id")
            return self.store.get_order(cid) if cid else None
        cid = resource.get("custom_id") or resource.get("invoice_id")
        o = self.store.get_order(cid) if cid else None
        if not o:
            rel = (resource.get("supplementary_data") or {}).get("related_ids") or {}
            o = self.store.order_by_paypal_id(rel.get("order_id", "")) if rel.get("order_id") else None
        return o or self.store.order_by_capture_id(resource.get("id", ""))

    def _settle(self, order: dict, capture: dict, via: str) -> dict:
        if order["status"] in ("PAID", "REFUNDED"):
            return order
        rec = reconcile_capture(order, capture)
        if rec.ok:
            order = self.store.update_order(order["id"], status="PAID", capture_id=capture.get("id"),
                                            paypal_fee=rec.paypal_fee, net_amount=rec.net_amount,
                                            paid_via=via, paid_at=time.time())
            self.store.add_event(order["id"], "reconciled", f"PAID - {rec.summary()}")
            lines = ", ".join(f"{l['quantity']}x {l['name'].split(' (')[0]}" for l in order["cart"]["lines"])
            self._notify_customer(
                order,
                f"Payment confirmed by PayPal: {order['total']} {order['currency']} for order "
                f"{order['id']} ({lines}). Thank you! It ships within 2 business days.",
                card={"type": "paid", "order_id": order["id"], "total": order["total"],
                      "currency": order["currency"]},
            )
        else:
            order = self.store.update_order(order["id"], status="MISMATCH", capture_id=capture.get("id"),
                                            note=rec.summary(), paid_via=via)
            self.store.add_event(order["id"], "reconcile.mismatch", rec.summary(), ok=False)
            self._notify_customer(order, "We received a payment notice that does not match your order. "
                                         "The shop owner will review it personally before shipping.")
        return order

    def _refunded(self, order: dict, refund: dict) -> None:
        amt = (refund.get("amount") or {}).get("value")
        order = self.store.update_order(order["id"], status="REFUNDED", refund_id=refund.get("id"),
                                        refunded_amount=amt)
        self.store.add_event(order["id"], "refund.confirmed",
                             f"refund {refund.get('id')} of {amt} {order['currency']} confirmed by webhook")
        self._notify_customer(order, f"Your refund of {amt} {order['currency']} for order {order['id']} "
                                     f"was issued through PayPal. It can take a few days to show up.")

    # ----------------------------------------------------------------- refund
    async def refund(self, order_id: str, reason: str, amount: str | None = None) -> dict:
        order = self.store.get_order(order_id)
        if not order:
            raise PaymentFlowError(f"order {order_id} not found")
        if order["status"] != "PAID" or not order["capture_id"]:
            raise PaymentFlowError(f"order {order_id} is {order['status']}; only PAID orders can be refunded")
        if amount is not None and money(amount) > money(order["total"]):
            raise PaymentFlowError("refund amount exceeds the captured amount")
        res = await self.paypal.refund_capture(order["capture_id"], amount=fmt(money(amount)) if amount else None,
                                               currency=order["currency"], note=reason,
                                               request_id=f"refund-{order_id}")
        self.store.add_event(order_id, "paypal.refund",
                             f"refund {res.get('id')} {res.get('status')} requested: {reason}")
        return {"refund_id": res.get("id"), "status": res.get("status"),
                "amount": (res.get("amount") or {}).get("value", order["total"])}

    # ------------------------------------------------------------------ sweep
    async def sweep(self, force: bool = False) -> dict[str, Any]:
        """Safety net for lost webhooks / closed tabs. Reads state straight from PayPal."""
        now, fixed = time.time(), []
        for o in self.store.list_orders():
            stale = force or now - o["updated_at"] > self.s.sweep_after_seconds
            if not stale:
                continue
            try:
                if o["status"] == "CAPTURED" and o["capture_id"]:
                    cap = await self.paypal.get_capture(o["capture_id"])
                    if cap.get("status") == "COMPLETED":
                        self._settle(o, cap, via="API sweep (webhook missing)")
                        fixed.append(o["id"])
                elif o["status"] in ("AWAITING_PAYMENT", "APPROVED") and o["paypal_order_id"]:
                    pp = await self.paypal.get_order(o["paypal_order_id"])
                    if pp.get("status") == "APPROVED":
                        await self.capture(o, trigger="sweep")
                        fixed.append(o["id"])
            except PayPalError as e:
                self.store.add_event(o["id"], "sweep.error", str(e), ok=False)
        return {"checked_at": now, "advanced": fixed}

    # ------------------------------------------------------------------ utils
    def _notify_customer(self, order: dict, text: str, card: dict | None = None) -> None:
        self.store.add_message(order["conversation_id"], "agent", text, card=card)
        self.store.add_event(order["id"], "customer.notified", text[:140])
