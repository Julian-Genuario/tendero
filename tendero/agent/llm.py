"""Swappable LLM providers behind one tiny interface.

``complete(system, tools, messages) -> LLMTurn`` where messages/content use the Anthropic
Messages API block format (text / tool_use / tool_result). Any provider that can speak tool
calls can be added by implementing ``complete``.

* ``AnthropicProvider`` - Claude via the official ``anthropic`` SDK (default model
  ``claude-sonnet-5-5``), server-side refusal fallbacks enabled.
* ``ScriptedProvider``  - offline demo brain used when no API key is configured. It is
  deterministic keyword logic that emits the *same* tool calls, so the PayPal flow, the
  guardrails and the UI run for real. The UI badge says "offline demo" whenever it is active.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..catalog import Catalog


@dataclass
class LLMTurn:
    content: list[dict]
    stop_reason: str
    model: str = ""
    usage: dict = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    label: str

    async def complete(self, system: str, tools: list[dict], messages: list[dict]) -> LLMTurn: ...


# --------------------------------------------------------------------------- Claude
_KEEP = {
    "text": ("type", "text"),
    "tool_use": ("type", "id", "name", "input"),
    "thinking": ("type", "thinking", "signature"),
    "redacted_thinking": ("type", "data"),
}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5-5", effort: str = "low",
                 fallbacks: bool = True, client: Any = None, max_tokens: int = 8000):
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(api_key=api_key)
        self.client, self.model, self.effort = client, model, effort
        self.fallbacks, self.max_tokens = fallbacks, max_tokens
        self.label = f"Claude · {model}"

    async def complete(self, system: str, tools: list[dict], messages: list[dict]) -> LLMTurn:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
        )
        if self.fallbacks:
            # On a safety-classifier decline the API re-runs the turn on a fallback model.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
            resp = await self.client.beta.messages.create(**kwargs)
        else:
            resp = await self.client.messages.create(**kwargs)
        content = []
        for block in resp.content:
            d = block.model_dump(mode="json") if hasattr(block, "model_dump") else dict(block)
            keep = _KEEP.get(d.get("type"))
            if keep:  # pass thinking / tool_use blocks back unchanged on the next request
                content.append({k: d[k] for k in keep if k in d})
        usage = resp.usage.model_dump(mode="json") if getattr(resp, "usage", None) is not None and hasattr(resp.usage, "model_dump") else {}
        return LLMTurn(content=content, stop_reason=resp.stop_reason or "end_turn",
                       model=getattr(resp, "model", self.model), usage=usage)


# ----------------------------------------------------------------- offline demo brain
NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "a couple of": 2}
YES = re.compile(r"\b(yes|yep|yeah|sure|ok(ay)?|confirm(ed)?|go ahead|send (me )?(the|a) link|si|dale|let'?s do it|please do)\b", re.I)
STATUS = re.compile(r"\b(paid|status|go through|went through|received|arrived|confirm(ed)? (my )?payment)\b", re.I)
INFO = re.compile(r"\b(ship|shipping|deliver|madrid|return|refund policy|hours|open)\b", re.I)
ORDER_ID = re.compile(r"\bTN-\d{4,}\b", re.I)


def _last_user(messages: list[dict]) -> tuple[str | None, list[dict]]:
    last = messages[-1]
    c = last["content"]
    if isinstance(c, str):
        return c, []
    texts = [b.get("text", "") for b in c if b.get("type") == "text"]
    results = [b for b in c if b.get("type") == "tool_result"]
    return (" ".join(texts) if texts and not results else None), results


def _tool_calls_by_id(messages: list[dict]) -> dict[str, dict]:
    out = {}
    for m in messages:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    out[b["id"]] = b
    return out


def _results(messages: list[dict]) -> list[tuple[str, dict]]:
    """All (tool_name, parsed_result) pairs in the conversation, oldest first."""
    calls, out = _tool_calls_by_id(messages), []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    try:
                        out.append((calls[b["tool_use_id"]]["name"], json.loads(_text_of(b))))
                    except Exception:
                        out.append((calls[b["tool_use_id"]]["name"], {"error": _text_of(b)}))
    return out


def _text_of(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c or ""


class ScriptedProvider:
    name = "scripted"
    label = "Offline demo brain (no API key)"

    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._n = 0

    def _id(self) -> str:
        self._n += 1
        return f"toolu_demo_{uuid.uuid4().hex[:12]}"

    def _use(self, name: str, inp: dict) -> dict:
        return {"type": "tool_use", "id": self._id(), "name": name, "input": inp}

    async def complete(self, system: str, tools: list[dict], messages: list[dict]) -> LLMTurn:
        names = {t["name"] for t in tools}
        text, results = _last_user(messages)
        if "refund_order" in names:
            turn = self._merchant(text, results, messages)
        else:
            turn = self._customer(text, results, messages)
        turn.model = "offline-demo"
        return turn

    # ------------------------------------------------------------- customer side
    def _quantities(self, text: str) -> list[dict]:
        t = text.lower()
        items = []
        for sku in self.catalog.find_in_text(text):
            p = self.catalog.products[sku]
            qty = 1
            for kw in sorted(p.keywords, key=len, reverse=True):
                i = t.find(kw)
                if i < 0:
                    continue
                before = t[max(0, i - 24):i].split()
                for w in reversed(before[-3:]):
                    w = w.strip(",.")
                    if w.isdigit():
                        qty = int(w)
                        break
                    if w in NUM_WORDS:
                        qty = NUM_WORDS[w]
                        break
                break
            items.append({"sku": sku, "quantity": qty})
        return items

    def _customer(self, text: str | None, results: list[dict], messages: list[dict]) -> LLMTurn:
        history = _results(messages)
        if text is not None:
            draft = next((r for n, r in reversed(history) if n == "build_cart" and "order_id" in r), None)
            linked = next((r for n, r in reversed(history) if n == "create_payment_link" and "order_id" in r), None)
            items = self._quantities(text)
            calls = []
            if items:
                calls.append(self._use("build_cart", {"items": items}))
                if INFO.search(text):
                    calls.append(self._use("store_info", {}))
            elif linked and STATUS.search(text):
                calls.append(self._use("get_order_status", {"order_id": linked["order_id"]}))
            elif draft and YES.search(text):
                calls.append(self._use("create_payment_link", {"order_id": draft["order_id"]}))
            elif INFO.search(text):
                calls.append(self._use("store_info", {}))
            else:
                calls.append(self._use("search_catalog", {"query": ""}))
            return LLMTurn(calls, "tool_use")

        # we just got tool results: write the reply from them
        calls = _tool_calls_by_id(messages)
        parts = []
        for r in sorted(results, key=lambda r: calls.get(r["tool_use_id"], {}).get("name") != "store_info"):
            name = calls.get(r["tool_use_id"], {}).get("name")
            if r.get("is_error"):
                parts.append(f"Sorry, I couldn't do that: {_text_of(r)}")
                continue
            parts.append(self._say(name, json.loads(_text_of(r))))
        return LLMTurn([{"type": "text", "text": "\n\n".join(p for p in parts if p)}], "end_turn")

    def _say(self, name: str | None, d: dict) -> str:
        if name == "search_catalog":
            rows = "\n".join(f"• {p['name']} - {p['price']} {d['currency']}" for p in d["products"])
            return f"Hi! Here's what we make in our Buenos Aires workshop:\n{rows}\nWhat would you like?"
        if name == "build_cart":
            rows = "\n".join(f"• {l['quantity']}× {l['name'].split(' (')[0]} - {l['line_total']}" for l in d["lines"])
            ship = "free" if d["shipping"] == "0.00" else f"{d['shipping']}"
            return (f"Here's your order {d['order_id']}:\n{rows}\nShipping: {ship}\n"
                    f"Total: {d['total']} {d['currency']}\n\nShall I send you a secure PayPal payment link?")
        if name == "store_info":
            return "Yes, we ship worldwide - Madrid included. " + d["shipping_policy"].split(". ", 1)[1]
        if name == "create_payment_link":
            return (f"Done! Tap the button below to pay {d['total']} {d['currency']} with PayPal. "
                    f"I'll confirm right here as soon as PayPal notifies us.")
        if name == "get_order_status":
            return f"Order {d['order_id']} is {d['status']}: {d['meaning']}."
        return json.dumps(d)

    # ------------------------------------------------------------- merchant side
    def _merchant(self, text: str | None, results: list[dict], messages: list[dict]) -> LLMTurn:
        calls = _tool_calls_by_id(messages)
        if text is not None:
            if re.search(r"\brefund\b", text, re.I):
                m = ORDER_ID.search(text)
                if m:
                    return LLMTurn([self._use("refund_order", {"order_id": m.group(0).upper(),
                                                               "reason": self._reason(text), "amount": ""})], "tool_use")
                return LLMTurn([self._use("list_orders", {"status": "PAID"})], "tool_use")
            if re.search(r"\b(how|today|summary|reconcil|issue|problem|money|made|sales)\w*", text, re.I):
                return LLMTurn([self._use("reconciliation_report", {})], "tool_use")
            return LLMTurn([self._use("list_orders", {"status": "ANY"})], "tool_use")

        r = results[0]
        name = calls.get(r["tool_use_id"], {}).get("name")
        if r.get("is_error"):
            return LLMTurn([{"type": "text", "text": f"PayPal refused that: {_text_of(r)}"}], "end_turn")
        d = json.loads(_text_of(r))
        if name == "list_orders":
            original = self._original_request(messages)
            if original and re.search(r"\brefund\b", original, re.I) and d["orders"]:
                target = d["orders"][0]["order_id"]
                return LLMTurn([{"type": "text", "text": f"The latest paid order is {target}. Refunding it now."},
                                self._use("refund_order", {"order_id": target, "reason": self._reason(original),
                                                           "amount": ""})], "tool_use")
            rows = "\n".join(f"• {o['order_id']} {o['status']} {o['total']} {o['currency']}" for o in d["orders"]) or "No orders yet."
            return LLMTurn([{"type": "text", "text": rows}], "end_turn")
        if name == "reconciliation_report":
            cur = d["currency"]
            lines = [f"Today: {d['paid_orders']} paid order(s), {d['gross_collected']} {cur} collected, "
                     f"{d['paypal_fees']} in PayPal fees, {d['net_collected']} {cur} net."]
            if d["units_sold"]:
                best = max(d["units_sold"].items(), key=lambda kv: kv[1])
                lines.append(f"Best seller: {best[0]} ({best[1]} units).")
            lines.append(f"Pending payments: {len(d['pending_orders'])}. Mismatches to review: {len(d['mismatches'])}.")
            if d["rejected_webhooks"]:
                lines.append(f"Heads-up: {d['rejected_webhooks']} fake payment notification(s) failed PayPal "
                             f"signature verification and were ignored - no order was marked paid.")
            return LLMTurn([{"type": "text", "text": " ".join(lines)}], "end_turn")
        if name == "refund_order":
            return LLMTurn([{"type": "text", "text": f"Refund {d['refund_id']} for {d['amount']} sent through PayPal "
                                                     f"({d['status']}). The customer is notified once PayPal's signed "
                                                     f"webhook confirms it."}], "end_turn")
        return LLMTurn([{"type": "text", "text": json.dumps(d)}], "end_turn")

    @staticmethod
    def _reason(text: str) -> str:
        m = re.search(r"(?:because|reason:?|\s-\s|—)\s*(.+)$", text, re.I)
        return (m.group(1) if m else "Refund requested by the shop").strip()[:200]

    @staticmethod
    def _original_request(messages: list[dict]) -> str | None:
        for m in reversed(messages):
            if m["role"] == "user" and isinstance(m["content"], str):
                return m["content"]
            if m["role"] == "user" and isinstance(m["content"], list):
                t = [b.get("text", "") for b in m["content"] if b.get("type") == "text"]
                if t:
                    return " ".join(t)
        return None
