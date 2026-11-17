"""Distributor management — buying channels (dist_a..dist_d + amazon are
seeded as placeholders; new vendors are created from the dashboard).

Each part can have a mapping row per distributor (distributor_sku +
distributor_part_name) and exactly ONE active distributor. The active
distributor decides who gets the reorder email for that part. Mapping data is
purchase-only: it never touches invoice matching (parts.aliases).
"""
import csv
import io


def list_distributors(conn):
    return conn.execute("SELECT * FROM distributors ORDER BY id").fetchall()


def get_distributor(conn, distributor_id):
    return conn.execute("SELECT * FROM distributors WHERE id=?", (distributor_id,)).fetchone()


def create_distributor(conn, distributor_id, name, email=""):
    """Insert a new distributor row. Raises sqlite3.IntegrityError on dup id."""
    conn.execute(
        "INSERT INTO distributors (id, name, email) VALUES (?, ?, ?)",
        (distributor_id, name.strip(), (email or "").strip()),
    )
    conn.commit()
    return get_distributor(conn, distributor_id)


def update_distributor(conn, distributor_id, name=None, email=None):
    fields, params = [], []
    if name is not None:
        fields.append("name=?")
        params.append(name)
    if email is not None:
        fields.append("email=?")
        params.append(email.strip())
    if fields:
        params.append(distributor_id)
        conn.execute(f"UPDATE distributors SET {', '.join(fields)} WHERE id=?", params)
        conn.commit()
    return get_distributor(conn, distributor_id)


def get_mapping(conn, product_id, distributor_id):
    return conn.execute(
        "SELECT * FROM product_distributors WHERE product_id=? AND distributor_id=?",
        (product_id, distributor_id),
    ).fetchone()


def upsert_mapping(conn, product_id, distributor_id, sku="", part_name="", is_active=None):
    existing = get_mapping(conn, product_id, distributor_id)
    if existing:
        conn.execute(
            """UPDATE product_distributors
               SET distributor_sku=?, distributor_part_name=?,
                   updated_at=datetime('now', 'localtime')
               WHERE product_id=? AND distributor_id=?""",
            (sku, part_name, product_id, distributor_id),
        )
    else:
        conn.execute(
            """INSERT INTO product_distributors
               (product_id, distributor_id, distributor_sku, distributor_part_name, is_active)
               VALUES (?, ?, ?, ?, 0)""",
            (product_id, distributor_id, sku, part_name),
        )
    if is_active is not None:
        _set_active_only(conn, product_id, distributor_id, is_active)
    conn.commit()
    return get_mapping(conn, product_id, distributor_id)


def _set_active_only(conn, product_id, distributor_id, is_active):
    """Enforce the single-active-distributor rule per product."""
    if is_active:
        conn.execute(
            "UPDATE product_distributors SET is_active=0, updated_at=datetime('now', 'localtime') "
            "WHERE product_id=?",
            (product_id,),
        )
        conn.execute(
            "UPDATE product_distributors SET is_active=1, updated_at=datetime('now', 'localtime') "
            "WHERE product_id=? AND distributor_id=?",
            (product_id, distributor_id),
        )


def set_active(conn, product_id, distributor_id):
    """Mark one distributor active for a product (turns the others off)."""
    existing = get_mapping(conn, product_id, distributor_id)
    if not existing:
        upsert_mapping(conn, product_id, distributor_id)
    _set_active_only(conn, product_id, distributor_id, True)
    conn.commit()
    return get_mapping(conn, product_id, distributor_id)


def list_mappings_for_product(conn, product_id):
    rows = conn.execute(
        """SELECT pd.*, d.name AS distributor_name, d.email AS distributor_email
           FROM product_distributors pd
           JOIN distributors d ON d.id = pd.distributor_id
           WHERE pd.product_id=? ORDER BY pd.distributor_id""",
        (product_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def batch_set_active(conn, product_ids, distributor_id):
    """Set the same distributor as active for many products."""
    for pid in product_ids:
        set_active(conn, pid, distributor_id)
    return len(product_ids)


def import_distributor_csv(conn, distributor_id, csv_data):
    """Import a per-distributor mapping file: Product SKU, Distributor SKU,
    Distributor Part Name. Returns created/updated counts plus warnings for
    DoorFlex SKUs that don't exist in the parts catalog. Distributors are now
    dynamic, so the only validation is that the distributor row exists."""
    results = {"created": 0, "updated": 0, "warnings": [], "errors": []}
    if not get_distributor(conn, distributor_id):
        results["errors"].append({"row": 0, "reason": f"Unknown distributor '{distributor_id}'"})
        return results

    reader = csv.DictReader(io.StringIO(csv_data))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    if "product sku" not in headers and "sku" not in headers:
        results["errors"].append({
            "row": 0,
            "reason": "Missing 'Product SKU' column (the DoorFlex part id).",
        })
        return results

    for row_num, row in enumerate(reader, start=2):
        try:
            product_sku = (row.get("Product SKU") or row.get("sku") or "").strip()
            if not product_sku:
                results["errors"].append({"row": row_num, "reason": "Missing Product SKU"})
                continue
            part = conn.execute("SELECT id, name FROM parts WHERE id=?", (product_sku,)).fetchone()
            if not part:
                results["warnings"].append(
                    f"Row {row_num}: DoorFlex SKU '{product_sku}' not in parts catalog — skipped."
                )
                continue
            d_sku = (row.get("Distributor SKU") or row.get("Distributor Sku") or "").strip()
            d_name = (row.get("Distributor Part Name") or "").strip()
            existing = get_mapping(conn, product_sku, distributor_id)
            upsert_mapping(conn, product_sku, distributor_id, sku=d_sku, part_name=d_name)
            if existing:
                results["updated"] += 1
            else:
                results["created"] += 1
        except Exception as e:
            results["errors"].append({"row": row_num, "reason": str(e)})

    conn.commit()
    return results
