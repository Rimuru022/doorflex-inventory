"""Weekly reorder engine — the Friday 08:00 AZ run.

Looks at warehouse stock only (per product decision). Products with
quantity <= min_threshold are grouped by their active distributor:
  - active distributor is one of dist_a..dist_d with an email  -> reorder email (C.1)
  - active distributor is amazon                                 -> goes in the internal "buy on Amazon" section
  - no active distributor or no distributor email                -> weekly summary only
"""
import logging
from datetime import datetime

from modules import distributors

logger = logging.getLogger("reorder")


def find_low_warehouse_stock(conn):
    """warehouse_stock rows where quantity <= min_threshold, part and warehouse active."""
    return conn.execute(
        """SELECT ws.*, p.id AS part_id, p.name AS part_name, p.amazon_sku,
                  w.name AS warehouse_name, d.id AS active_distributor_id,
                  d.name AS active_distributor_name, d.email AS active_distributor_email,
                  pd.distributor_sku, pd.distributor_part_name
           FROM warehouse_stock ws
           JOIN parts p ON p.id = ws.part_id
           JOIN warehouses w ON w.id = ws.warehouse_id
           LEFT JOIN product_distributors pd
                  ON pd.product_id = ws.part_id AND pd.is_active = 1
           LEFT JOIN distributors d ON d.id = pd.distributor_id
           WHERE ws.quantity <= ws.min_threshold
             AND p.active = 1
             AND w.active = 1
           ORDER BY w.id, p.name"""
    ).fetchall()


def compute_qty(min_threshold, current):
    """Default reorder quantity: min*2 - current (min 1)."""
    return max((min_threshold or 0) * 2 - (current or 0), 1)


def has_pending_reorder(conn, part_id, warehouse_id):
    row = conn.execute(
        "SELECT 1 FROM reorder_requests WHERE part_id=? AND warehouse_id=? AND status IN ('pending','sent')",
        (part_id, warehouse_id),
    ).fetchone()
    return row is not None


def create_reorder(conn, part_id, distributor_id, warehouse_id, qty):
    conn.execute(
        """INSERT INTO reorder_requests (part_id, distributor_id, warehouse_id, qty, status)
           VALUES (?, ?, ?, ?, 'pending')""",
        (part_id, distributor_id, warehouse_id, qty),
    )
    conn.commit()
    return conn.execute("SELECT * FROM reorder_requests ORDER BY id DESC LIMIT 1").fetchone()


def resolve_reorder(conn, reorder_id, mode="manual"):
    """Mark a reorder as received. `mode` is 'manual' (Mark as ready) or 'stock'
    (auto-resolve because warehouse stock rose back >= min)."""
    row = conn.execute("SELECT * FROM reorder_requests WHERE id=?", (reorder_id,)).fetchone()
    if not row:
        return None, "Reorder not found"
    if row["status"] == "received":
        return row, None
    conn.execute(
        "UPDATE reorder_requests SET status='received', resolved_at=datetime('now', 'localtime') WHERE id=?",
        (reorder_id,),
    )
    conn.commit()
    return conn.execute("SELECT * FROM reorder_requests WHERE id=?", (reorder_id,)).fetchone(), None


def auto_resolve_by_stock(conn):
    """Resolve pending reorders whose warehouse stock is now >= min_threshold."""
    resolved = 0
    rows = conn.execute(
        """SELECT r.id FROM reorder_requests r
           JOIN warehouse_stock ws ON ws.warehouse_id = r.warehouse_id AND ws.part_id = r.part_id
           WHERE r.status IN ('pending','sent') AND ws.quantity >= ws.min_threshold"""
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE reorder_requests SET status='received', resolved_at=datetime('now', 'localtime') WHERE id=?",
            (row["id"],),
        )
        resolved += 1
    if resolved:
        conn.commit()
    return resolved


