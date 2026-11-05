"""Entry point: ``python -m tendero.main`` (or ``uvicorn tendero.main:app``)."""
from __future__ import annotations

import logging
import os

from .app import create_app
from .config import load_settings

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
settings = load_settings()
app = create_app(settings)


def run() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"\n  Tendero on http://{host}:{port}  |  LLM: {app.state.provider.label}  |  "
          f"PayPal: {settings.paypal_mode}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
