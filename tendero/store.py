"""SQLite persistence: conversations, chat transcript, orders, audit timeline, webhook log."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                 -- customer | merchant
    customer_name TEXT,
    llm_history TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,                 -- customer | agent | merchant | system
    text TEXT NOT NULL,
    card TEXT,                          -- optional JSON payload rendered as a card
    tools TEXT,                         -- JSON list of tool calls made for this reply
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    customer_name TEXT,
    status TEXT NOT NULL,
    currency TEXT NOT NULL,
    total TEXT NOT NULL,
    cart TEXT NOT NULL,
    paypal_order_id TEXT,
    approve_url TEXT,
    capture_id TEXT,
    paypal_fee TEXT,
    net_amount TEXT,
    paid_via TEXT,
    refund_id TEXT,
    refunded_amount TEXT,
    note TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    paid_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fulfillment_tasks (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,          -- qa | bench | courier
    kind TEXT NOT NULL,                 -- qa | pack | pickup
    name TEXT NOT NULL,
    start TEXT NOT NULL,                -- shop-local wall clock, ISO without offset
    "end" TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    verified INTEGER NOT NULL,
    received_at REAL NOT NULL,
    raw TEXT NOT NULL
);
"""

ORDER_COLUMNS = (
    "id conversation_id customer_name status currency total cart paypal_order_id approve_url "
    "capture_id paypal_fee net_amount paid_via refund_id refunded_amount note created_at "
    "updated_at paid_at"
).split()


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # ------------------------------------------------------------ conversations
    def create_conversation(self, kind: str, customer_name: str | None = None) -> str:
        cid = ("c_" if kind == "customer" else "m_") + uuid.uuid4().hex[:10]
        self._exec(
            "INSERT INTO conversations (id, kind, customer_name, created_at) VALUES (?,?,?,?)",
            (cid, kind, customer_name, time.time()),
        )
        return cid

    def get_conversation(self, cid: str) -> dict | None:
        return self._one("SELECT * FROM conversations WHERE id=?", (cid,))

    def get_history(self, cid: str) -> list[Any]:
        row = self.get_conversation(cid)
        return json.loads(row["llm_history"]) if row else []

    def set_history(self, cid: str, history: list[Any]) -> None:
        self._exec("UPDATE conversations SET llm_history=? WHERE id=?", (json.dumps(history), cid))

    def add_message(self, cid: str, role: str, text: str, card: dict | None = None,
                    tools: list | None = None) -> int:
        cur = self._exec(
            "INSERT INTO chat_messages (conversation_id, role, text, card, tools, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (cid, role, text, json.dumps(card) if card else None,
             json.dumps(tools) if tools else None, time.time()),
        )
        return int(cur.lastrowid)

    def messages(self, cid: str, after_id: int = 0) -> list[dict]:
        rows = self._all(
            "SELECT * FROM chat_messages WHERE conversation_id=? AND id>? ORDER BY id",
            (cid, after_id),
        )
        for r in rows:
            r["card"] = json.loads(r["card"]) if r["card"] else None
            r["tools"] = json.loads(r["tools"]) if r["tools"] else []
        return rows

    # ------------------------------------------------------------------- orders
    def next_order_id(self) -> str:
        row = self._one("SELECT COUNT(*) AS n FROM orders")
        return f"TN-{1001 + int(row['n'])}"

    def create_order(self, conversation_id: str, customer_name: str | None, cart: dict) -> dict:
        now = time.time()
        oid = self.next_order_id()
        self._exec(
            "INSERT INTO orders (id, conversation_id, customer_name, status, currency, total, cart,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (oid, conversation_id, customer_name, "DRAFT", cart["currency"], cart["total"],
             json.dumps(cart), now, now),
        )
        return self.get_order(oid)

    def update_order(self, oid: str, **fields) -> dict:
        bad = set(fields) - set(ORDER_COLUMNS)
        if bad:
            raise KeyError(f"unknown order fields {bad}")
        fields["updated_at"] = time.time()
        if "cart" in fields and isinstance(fields["cart"], dict):
            fields["cart"] = json.dumps(fields["cart"])
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), oid))
        return self.get_order(oid)

    def _decode(self, row: dict | None) -> dict | None:
        if row:
            row["cart"] = json.loads(row["cart"])
        return row

    def get_order(self, oid: str) -> dict | None:
        return self._decode(self._one("SELECT * FROM orders WHERE id=?", (oid,)))

    def order_by_paypal_id(self, paypal_order_id: str) -> dict | None:
        return self._decode(self._one("SELECT * FROM orders WHERE paypal_order_id=?", (paypal_order_id,)))

    def order_by_capture_id(self, capture_id: str) -> dict | None:
        return self._decode(self._one("SELECT * FROM orders WHERE capture_id=?", (capture_id,)))

    def latest_order_for(self, conversation_id: str, statuses: tuple[str, ...] | None = None) -> dict | None:
        sql = "SELECT * FROM orders WHERE conversation_id=?"
        args: tuple = (conversation_id,)
        if statuses:
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        return self._decode(self._one(sql + " ORDER BY created_at DESC LIMIT 1", args))

    def list_orders(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self._all("SELECT * FROM orders WHERE status=? ORDER BY created_at DESC", (status,))
        else:
            rows = self._all("SELECT * FROM orders ORDER BY created_at DESC")
        return [self._decode(r) for r in rows]

    # ------------------------------------------------------------------- events
    def add_event(self, order_id: str | None, kind: str, detail: str, ok: bool = True) -> None:
        self._exec(
            "INSERT INTO events (order_id, kind, detail, ok, created_at) VALUES (?,?,?,?,?)",
            (order_id, kind, detail, 1 if ok else 0, time.time()),
        )

    def events(self, after_id: int = 0, limit: int = 200) -> list[dict]:
        return self._all("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after_id, limit))

    # ----------------------------------------------------------------- webhooks
    def record_webhook(self, event_id: str, event_type: str, verified: bool, raw: str) -> bool:
        """Returns False when this event id was already processed (idempotency)."""
        try:
            self._exec(
                "INSERT INTO webhook_events (event_id, event_type, verified, received_at, raw) "
                "VALUES (?,?,?,?,?)",
                (event_id, event_type, 1 if verified else 0, time.time(), raw),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def webhook_seen(self, event_id: str) -> bool:
        return self._one("SELECT 1 AS x FROM webhook_events WHERE event_id=? AND verified=1",
                         (event_id,)) is not None

    # -------------------------------------------------------------- fulfillment
    def add_fulfillment_task(self, order_id: str, resource_id: str, kind: str, name: str,
                             start: str, end: str, detail: str = "") -> str:
        tid = "FT-" + uuid.uuid4().hex[:8]
        self._exec('INSERT INTO fulfillment_tasks (id, order_id, resource_id, kind, name, start, "end", detail) '
                   "VALUES (?,?,?,?,?,?,?,?)", (tid, order_id, resource_id, kind, name, start, end, detail))
        return tid

    def fulfillment_tasks(self, order_id: str | None = None) -> list[dict]:
        if order_id:
            return self._all("SELECT * FROM fulfillment_tasks WHERE order_id=? ORDER BY start", (order_id,))
        return self._all("SELECT * FROM fulfillment_tasks ORDER BY start")

    def get_fulfillment_task(self, task_id: str) -> dict | None:
        return self._one("SELECT * FROM fulfillment_tasks WHERE id=?", (task_id,))

    def update_fulfillment_task(self, task_id: str, start: str, end: str) -> None:
        self._exec('UPDATE fulfillment_tasks SET start=?, "end"=? WHERE id=?', (start, end, task_id))

    def delete_fulfillment_tasks(self, order_id: str) -> int:
        return self._exec("DELETE FROM fulfillment_tasks WHERE order_id=?", (order_id,)).rowcount
