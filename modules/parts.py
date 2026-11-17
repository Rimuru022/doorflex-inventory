import json
import csv
import io
import re


def _slug(name):
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return s


def list_parts(conn):
    return conn.execute("SELECT * FROM parts WHERE active=1 ORDER BY name").fetchall()


def get_part(conn, part_id):
    return conn.execute("SELECT * FROM parts WHERE id=?", (part_id,)).fetchone()


def create_part(conn, part_id, name, aliases=None, unit="piece", category="", subcategory="",
                hand="", length_size="", brand="", supplier="", cost=0, notes="",
                amazon_sku=""):
    if aliases is None or not isinstance(aliases, list):
        aliases = []
    conn.execute(
        """INSERT INTO parts (id, name, aliases, unit, category, subcategory, hand,
           length_size, brand, supplier, cost, notes, amazon_sku)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (part_id, name, json.dumps(aliases), unit, category, subcategory,
         hand, length_size, brand, supplier, cost, notes, amazon_sku),
    )
    conn.commit()
    return get_part(conn, part_id)


def update_part(conn, part_id, name=None, unit=None, aliases=None, category=None,
                subcategory=None, hand=None, length_size=None, brand=None,
                supplier=None, cost=None, notes=None, amazon_sku=None):
    fields = []
    params = []
    for field, value in [
        ("name", name), ("unit", unit), ("category", category),
        ("subcategory", subcategory), ("hand", hand), ("length_size", length_size),
        ("brand", brand), ("supplier", supplier), ("notes", notes),
    ]:
        if value is not None:
            fields.append(f"{field}=?")
            params.append(value)
    if amazon_sku is not None:
        fields.append("amazon_sku=?")
        params.append(amazon_sku)
    if aliases is not None:
        fields.append("aliases=?")
        params.append(json.dumps(aliases))
    if cost is not None:
        fields.append("cost=?")
        params.append(float(cost))
    if not fields:
        return get_part(conn, part_id)
    params.append(part_id)
    conn.execute(f"UPDATE parts SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    return get_part(conn, part_id)


def add_alias(conn, part_id, alias):
    part = get_part(conn, part_id)
    if not part:
        return None
    aliases = json.loads(part["aliases"])
    if alias not in aliases:
        aliases.append(alias)
        conn.execute("UPDATE parts SET aliases=? WHERE id=?", (json.dumps(aliases), part_id))
        conn.commit()
    return get_part(conn, part_id)


def bulk_add_aliases(conn, part_id, aliases_csv):
    """Add many aliases at once from a comma-separated string. Trims, drops
    empties, dedupes against the existing alias list. Returns the new alias
    list, or None if the part does not exist."""
    part = get_part(conn, part_id)
    if not part:
        return None
    existing = json.loads(part["aliases"])
    seen = {a.strip().lower(): a for a in existing if a.strip()}
    order = [a for a in existing if a.strip()]
    for raw in (aliases_csv or "").split(","):
        a = raw.strip()
        if not a:
            continue
        key = a.lower()
        if key in seen:
            continue
        seen[key] = a
        order.append(a)
    conn.execute(
        "UPDATE parts SET aliases=? WHERE id=?",
        (json.dumps(order), part_id),
    )
    conn.commit()
    return order


def remove_alias(conn, part_id, alias):
    """Remove a single alias. Returns True if removed, False if the part is
    missing or the alias was not present."""
    part = get_part(conn, part_id)
    if not part:
        return False
    existing = json.loads(part["aliases"])
    target = alias.strip()
    new_list = [a for a in existing if a != target]
    if len(new_list) == len(existing):
        return False
    conn.execute(
        "UPDATE parts SET aliases=? WHERE id=?",
        (json.dumps(new_list), part_id),
    )
    conn.commit()
    return True


def bulk_import_csv(conn, csv_data):
    results = {"created": 0, "updated": 0, "removed": 0, "errors": [], "warnings": []}
    imported_ids = set()
    reader = csv.DictReader(io.StringIO(csv_data))
    headers = reader.fieldnames or []

    warehouses = conn.execute("SELECT id, name FROM warehouses WHERE active=1").fetchall()
    wh_by_id = {w["id"].lower(): w["id"] for w in warehouses}
    wh_by_name = {w["name"].strip().lower(): w["id"] for w in warehouses}
    trucks = conn.execute("SELECT id, name FROM trucks WHERE active=1").fetchall()
    tr_by_id = {t["id"].lower(): t["id"] for t in trucks}
    tr_by_name = {t["name"].strip().lower(): t["id"] for t in trucks}

    def resolve_warehouse(value):
        if not value:
            return None
        v = value.strip()
        if v.lower() in wh_by_id:
            return wh_by_id[v.lower()]
        if v.lower() in wh_by_name:
            return wh_by_name[v.lower()]
        return False

    def resolve_truck(value):
        if not value:
            return None
        v = value.strip()
        if v.lower() in tr_by_id:
            return tr_by_id[v.lower()]
        if v.lower() in tr_by_name:
            return tr_by_name[v.lower()]
        return False

    for row_num, row in enumerate(reader, start=2):
        try:
            name = row.get("Product Name", row.get("name", "")).strip()
            if not name:
                results["errors"].append({"row": row_num, "reason": "Missing Product Name"})
                continue

            sku = row.get("sku", row.get("SKU", _slug(name))).strip()
            if not sku:
                sku = _slug(name)
            imported_ids.add(sku)

            category = row.get("Category", "").strip()
            subcategory = row.get("Subcategory", "").strip()
            hand = row.get("Hand", "").strip()
            length_size = row.get("Length/Size", "").strip()
            brand = row.get("Brand", "").strip()
            supplier = row.get("Supplier", "").strip()
            unit = row.get("unit", row.get("Unit", "piece")).strip()
            notes = row.get("Notes", "").strip()

            cost_raw = row.get("Cost", "").strip()
            cost = float(cost_raw.replace("$", "").replace(",", "")) if cost_raw else 0
            amazon_sku = row.get("Amazon URL", row.get("Amazon", "")).strip()

            qty_raw = row.get("Qty On Hand", "").strip()
            qty = int(qty_raw) if qty_raw else 0
            min_raw = row.get("Min Qty", "").strip()
            min_qty = int(min_raw) if min_raw else 0
            warehouse_raw = row.get("Warehouse", "").strip()
            truck_raw = row.get("Truck", "").strip()

            existing = conn.execute("SELECT * FROM parts WHERE id=?", (sku,)).fetchone()
            if existing:
                update_part(conn, sku, name=name, unit=unit, category=category,
                            subcategory=subcategory, hand=hand, length_size=length_size,
                            brand=brand, supplier=supplier, cost=cost, notes=notes,
                            amazon_sku=amazon_sku)
                if not existing["active"]:
                    conn.execute("UPDATE parts SET active=1 WHERE id=?", (sku,))
                results["updated"] += 1
            else:
                create_part(conn, sku, name, unit=unit, category=category,
                            subcategory=subcategory, hand=hand, length_size=length_size,
                            brand=brand, supplier=supplier, cost=cost, notes=notes,
                            amazon_sku=amazon_sku)
                results["created"] += 1

            if qty and warehouse_raw:
                wh_id = resolve_warehouse(warehouse_raw)
                if wh_id is False:
                    valid = ", ".join(f"{w['id']} ({w['name']})" for w in warehouses)
                    results["warnings"].append(
                        f"Row {row_num}: Warehouse '{warehouse_raw}' not found, stock not assigned. "
                        f"Valid warehouses: {valid or 'none'}"
                    )
                elif wh_id:
                    conn.execute(
                        """INSERT INTO warehouse_stock (warehouse_id, part_id, quantity, min_threshold)
                           VALUES (?, ?, ?, ?) ON CONFLICT(warehouse_id, part_id)
                           DO UPDATE SET quantity=?, min_threshold=?,
                           updated_at=datetime('now', 'localtime')""",
                        (wh_id, sku, qty, min_qty, qty, min_qty),
                    )
            if qty and truck_raw:
                tr_id = resolve_truck(truck_raw)
                if tr_id is False:
                    valid = ", ".join(f"{t['id']} ({t['name']})" for t in trucks)
                    results["warnings"].append(
                        f"Row {row_num}: Truck '{truck_raw}' not found, stock not assigned. "
                        f"Valid trucks: {valid or 'none'}"
                    )
                elif tr_id:
                    conn.execute(
                        """INSERT INTO stock (truck_id, part_id, quantity, min_threshold)
                           VALUES (?, ?, ?, ?) ON CONFLICT(truck_id, part_id)
                           DO UPDATE SET quantity=?, min_threshold=?,
                           updated_at=datetime('now', 'localtime')""",
                        (tr_id, sku, qty, min_qty, qty, min_qty),
                    )

        except Exception as e:
            results["errors"].append({"row": row_num, "reason": str(e)})

    conn.commit()
    try:
        from modules.reorder import auto_resolve_by_stock
        auto_resolve_by_stock(conn)
    except ImportError:
        pass

    return results


def bulk_import_xlsx(conn, xlsx_bytes):
    import io as _io
    from openpyxl import load_workbook
    wb = load_workbook(_io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    ws = wb.active
    headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))] if ws else []
    if not headers:
        return {"created": 0, "updated": 0, "errors": [{"row": 0, "reason": "Empty spreadsheet"}]}

    output = _io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in ws.iter_rows(min_row=2, values_only=True):
        writer.writerow(row)
    output.seek(0)
    wb.close()
    return bulk_import_csv(conn, output.read())


def deactivate_part(conn, part_id):
    conn.execute("UPDATE parts SET active=0 WHERE id=?", (part_id,))
    conn.commit()
    return get_part(conn, part_id)
