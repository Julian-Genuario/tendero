"""Fulfillment planning: what happens in the workshop after PayPal confirms a payment.

When an order becomes PAID (verified webhook or API sweep), Tendero books the work on the
shop's three resources and the merchant sees it on a Bryntum Scheduler board:

* **Quality check** - gourds and leather pieces are inspected one by one (15 min per unit)
* **Packing bench** - pack and label (15 min + 5 min per unit)
* **Courier pickup** - the courier collects at 17:00, Monday to Friday

Tasks never overlap on a resource, respect workshop hours (Mon-Sat 9:00-19:00, Buenos Aires
time, which has no DST) and keep their order (check -> pack -> pickup). The customer is told
the planned dispatch day. Refunded orders are taken off the board. The owner can drag tasks on
the board; every move is validated here, so the board cannot produce an impossible plan.

All times are shop-local wall-clock times, stored as naive ISO strings ("2026-11-09T17:00:00"),
so the board shows the workshop's hours no matter where the viewer is.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

SHOP_TZ = timezone(timedelta(hours=-3))  # America/Argentina/Buenos_Aires, no DST
OPEN_H, CLOSE_H = 9, 19
WORKDAYS = range(0, 6)  # Mon-Sat
PICKUP_DAYS = range(0, 5)  # Mon-Fri
PICKUP_H, PICKUP_MIN = 17, 30
STEP = timedelta(minutes=15)
INSPECTED_SKUS = {"MATE-CAL", "CASE-LTH"}

RESOURCES = [
    {"id": "qa", "name": "Quality check", "role": "gourds and leather"},
    {"id": "bench", "name": "Packing bench", "role": "pack and label"},
    {"id": "courier", "name": "Courier pickup", "role": "17:00, Mon-Fri"},
]
KIND_RESOURCE = {"qa": "qa", "pack": "bench", "pickup": "courier"}
SEQUENCE = ["qa", "pack", "pickup"]
FMT = "%Y-%m-%dT%H:%M:%S"


class PlanError(ValueError):
    pass


def _iso(d: datetime) -> str:
    return d.strftime(FMT)


def _parse(s: str) -> datetime:
    return datetime.strptime(s[:19], FMT)


def _ceil(d: datetime) -> datetime:
    d = d.replace(second=0, microsecond=0)
    extra = d.minute % 15
    return d + timedelta(minutes=15 - extra) if extra else d


def _open_at(d: datetime) -> datetime:
    """First working instant at or after d."""
    while True:
        if d.weekday() in WORKDAYS and OPEN_H <= d.hour < CLOSE_H:
            return d
        if d.weekday() in WORKDAYS and d.hour < OPEN_H:
            return d.replace(hour=OPEN_H, minute=0)
        d = (d + timedelta(days=1)).replace(hour=OPEN_H, minute=0)


def _in_hours(start: datetime, end: datetime) -> bool:
    return (start.weekday() in WORKDAYS and start.date() == end.date()
            and start.hour >= OPEN_H and (end.hour, end.minute) <= (CLOSE_H, 0) and end > start)


def shop_now(clock: Callable[[], float] = time.time) -> datetime:
    return datetime.fromtimestamp(clock(), SHOP_TZ).replace(tzinfo=None)


class Fulfillment:
    def __init__(self, store, clock: Callable[[], float] = time.time):
        self.store, self.clock = store, clock

    # ----------------------------------------------------------- planning
    def _busy(self, resource_id: str) -> list[tuple[datetime, datetime]]:
        return [(_parse(t["start"]), _parse(t["end"])) for t in self.store.fulfillment_tasks()
                if t["resource_id"] == resource_id]

    def _slot(self, resource_id: str, earliest: datetime, minutes: int) -> datetime:
        dur = timedelta(minutes=minutes)
        busy = self._busy(resource_id)
        start = _open_at(_ceil(earliest))
        for _ in range(2000):
            end = start + dur
            if not _in_hours(start, end):
                start = _open_at((start + timedelta(days=1)).replace(hour=0, minute=0))
                continue
            clash = next((b for b in busy if start < b[1] and b[0] < end), None)
            if not clash:
                return start
            start = _open_at(_ceil(clash[1]))
        raise PlanError("no free slot found")  # pragma: no cover

    @staticmethod
    def _pickup(after: datetime) -> datetime:
        d = after.replace(hour=PICKUP_H, minute=0, second=0, microsecond=0)
        if d < after:
            d += timedelta(days=1)
        while d.weekday() not in PICKUP_DAYS:
            d += timedelta(days=1)
        return d

    def plan(self, order: dict) -> list[dict]:
        """Books the work for a PAID order. Idempotent: an order is planned once."""
        existing = self.store.fulfillment_tasks(order["id"])
        if existing:
            return existing
        units = sum(l["quantity"] for l in order["cart"]["lines"])
        inspected = sum(l["quantity"] for l in order["cart"]["lines"] if l["sku"] in INSPECTED_SKUS)
        label = ", ".join(f"{l['quantity']}x {l['name'].split(' (')[0]}" for l in order["cart"]["lines"])
        t = shop_now(self.clock)
        tasks = []
        if inspected:
            s = self._slot("qa", t, min(60, 15 * inspected))
            tasks.append(("qa", f"Check {inspected} piece(s)", s, s + timedelta(minutes=min(60, 15 * inspected))))
            t = tasks[-1][3]
        pack_min = 15 + 5 * units
        s = self._slot("bench", t, pack_min)
        tasks.append(("pack", f"Pack {units} item(s)", s, s + timedelta(minutes=pack_min)))
        p = self._pickup(tasks[-1][3])
        tasks.append(("pickup", "Courier pickup", p, p + timedelta(minutes=PICKUP_MIN)))
        for kind, name, start, end in tasks:
            self.store.add_fulfillment_task(order["id"], KIND_RESOURCE[kind], kind, f"{order['id']} · {name}",
                                            _iso(start), _iso(end), detail=label)
        self.store.add_event(order["id"], "fulfillment.planned",
                             f"{len(tasks)} task(s) booked; courier pickup {p.strftime('%a %d %b %H:%M')}")
        return self.store.fulfillment_tasks(order["id"])

    def dispatch_of(self, order_id: str) -> datetime | None:
        pick = next((t for t in self.store.fulfillment_tasks(order_id) if t["kind"] == "pickup"), None)
        return _parse(pick["start"]) if pick else None

    def cancel(self, order_id: str, why: str) -> int:
        n = self.store.delete_fulfillment_tasks(order_id)
        if n:
            self.store.add_event(order_id, "fulfillment.cancelled", f"{n} task(s) removed: {why}")
        return n

    # ------------------------------------------------------------- moves
    def move(self, task_id: str, start: str, end: str) -> dict:
        """Validates a drag/resize from the board. Raises PlanError if the plan would break."""
        task = self.store.get_fulfillment_task(task_id)
        if not task:
            raise PlanError("unknown task")
        s, e = _parse(start), _parse(end)
        if not _in_hours(s, e):
            raise PlanError("outside workshop hours (Mon-Sat 9:00-19:00)")
        if task["kind"] == "pickup" and (s.weekday() not in PICKUP_DAYS or (s.hour, s.minute) != (PICKUP_H, 0)):
            raise PlanError("the courier only collects at 17:00, Monday to Friday")
        if task["kind"] == "pickup" and e - s != timedelta(minutes=PICKUP_MIN):
            raise PlanError("a pickup lasts 30 minutes")
        siblings = {t["kind"]: t for t in self.store.fulfillment_tasks(task["order_id"])}
        i = SEQUENCE.index(task["kind"])
        before = [siblings[k] for k in SEQUENCE[:i] if k in siblings]
        after = [siblings[k] for k in SEQUENCE[i + 1:] if k in siblings]
        if before and s < _parse(before[-1]["end"]):
            raise PlanError(f"must start after '{before[-1]['name']}' ends")
        if after and e > _parse(after[0]["start"]):
            raise PlanError(f"must end before '{after[0]['name']}' starts")
        for b0, b1 in self._busy(task["resource_id"]):
            if (b0, b1) != (_parse(task["start"]), _parse(task["end"])) and s < b1 and b0 < e \
                    and task["kind"] != "pickup":
                raise PlanError("that slot is already taken on this resource")
        self.store.update_fulfillment_task(task_id, _iso(s), _iso(e))
        self.store.add_event(task["order_id"], "fulfillment.moved",
                             f"{task['name']} moved to {s.strftime('%a %d %b %H:%M')} by the owner")
        return self.store.get_fulfillment_task(task_id)

    # ------------------------------------------------------------- views
    def board(self) -> dict:
        tasks = self.store.fulfillment_tasks()
        return {
            "timezone": "America/Argentina/Buenos_Aires",
            "now": _iso(shop_now(self.clock)),
            "resources": RESOURCES,
            "events": [{"id": t["id"], "resourceId": t["resource_id"], "name": t["name"], "kind": t["kind"],
                        "orderId": t["order_id"], "startDate": t["start"], "endDate": t["end"],
                        "detail": t["detail"], "eventColor": {"qa": "orange", "pack": "teal",
                                                              "pickup": "indigo"}[t["kind"]]}
                       for t in tasks],
            "dependencies": [
                {"id": f"{a['id']}>{b['id']}", "from": a["id"], "to": b["id"]}
                for oid in sorted({t["order_id"] for t in tasks})
                for a, b in _pairs([t for k in SEQUENCE for t in tasks if t["order_id"] == oid and t["kind"] == k])
            ],
        }

    def upcoming(self) -> list[dict]:
        now = _iso(shop_now(self.clock))
        out: dict[str, dict] = {}
        for t in self.store.fulfillment_tasks():
            if t["end"] < now:
                continue
            o = out.setdefault(t["order_id"], {"order_id": t["order_id"], "items": t["detail"], "tasks": []})
            o["tasks"].append({"step": t["kind"], "start": t["start"].replace("T", " ")[:16]})
            if t["kind"] == "pickup":
                o["dispatch"] = _parse(t["start"]).strftime("%a %d %b, %H:%M")
                o["_at"] = t["start"]
        rows = sorted(out.values(), key=lambda o: o.get("_at", ""))
        for o in rows:
            o.pop("_at", None)
        return rows


def _pairs(seq: list) -> list[tuple]:
    return list(zip(seq, seq[1:]))
