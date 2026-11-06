"""Market price check for the owner, powered by Channel3 product search.

"Are my prices fair?" is a question small makers rarely have time to answer. The owner copilot
asks Channel3 (https://channel3.ai, one API over 100M+ products and their retailer offers) for
products comparable to each catalog item, keeps in-stock offers in the shop's currency, and
returns the low / median / high market price next to the shop's own price.

* Live: ``CHANNEL3_API_KEY`` set -> ``POST https://api.trychannel3.com/v1/search``
  with the ``x-api-key`` header.
* Offline: no key -> a bundled **sample** response in the same schema
  (``tendero/data/market_sample.json``), labelled as sample data everywhere it is shown.

Prices are only *reported*: the copilot never changes catalog prices on its own.
"""
from __future__ import annotations

import json
import statistics
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from .catalog import Catalog, fmt, money

C3_BASE = "https://api.trychannel3.com"
SAMPLE = Path(__file__).resolve().parent / "data" / "market_sample.json"


class MarketError(RuntimeError):
    pass


class Channel3Client:
    def __init__(self, api_key: str, base_url: str = C3_BASE,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15.0):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport, timeout=timeout,
                                       headers={"x-api-key": api_key, "Content-Type": "application/json"})

    async def search(self, query: str, limit: int = 10) -> dict:
        r = await self._http.post("/v1/search", json={"query": query, "limit": limit,
                                                      "filters": {"availability": ["InStock"]}})
        if r.status_code >= 400:
            raise MarketError(f"Channel3 search failed: HTTP {r.status_code}")
        return r.json()

    async def aclose(self) -> None:
        await self._http.aclose()


class SampleChannel3:
    """Offline stand-in that replays a recorded response shape. Never presented as live data."""

    def __init__(self, path: Path = SAMPLE):
        self.data = json.loads(path.read_text(encoding="utf-8"))

    async def search(self, query: str, limit: int = 10) -> dict:
        return {"products": self.data.get(query, [])[:limit]}

    async def aclose(self) -> None:
        pass


def _offer_price(offer: dict) -> tuple[Decimal, str] | None:
    p = offer.get("price") or {}
    # The docs show both {"price": n} and {"amount": n}; accept either.
    v = p.get("price", p.get("amount"))
    if v is None:
        return None
    return money(v), p.get("currency", "")


def summarize(products: list[dict], currency: str) -> dict:
    """Collapses Channel3 products into comparable in-stock, new offers in our currency."""
    comps = []
    for prod in products:
        best = None
        for off in prod.get("offers") or []:
            if off.get("availability") not in (None, "InStock") or off.get("condition") not in (None, "new"):
                continue
            pc = _offer_price(off)
            if not pc or pc[1] != currency:
                continue
            if best is None or pc[0] < best[0]:
                best = (pc[0], off)
        if best:
            comps.append({"title": prod.get("title", "")[:90], "domain": best[1].get("domain", ""),
                          "price": fmt(best[0]), "url": best[1].get("url", "")})
    return {"comparables": sorted(comps, key=lambda c: Decimal(c["price"]))}


class Market:
    def __init__(self, catalog: Catalog, api_key: str = "", client=None):
        self.catalog = catalog
        self.live = bool(api_key) or client is not None
        self.client = client or (Channel3Client(api_key) if api_key else SampleChannel3())
        self.source = "Channel3 live search" if self.live else "Channel3 sample data (no CHANNEL3_API_KEY)"

    async def check(self, sku: str = "") -> dict[str, Any]:
        cur = self.catalog.currency
        rows = []
        targets = [self.catalog.products[sku.upper()]] if sku else list(self.catalog.products.values())
        if sku and sku.upper() not in self.catalog.products:
            raise MarketError(f"unknown SKU {sku}")
        for p in targets:
            query = self.catalog.market_queries.get(p.sku, p.name.split(" (")[0])
            res = await self.client.search(query, limit=10)
            s = summarize(res.get("products") or [], cur)
            prices = [Decimal(c["price"]) for c in s["comparables"]]
            row = {"sku": p.sku, "product": p.name.split(" (")[0], "our_price": fmt(p.price), "query": query,
                   "offers": len(prices), "comparables": s["comparables"][:5]}
            if prices:
                med = money(statistics.median(prices))
                diff = (p.price - med) / med * 100
                row.update(low=fmt(min(prices)), median=fmt(med), high=fmt(max(prices)),
                           vs_median_pct=float(round(diff, 1)),
                           position="below market" if diff < -10 else "above market" if diff > 10 else "in line")
            else:
                row.update(low=None, median=None, high=None, vs_median_pct=None, position="no comparable offers")
            rows.append(row)
        return {"currency": cur, "source": self.source, "live": self.live, "rows": rows}

    async def aclose(self) -> None:
        await self.client.aclose()