def build_distributor_orders(conn):
    """Group low warehouse stock by active distributor. Returns a dict:
        {
          "dist_a": [ {part..., qty}, ... ],
          ...
          "amazon": [ ... ],        # buy-on-Amazon items
          "unassigned": [ ... ],    # no active distributor or no email
        }
    """
    groups = {d["id"]: [] for d in distributors.list_distributors(conn)}
    groups["unassigned"] = []
    for row in find_low_warehouse_stock(conn):
        item = {
            "part_id": row["part_id"],
            "part_name": row["part_name"],
            "sku": row["part_id"],
            "distributor_sku": row["distributor_sku"] or "",
            "distributor_part_name": row["distributor_part_name"] or "",
            "qty": compute_qty(row["min_threshold"], row["quantity"]),
            "current": row["quantity"],
            "min": row["min_threshold"],
            "warehouse_id": row["warehouse_id"],
            "warehouse_name": row["warehouse_name"],
            "amazon_url": row["amazon_sku"] or "",
        }
        active_id = row["active_distributor_id"]
        if active_id in groups:
            groups[active_id].append(item)
        else:
            groups["unassigned"].append(item)
    return {k: v for k, v in groups.items() if v}


def build_weekly_alert_data(conn):
    """Section A: every low item grouped by distributor, plus the 'no
    distributor' bucket. Reuses build_distributor_orders + adds diff field."""
    orders = build_distributor_orders(conn)
    sections = {}
    for key, items in orders.items():
        if key == "unassigned":
            continue
        sections[key] = [
            {**it, "diff": it["min"] - it["current"]} for it in items
        ]
    sections["unassigned"] = [
        {**it, "diff": it["min"] - it["current"]} for it in orders.get("unassigned", [])
    ]
    return sections, sum(len(v) for v in sections.values())


def run_weekly_reorder(conn, send_fn, throttle_seconds=60):
    """Friday orchestrator. `send_fn` is injected so tests can fake delivery.

    Returns a result dict describing what was done (used for email_log and
    the weekly summary).
    """
    auto_resolve_by_stock(conn)
    orders = build_distributor_orders(conn)
    result = {
        "reorders": [],       # per-distributor sends
        "amazon_items": [],   # buy-on-Amazon
        "unassigned": [],     # low stock, no distributor
        "total_low": 0,
        "emails_sent": [],
        "errors": [],
    }
    result["total_low"] = sum(len(v) for v in orders.values())

    for distributor_id in ("dist_a", "dist_b", "dist_c", "dist_d"):
        items = orders.get(distributor_id, [])
        if not items:
            continue
        dist = distributors.get_distributor(conn, distributor_id)
        email = (dist["email"] or "").strip() if dist else ""
        if not email:
            for it in items:
                result["unassigned"].append(it)
                _link_reorder(conn, it, distributor_id, "no_recipient")
            continue
        reorder_ids = []
        for it in items:
            rr = _link_reorder(conn, it, distributor_id, "pending")
            reorder_ids.append(rr["id"])
        result["reorders"].append({
            "distributor_id": distributor_id,
            "distributor_name": dist["name"] if dist else distributor_id,
            "email": email,
            "items": items,
            "reorder_ids": reorder_ids,
        })

    amazon_items = orders.get("amazon", [])
    for it in amazon_items:
        rr = _link_reorder(conn, it, "amazon", "pending")
        result["amazon_items"].append({**it, "reorder_id": rr["id"]})

    result["unassigned"].extend(orders.get("unassigned", []))

    conn.commit()
    return result


def _link_reorder(conn, item, distributor_id, status):
    """Create a reorder_requests row if none is pending for (part, warehouse).
    BEGIN IMMEDIATE closes the check-then-insert race between the pending
    check and the insert (only matters if this is ever called from more
    than the single weekly-reorder thread, but it's a one-line guard)."""
    conn.execute("BEGIN IMMEDIATE")
    if has_pending_reorder(conn, item["part_id"], item["warehouse_id"]):
        conn.commit()
        return conn.execute(
            "SELECT * FROM reorder_requests WHERE part_id=? AND warehouse_id=? AND status IN ('pending','sent')",
            (item["part_id"], item["warehouse_id"]),
        ).fetchone()
    rr = create_reorder(conn, item["part_id"], distributor_id, item["warehouse_id"], item["qty"])
    conn.execute("UPDATE reorder_requests SET status=? WHERE id=?", (status, rr["id"]))
    return conn.execute("SELECT * FROM reorder_requests WHERE id=?", (rr["id"],)).fetchone()


def week_key(now=None):
    now = now or datetime.now()
    return now.isocalendar()[:2]
