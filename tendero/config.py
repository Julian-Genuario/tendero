"""Runtime configuration, read from environment variables (and an optional .env file).

Two independent switches decide how Tendero runs:

* ``PAYPAL_MODE``  - ``mock`` (default): a local PayPal sandbox simulator mounted at
  ``/__mock_paypal`` that speaks the same REST shapes as PayPal (Orders v2, Payments v2,
  webhook signature verification). ``sandbox``: the real PayPal sandbox
  (https://api-m.sandbox.paypal.com) using ``PAYPAL_CLIENT_ID`` / ``PAYPAL_CLIENT_SECRET``.
* ``LLM_PROVIDER`` - ``auto`` (default): Claude if ``ANTHROPIC_API_KEY`` is set, otherwise
  the offline scripted demo brain. ``anthropic`` or ``scripted`` force one of them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

try:  # optional convenience; the app works without python-dotenv
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    public_base_url: str = "http://localhost:8000"
    db_path: str = str(ROOT / "data" / "tendero.sqlite3")

    paypal_mode: str = "mock"  # mock | sandbox
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_webhook_id: str = ""
    paypal_base_url: str = ""  # derived when empty

    llm_provider: str = "auto"  # auto | anthropic | scripted
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_effort: str = "low"
    anthropic_fallbacks: bool = True

    currency: str = "USD"
    max_order_total: Decimal = Decimal("500.00")
    mock_webhook_delay: float = 0.8
    sweep_after_seconds: int = 60
    demo_tools: bool = True  # exposes "send forged webhook" etc. in the merchant console

    merchant_name: str = "Taller Ñandú"
    extra: dict = field(default_factory=dict)

    @property
    def resolved_llm(self) -> str:
        if self.llm_provider in ("anthropic", "scripted"):
            return self.llm_provider
        return "anthropic" if self.anthropic_api_key else "scripted"

    @property
    def resolved_paypal_base(self) -> str:
        if self.paypal_base_url:
            return self.paypal_base_url.rstrip("/")
        if self.paypal_mode == "sandbox":
            return "https://api-m.sandbox.paypal.com"
        return self.public_base_url.rstrip("/") + "/__mock_paypal"


def _bool(v: str | None, default: bool) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_settings(**overrides) -> Settings:
    env = os.environ.get
    s = Settings(
        public_base_url=env("PUBLIC_BASE_URL") or env("RENDER_EXTERNAL_URL")
        or f"http://localhost:{env('PORT', '8000')}",
        db_path=env("TENDERO_DB", str(ROOT / "data" / "tendero.sqlite3")),
        paypal_mode=env("PAYPAL_MODE", "mock").lower(),
        paypal_client_id=env("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=env("PAYPAL_CLIENT_SECRET", ""),
        paypal_webhook_id=env("PAYPAL_WEBHOOK_ID", ""),
        paypal_base_url=env("PAYPAL_BASE_URL", ""),
        llm_provider=env("LLM_PROVIDER", "auto").lower(),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        anthropic_model=env("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        anthropic_effort=env("ANTHROPIC_EFFORT", "low"),
        anthropic_fallbacks=_bool(env("ANTHROPIC_FALLBACKS"), True),
        currency=env("STORE_CURRENCY", "USD"),
        max_order_total=Decimal(env("MAX_ORDER_TOTAL", "500.00")),
        mock_webhook_delay=float(env("MOCK_WEBHOOK_DELAY", "0.8")),
        sweep_after_seconds=int(env("SWEEP_AFTER_SECONDS", "60")),
        demo_tools=_bool(env("DEMO_TOOLS"), True),
    )
    if s.paypal_mode == "mock":
        s.paypal_client_id = s.paypal_client_id or "mock-client-id"
        s.paypal_client_secret = s.paypal_client_secret or "mock-client-secret"
        s.paypal_webhook_id = s.paypal_webhook_id or "MOCK-WEBHOOK-ID"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s
