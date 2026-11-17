from modules.logger import log_transaction
from modules import events as events_mod


def _maybe_auto_resolve_reorders(conn):
    """Hook: after any warehouse_stock write, resolve pending reorders whose
    stock is back at/above min. Import is local to avoid a circular import
    (reorder.py imports distributors, which does not import inventory)."""
    try:
        from modules.reorder import auto_resolve_by_stock
        auto_resolve_by_stock(conn)
    except ImportError:
        pass


def get_truck_stock(conn, truck_id):
    return conn.execute("""
        SELECT s.*, p.name AS part_name, p.unit
        FROM stock s
        JOIN parts p ON p.id = s.part_id
        WHERE s.truck_id=? AND p.active=1
        ORDER BY p.name
    """, (truck_id,)).fetchall()


def get_warehouse_stock(conn, warehouse_id):
    return conn.execute("""
        SELECT ws.*, p.name AS part_name, p.unit
        FROM warehouse_stock ws
        JOIN parts p ON p.id = ws.part_id
        WHERE ws.warehouse_id=? AND p.active=1
        ORDER BY p.name
    """, (warehouse_id,)).fetchall()


def deduct(conn, truck_id, warehouse_id, part_id, quantity, tech_name, invoice_id, confidence, format_type, raw_text="", invoice_name="", record_alert=True):
    part = conn.execute("SELECT active, name FROM parts WHERE id=?", (part_id,)).fetchone()
    if not part:
        return None, f"Part '{part_id}' not found in catalog"
    if not part["active"]:
        return None, f"Part '{part_id}' is deactivated (not in current catalog)"

    # Lock before reading current quantity: two concurrent deductions on the
    # same truck/part must not both read the same stale value and race on
    # the later UPDATE (lost-update). BEGIN IMMEDIATE grabs SQLite's write
    # lock upfront; a second concurrent call blocks here instead.
    conn.execute("BEGIN IMMEDIATE")
    truck_stock = conn.execute(
        "SELECT * FROM stock WHERE truck_id=? AND part_id=?",
        (truck_id, part_id),
    ).fetchone()

    if not truck_stock or truck_stock["quantity"] < quantity:
        available = truck_stock["quantity"] if truck_stock else 0
        if record_alert:
            _record_stock_alert(conn, invoice_id, part_id, part["name"], truck_id, warehouse_id,
                                quantity, available)
        else:
            conn.rollback()
        loc = "assigned" if truck_stock else "on the truck"
        return None, f"Insufficient stock: truck {truck_id} has {available} of '{part['name']}' {loc}, invoice requests {quantity}. Restock the truck first."

    truck_before = truck_stock["quantity"]
    truck_after = truck_before - quantity

    conn.execute(
        "UPDATE stock SET quantity=?, updated_at=datetime('now', 'localtime') WHERE truck_id=? AND part_id=?",
        (truck_after, truck_id, part_id),
    )

    log_transaction(
        conn, tech_name=tech_name, truck_id=truck_id, warehouse_id=warehouse_id,
        part_id=part_id, quantity_change=-quantity,
        stock_before=truck_before,
        stock_after=truck_after, invoice_id=invoice_id, confidence=confidence,
        raw_text=raw_text, source="email", format=format_type,
        invoice_name=invoice_name,
    )

    conn.execute(
        """UPDATE stock_alerts SET resolved=1
           WHERE resolved=0 AND part_id=? AND truck_id=? AND requested_qty<=? AND invoice_id=?""",
        (part_id, truck_id, quantity, invoice_id or ""),
    )

    conn.commit()

    is_low = truck_after < truck_stock["min_threshold"]
    return {
        "part_id": part_id,
        "truck_before": truck_before,
        "truck_after": truck_after,
        "is_low": is_low,
    }, None


