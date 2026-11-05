"""Check a real PayPal *sandbox* app end to end, from the command line.

    python scripts/sandbox_check.py                 # token + create order + print approval link
    python scripts/sandbox_check.py --capture ID    # capture after approving with a sandbox buyer
    python scripts/sandbox_check.py --register-webhook https://<public-url>
                                                    # creates the webhook, prints PAYPAL_WEBHOOK_ID

Reads PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET from the environment or .env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tendero.catalog import Catalog  # noqa: E402
from tendero.config import load_settings  # noqa: E402
from tendero.paypal_client import PayPalClient, approve_link, first_capture  # noqa: E402

EVENTS = ["CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED",
          "PAYMENT.CAPTURE.DECLINED", "PAYMENT.CAPTURE.REFUNDED"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capture", metavar="PAYPAL_ORDER_ID")
    ap.add_argument("--register-webhook", metavar="PUBLIC_BASE_URL")
    a = ap.parse_args()
    os.environ["PAYPAL_MODE"] = "sandbox"
    s = load_settings()
    if not (s.paypal_client_id and s.paypal_client_secret):
        sys.exit("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (sandbox app) first.")
    pp = PayPalClient(s.resolved_paypal_base, s.paypal_client_id, s.paypal_client_secret)
    try:
        await pp.token()
        print("OK  OAuth token from", s.resolved_paypal_base)
        if a.register_webhook:
            url = a.register_webhook.rstrip("/") + "/webhooks/paypal"
            r = await pp._call("POST", "/v1/notifications/webhooks", "create_webhook",
                               {"url": url, "event_types": [{"name": e} for e in EVENTS]}, prefer_full=False)
            print("OK  webhook created for", url)
            print(f"\n    PAYPAL_WEBHOOK_ID={r['id']}\n")
            return
        if a.capture:
            o = await pp.capture_order(a.capture)
            cap = first_capture(o)
            print("OK  captured:", json.dumps({k: cap.get(k) for k in ("id", "status", "amount")}, indent=2))
            print("    fee/net:", json.dumps(cap.get("seller_receivable_breakdown", {}), indent=2))
            return
        cart = Catalog.load().price_cart([{"sku": "MATE-CAL", "quantity": 1}, {"sku": "BOMB-ALP", "quantity": 1}])
        o = await pp.create_order(reference_id="TN-CHECK-" + os.urandom(3).hex().upper(), cart=cart,
                                  brand_name="Taller Ñandú (sandbox check)",
                                  return_url="https://example.com/return?order=check",
                                  cancel_url="https://example.com/cancel?order=check")
        print("OK  Orders v2 created:", o["id"], o["status"], cart["total"], cart["currency"])
        print("\n    1) Open this link and pay with your *sandbox personal* account:")
        print("      ", approve_link(o))
        print("    2) Then run:  python scripts/sandbox_check.py --capture", o["id"])
    finally:
        await pp.aclose()


if __name__ == "__main__":
    asyncio.run(main())
