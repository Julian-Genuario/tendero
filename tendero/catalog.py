"""The merchant's catalog and the *only* place where prices and totals are computed.

The LLM never does arithmetic: it proposes SKUs and quantities, this module prices them.
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "catalog.json"
CENT = Decimal("0.01")


def money(v) -> Decimal:
    return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(v: Decimal) -> str:
    return str(money(v))


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


@dataclass
class Product:
    sku: str
    name: str
    price: Decimal
    stock: int
    description: str
    keywords: list[str]

    def public(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "price": fmt(self.price),
            "in_stock": self.stock,
            "description": self.description,
        }


class CartError(ValueError):
    pass


class Catalog:
    def __init__(self, data: dict):
        self.store = data["store"]
        self.currency: str = data["store"]["currency"]
        self.shipping_flat = money(data["store"]["shipping_flat"])
        self.free_shipping_over = money(data["store"]["free_shipping_over"])
        self.products: dict[str, Product] = {
            p["sku"]: Product(
                sku=p["sku"],
                name=p["name"],
                price=money(p["price"]),
                stock=int(p["stock"]),
                description=p["description"],
                keywords=[_norm(k) for k in p.get("keywords", [])],
            )
            for p in data["products"]
        }

    @classmethod
    def load(cls, path: Path = CATALOG_PATH) -> "Catalog":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def search(self, query: str = "") -> list[dict]:
        q = _norm(query or "").strip()
        if not q:
            return [p.public() for p in self.products.values()]
        words = [w for w in q.replace(",", " ").split() if len(w) > 2]
        hits = []
        for p in self.products.values():
            hay = " ".join([_norm(p.name), _norm(p.description), *p.keywords, p.sku.lower()])
            if any(w in hay for w in words) or q in hay:
                hits.append(p.public())
        return hits or [p.public() for p in self.products.values()]

    def find_in_text(self, text: str) -> list[str]:
        """SKUs whose keywords appear in free text (used by the offline demo brain)."""
        t = _norm(text)
        found: list[tuple[int, str]] = []
        for p in self.products.values():
            positions = [t.find(k) for k in p.keywords if k and t.find(k) >= 0]
            if positions:
                found.append((min(positions), p.sku))
        return [sku for _, sku in sorted(found)]

    def price_cart(self, items: list[dict], max_total: Decimal | None = None) -> dict:
        if not items:
            raise CartError("The cart is empty.")
        merged: dict[str, int] = {}
        for it in items:
            sku = str(it.get("sku", "")).strip().upper()
            qty = int(it.get("quantity", 0))
            if sku not in self.products:
                raise CartError(f"Unknown SKU {sku!r}. Use search_catalog to find valid SKUs.")
            if qty <= 0 or qty > 50:
                raise CartError(f"Invalid quantity {qty} for {sku}.")
            merged[sku] = merged.get(sku, 0) + qty
        lines, subtotal = [], Decimal("0")
        for sku, qty in merged.items():
            p = self.products[sku]
            if qty > p.stock:
                raise CartError(f"Only {p.stock} units of {p.name} in stock.")
            line_total = money(p.price * qty)
            subtotal += line_total
            lines.append(
                {
                    "sku": sku,
                    "name": p.name,
                    "quantity": qty,
                    "unit_price": fmt(p.price),
                    "line_total": fmt(line_total),
                }
            )
        subtotal = money(subtotal)
        shipping = Decimal("0.00") if subtotal >= self.free_shipping_over else self.shipping_flat
        total = money(subtotal + shipping)
        if max_total is not None and total > max_total:
            raise CartError(
                f"Order total {fmt(total)} {self.currency} exceeds the per-order limit of "
                f"{fmt(max_total)} {self.currency}; the merchant must handle this one personally."
            )
        return {
            "currency": self.currency,
            "lines": lines,
            "subtotal": fmt(subtotal),
            "shipping": fmt(shipping),
            "total": fmt(total),
        }
