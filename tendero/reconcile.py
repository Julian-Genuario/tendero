"""Pure reconciliation logic: does a PayPal capture really settle *this* order?"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class Reconciliation:
    ok: bool
    issues: list[str] = field(default_factory=list)
    paypal_fee: str | None = None
    net_amount: str | None = None
    gross: str | None = None

    def summary(self) -> str:
        if self.ok:
            fee = f", PayPal fee {self.paypal_fee}, net {self.net_amount}" if self.paypal_fee else ""
            return f"amount {self.gross} matches order total{fee}"
        return "; ".join(self.issues)


def reconcile_capture(order: dict, capture: dict) -> Reconciliation:
    issues: list[str] = []
    amount = capture.get("amount") or {}
    gross = amount.get("value")
    if capture.get("status") != "COMPLETED":
        issues.append(f"capture status is {capture.get('status')!r}, expected 'COMPLETED'")
    if amount.get("currency_code") != order["currency"]:
        issues.append(f"currency {amount.get('currency_code')} != order currency {order['currency']}")
    try:
        if Decimal(str(gross)) != Decimal(order["total"]):
            issues.append(f"captured {gross} but order total is {order['total']}")
    except Exception:
        issues.append(f"capture amount {gross!r} is not a number")
    custom_id = capture.get("custom_id") or capture.get("invoice_id")
    if custom_id and custom_id != order["id"]:
        issues.append(f"capture belongs to {custom_id}, not {order['id']}")
    srb = capture.get("seller_receivable_breakdown") or {}
    fee = (srb.get("paypal_fee") or {}).get("value")
    net = (srb.get("net_amount") or {}).get("value")
    return Reconciliation(ok=not issues, issues=issues, paypal_fee=fee, net_amount=net, gross=gross)