def _record_stock_alert(conn, invoice_id, part_id, part_name, truck_id, warehouse_id, requested, available):
    conn.execute(
        """INSERT INTO stock_alerts (invoice_id, part_id, part_name, truck_id, warehouse_id, requested_qty, available_qty)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (invoice_id, part_id, part_name, truck_id, warehouse_id, requested, available),
    )
    events_mod.log_event(
        conn, "warn", "inventory", "stock.shortfall",
        f"Stock shortfall: '{part_name}' {truck_id} requested {requested} (had {available})",
        detail={"part_id": part_id, "part_name": part_name, "truck_id": truck_id,
                "warehouse_id": warehouse_id, "requested": requested, "available": available},
        ref=invoice_id,
    )
    conn.commit()


def pwa_deduct(conn, truck_id, warehouse_id, part_id, quantity, tech_name, ts=0, wo_number=""):
    """Deduction from the PWA: deducts ONLY the truck's stock (never warehouse),
    allows negative stock (consumption is the reality; a reconciliation alert is
    raised instead of blocking the tech), and logs with source='pwa'.

    This mirrors the email-pipeline `deduct` for truck behavior but never
    blocks on insufficient stock (Option A per PRD v3 14.2)."""
    part = conn.execute("SELECT active, name FROM parts WHERE id=?", (part_id,)).fetchone()
    if not part:
        return None, f"Part '{part_id}' not found in catalog"
    if not part["active"]:
        return None, f"Part '{part_id}' is deactivated (not in current catalog)"

    conn.execute("BEGIN IMMEDIATE")
    truck_stock = conn.execute(
        "SELECT * FROM stock WHERE truck_id=? AND part_id=?",
        (truck_id, part_id),
    ).fetchone()

    if not truck_stock:
        conn.execute(
            "INSERT INTO stock (truck_id, part_id, quantity, min_threshold) VALUES (?, ?, ?, ?)",
            (truck_id, part_id, -quantity, 0),
        )
        stock_after = -quantity
    else:
        stock_after = truck_stock["quantity"] - quantity
        conn.execute(
            "UPDATE stock SET quantity=?, updated_at=datetime('now', 'localtime') WHERE truck_id=? AND part_id=?",
            (stock_after, truck_id, part_id),
        )

    invoice_id = f"pwa:{ts}" if ts else ""
    log_transaction(
        conn, tech_name=tech_name, truck_id=truck_id, warehouse_id=warehouse_id,
        part_id=part_id, quantity_change=-quantity,
        stock_before=(truck_stock["quantity"] if truck_stock else 0),
        stock_after=stock_after, invoice_id=invoice_id, confidence=1.0,
        raw_text="PWA deduction", source="pwa", format="", invoice_name="",
        wo_number=wo_number,
    )

    if stock_after < 0:
        _record_stock_alert(
            conn, invoice_id, part_id, part["name"], truck_id, warehouse_id,
            quantity, truck_stock["quantity"] if truck_stock else 0,
        )

    conn.commit()

    is_low = stock_after < (truck_stock["min_threshold"] if truck_stock else 0)
    return {
        "part_id": part_id,
        "truck_before": (truck_stock["quantity"] if truck_stock else 0),
        "truck_after": stock_after,
        "is_low": is_low,
    }, None


def restock(conn, truck_id, part_id, quantity, warehouse_id=None, source="restock"):
    if not warehouse_id:
        return None, "No warehouse specified. Select which warehouse to restock from."

    conn.execute("BEGIN IMMEDIATE")
    wh_stock = conn.execute(
        "SELECT * FROM warehouse_stock WHERE warehouse_id=? AND part_id=?",
        (warehouse_id, part_id),
    ).fetchone()

    if not wh_stock or wh_stock["quantity"] < quantity:
        conn.rollback()
        return None, "Insufficient warehouse stock"

    truck_stock = conn.execute(
        "SELECT * FROM stock WHERE truck_id=? AND part_id=?",
        (truck_id, part_id),
    ).fetchone()

    truck_before = truck_stock["quantity"] if truck_stock else 0
    wh_before = wh_stock["quantity"]

    conn.execute(
        "UPDATE warehouse_stock SET quantity=?, updated_at=datetime('now', 'localtime') WHERE warehouse_id=? AND part_id=?",
        (wh_before - quantity, warehouse_id, part_id),
    )

    if truck_stock:
        conn.execute(
            "UPDATE stock SET quantity=?, updated_at=datetime('now', 'localtime') WHERE truck_id=? AND part_id=?",
            (truck_before + quantity, truck_id, part_id),
        )
    else:
        conn.execute(
            "INSERT INTO stock (truck_id, part_id, quantity) VALUES (?, ?, ?)",
            (truck_id, part_id, quantity),
        )

    log_transaction(
        conn, tech_name="system", truck_id=truck_id, warehouse_id=warehouse_id,
        part_id=part_id, quantity_change=quantity,
        stock_before=truck_before,
        stock_after=truck_before + quantity, invoice_id="",
        confidence=1.0, source=source, format="",
    )

    conn.commit()

    return {
        "part_id": part_id,
        "truck_before": truck_before,
        "truck_after": truck_before + quantity,
        "wh_before": wh_before,
        "wh_after": wh_before - quantity,
    }, None


def adjust(conn, location_type, location_id, part_id, new_quantity, reason="manual_adjustment"):
    if location_type == "truck":
        table = "stock"
        id_col = "truck_id"
    elif location_type == "warehouse":
        table = "warehouse_stock"
        id_col = "warehouse_id"
    else:
        return None, "Invalid location type"

    conn.execute("BEGIN IMMEDIATE")
    current = conn.execute(
        f"SELECT * FROM {table} WHERE {id_col}=? AND part_id=?",
        (location_id, part_id),
    ).fetchone()

    if not current:
        conn.rollback()
        return None, f"Stock entry not found for {location_type} {location_id}, part {part_id}"

    old_quantity = current["quantity"]
    delta = new_quantity - old_quantity

    conn.execute(
        f"UPDATE {table} SET quantity=?, updated_at=datetime('now', 'localtime') WHERE {id_col}=? AND part_id=?",
        (new_quantity, location_id, part_id),
    )

    log_transaction(
        conn, tech_name="system", truck_id=location_id if location_type == "truck" else "",
        warehouse_id=location_id if location_type == "warehouse" else "", part_id=part_id,
        quantity_change=delta,
        stock_before=old_quantity, stock_after=new_quantity,
        invoice_id="", confidence=1.0, source=reason, format="",
    )

    conn.commit()
    if location_type == "warehouse":
        _maybe_auto_resolve_reorders(conn)

    return {
        "part_id": part_id,
        "old_quantity": old_quantity,
        "new_quantity": new_quantity,
        "delta": delta,
    }, None


def add_warehouse_stock_entry(conn, warehouse_id, part_id, quantity, min_threshold=0):
    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT * FROM warehouse_stock WHERE warehouse_id=? AND part_id=?",
        (warehouse_id, part_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE warehouse_stock SET quantity=?, min_threshold=?, updated_at=datetime('now', 'localtime') WHERE warehouse_id=? AND part_id=?",
            (quantity, min_threshold, warehouse_id, part_id),
        )
        old_qty = existing["quantity"]
    else:
        conn.execute(
            "INSERT INTO warehouse_stock (warehouse_id, part_id, quantity, min_threshold) VALUES (?, ?, ?, ?)",
            (warehouse_id, part_id, quantity, min_threshold),
        )
        old_qty = 0
    log_transaction(
        conn, tech_name="system", truck_id="", warehouse_id=warehouse_id,
        part_id=part_id, quantity_change=quantity - old_qty,
        stock_before=old_qty, stock_after=quantity,
        invoice_id="", confidence=1.0, source="manual_assign", format="",
    )
    conn.commit()
    _maybe_auto_resolve_reorders(conn)
    return {"part_id": part_id, "old_quantity": old_qty, "new_quantity": quantity}, None


def set_truck_stock_entry(conn, truck_id, part_id, quantity, min_threshold=0, source="manual_assign"):
    """Set a truck's stock for a part. Stock moves from (or returns to) the
    truck's warehouse; blocks if the warehouse lacks enough stock.
    Audit Adjust (inventory.adjust) is the escape hatch for corrections
    that should NOT touch the warehouse."""
    truck = conn.execute("SELECT * FROM trucks WHERE id=?", (truck_id,)).fetchone()
    if not truck:
        return None, f"Truck {truck_id} not found"
    warehouse_id = truck["warehouse_id"]

    part = conn.execute("SELECT name FROM parts WHERE id=?", (part_id,)).fetchone()
    part_name = part["name"] if part else part_id

    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        "SELECT * FROM stock WHERE truck_id=? AND part_id=?",
        (truck_id, part_id),
    ).fetchone()
    old_qty = existing["quantity"] if existing else 0
    delta = quantity - old_qty

    if warehouse_id and delta != 0:
        wh = conn.execute(
            "SELECT * FROM warehouse_stock WHERE warehouse_id=? AND part_id=?",
            (warehouse_id, part_id),
        ).fetchone()
        wh_qty = wh["quantity"] if wh else 0
        if delta > 0:
            if wh_qty < delta:
                conn.rollback()
                return None, (
                    f"Insufficient warehouse stock: {warehouse_id} has {wh_qty} of "
                    f"'{part_name}', truck {truck_id} needs {delta} more (target {quantity}). "
                    f"Assign to the warehouse first, or use Audit Adjust to correct values."
                )
            conn.execute(
                "UPDATE warehouse_stock SET quantity=?, updated_at=datetime('now', 'localtime') WHERE warehouse_id=? AND part_id=?",
                (wh_qty - delta, warehouse_id, part_id),
            )
        else:
            conn.execute(
                "UPDATE warehouse_stock SET quantity=quantity+?, updated_at=datetime('now', 'localtime') WHERE warehouse_id=? AND part_id=?",
                (-delta, warehouse_id, part_id),
            )

    if existing:
        conn.execute(
            "UPDATE stock SET quantity=?, min_threshold=?, updated_at=datetime('now', 'localtime') WHERE truck_id=? AND part_id=?",
            (quantity, min_threshold, truck_id, part_id),
        )
    else:
        conn.execute(
            "INSERT INTO stock (truck_id, part_id, quantity, min_threshold) VALUES (?, ?, ?, ?)",
            (truck_id, part_id, quantity, min_threshold),
        )
    log_transaction(
        conn, tech_name="system", truck_id=truck_id, warehouse_id=warehouse_id or "",
        part_id=part_id, quantity_change=delta,
        stock_before=old_qty, stock_after=quantity,
        invoice_id="", confidence=1.0, source=source, format="",
    )
    conn.commit()
    if warehouse_id:
        _maybe_auto_resolve_reorders(conn)
    return {"part_id": part_id, "old_quantity": old_qty, "new_quantity": quantity}, None


def remove_from_warehouse(conn, warehouse_id, part_id):
    conn.execute(
        "DELETE FROM warehouse_stock WHERE warehouse_id=? AND part_id=?",
        (warehouse_id, part_id),
    )
    conn.commit()
    return {"removed": True}, None


def set_audit_entry(conn, location_type, location_id, part_id, quantity, min_threshold=0, reason="manual_adjustment"):
    """Directly create or update a stock entry at a location WITHOUT moving
    stock from/to the related warehouse. Used by batch/row Audit Adjust."""
    if location_type == "truck":
        table, id_col = "stock", "truck_id"
    elif location_type == "warehouse":
        table, id_col = "warehouse_stock", "warehouse_id"
    else:
        return None, "Invalid location type"

    conn.execute("BEGIN IMMEDIATE")
    existing = conn.execute(
        f"SELECT * FROM {table} WHERE {id_col}=? AND part_id=?", (location_id, part_id)
    ).fetchone()
    old_qty = existing["quantity"] if existing else 0
    if existing:
        conn.execute(
            f"UPDATE {table} SET quantity=?, min_threshold=?, updated_at=datetime('now', 'localtime') WHERE {id_col}=? AND part_id=?",
            (quantity, min_threshold, location_id, part_id),
        )
    else:
        if location_type == "truck":
            conn.execute(
                "INSERT INTO stock (truck_id, part_id, quantity, min_threshold) VALUES (?, ?, ?, ?)",
                (location_id, part_id, quantity, min_threshold),
            )
        else:
            conn.execute(
                "INSERT INTO warehouse_stock (warehouse_id, part_id, quantity, min_threshold) VALUES (?, ?, ?, ?)",
                (location_id, part_id, quantity, min_threshold),
            )
    log_transaction(
        conn, tech_name="system",
        truck_id=location_id if location_type == "truck" else "",
        warehouse_id=location_id if location_type == "warehouse" else "",
        part_id=part_id, quantity_change=quantity - old_qty,
        stock_before=old_qty, stock_after=quantity,
        invoice_id="", confidence=1.0, source=reason, format="",
    )
    conn.commit()
    if location_type == "warehouse":
        _maybe_auto_resolve_reorders(conn)
    return {"part_id": part_id, "old_quantity": old_qty, "new_quantity": quantity}, None


def bulk_restock_truck(conn, truck_id, warehouse_id=None):
    truck = conn.execute("SELECT * FROM trucks WHERE id=?", (truck_id,)).fetchone()
    if not truck:
        return None, "Truck not found"
    if not warehouse_id:
        warehouse_id = truck["warehouse_id"]
    if not warehouse_id:
        return None, "Truck has no warehouse assigned"
    low_items = conn.execute("""
        SELECT s.*, p.name AS part_name
        FROM stock s
        JOIN parts p ON p.id = s.part_id
        WHERE s.truck_id=? AND s.quantity < s.min_threshold AND p.active=1
        ORDER BY p.name
    """, (truck_id,)).fetchall()

    results = []
    errors = []
    for item in low_items:
        needed = item["min_threshold"] - item["quantity"]
        if needed <= 0:
            continue
        result, err = restock(conn, truck_id, item["part_id"], needed, warehouse_id, source="bulk_restock")
        if err:
            errors.append({"part_id": item["part_id"], "error": err})
        else:
            results.append(result)
    return {"restocked": len(results), "errors": errors}, None


def check_thresholds(conn, warehouse_id=None):
    wh_clause = ""
    wh_params = []
    if warehouse_id and warehouse_id != "all":
        wh_clause = " AND t.warehouse_id=?"
        wh_params.append(warehouse_id)
    low_items = conn.execute(f"""
        SELECT s.*, p.name AS part_name, t.name AS truck_name, t.id AS truck_id, t.warehouse_id
        FROM stock s
        JOIN parts p ON p.id = s.part_id
        JOIN trucks t ON t.id = s.truck_id
        WHERE s.quantity < s.min_threshold AND p.active=1{wh_clause}
        ORDER BY s.truck_id, p.name
    """, wh_params).fetchall()

    wh_clause2 = ""
    wh_params2 = []
    if warehouse_id and warehouse_id != "all":
        wh_clause2 = " AND ws.warehouse_id=?"
        wh_params2.append(warehouse_id)
    low_wh = conn.execute(f"""
        SELECT ws.*, p.name AS part_name, w.name AS warehouse_name
        FROM warehouse_stock ws
        JOIN parts p ON p.id = ws.part_id
        JOIN warehouses w ON w.id = ws.warehouse_id
        WHERE ws.quantity < ws.min_threshold AND p.active=1{wh_clause2}
        ORDER BY ws.warehouse_id, p.name
    """, wh_params2).fetchall()

    return {"truck_items": low_items, "warehouse_items": low_wh}
