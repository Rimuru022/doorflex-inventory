import re


def _slug(name):
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s


def list_warehouses(conn):
    return conn.execute(
        """SELECT w.*,
                  (SELECT COUNT(*) FROM trucks t WHERE t.warehouse_id=w.id AND t.active=1) AS truck_count,
                  (SELECT COUNT(*) FROM warehouse_stock ws WHERE ws.warehouse_id=w.id) AS part_count
           FROM warehouses w
           WHERE w.active=1
           ORDER BY w.name"""
    ).fetchall()


def get_warehouse(conn, warehouse_id):
    return conn.execute("SELECT * FROM warehouses WHERE id=?", (warehouse_id,)).fetchone()


def create_warehouse(conn, name):
    wh_id = _slug(name)
    if not wh_id:
        return None, "Warehouse name cannot be empty"
    existing = conn.execute("SELECT * FROM warehouses WHERE id=?", (wh_id,)).fetchone()
    if existing:
        if not existing["active"]:
            conn.execute("UPDATE warehouses SET active=1, name=? WHERE id=?", (name, wh_id))
            conn.commit()
        return get_warehouse(conn, wh_id), None
    conn.execute("INSERT INTO warehouses (id, name) VALUES (?, ?)", (wh_id, name))
    conn.commit()
    return get_warehouse(conn, wh_id), None


def rename_warehouse(conn, warehouse_id, name):
    name = name.strip()
    if not name:
        return None, "Warehouse name cannot be empty"
    conn.execute("UPDATE warehouses SET name=? WHERE id=?", (name, warehouse_id))
    conn.commit()
    return get_warehouse(conn, warehouse_id), None


def deactivate_warehouse(conn, warehouse_id):
    active_trucks = conn.execute(
        "SELECT COUNT(*) FROM trucks WHERE warehouse_id=? AND active=1", (warehouse_id,)
    ).fetchone()[0]
    if active_trucks:
        return None, "Cannot delete warehouse with active trucks — move or delete them first"
    conn.execute("UPDATE warehouses SET active=0 WHERE id=?", (warehouse_id,))
    conn.commit()
    return {"deactivated": warehouse_id}, None
