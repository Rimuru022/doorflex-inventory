def list_trucks(conn):
    return conn.execute("""
        SELECT t.*, w.name AS warehouse_name
        FROM trucks t
        LEFT JOIN warehouses w ON w.id = t.warehouse_id
        WHERE t.active=1
        ORDER BY t.id
    """).fetchall()


def get_truck(conn, truck_id):
    return conn.execute("""
        SELECT t.*, w.name AS warehouse_name
        FROM trucks t
        LEFT JOIN warehouses w ON w.id = t.warehouse_id
        WHERE t.id=?
    """, (truck_id,)).fetchone()


def create_truck(conn, truck_id, name, warehouse_id=None):
    conn.execute(
        "INSERT INTO trucks (id, name, warehouse_id) VALUES (?, ?, ?)",
        (truck_id, name, warehouse_id),
    )
    conn.commit()
    return get_truck(conn, truck_id)


def update_truck(conn, truck_id, name=None, warehouse_id=None):
    fields = []
    params = []
    if name is not None:
        fields.append("name=?")
        params.append(name)
    if warehouse_id is not None:
        fields.append("warehouse_id=?")
        params.append(warehouse_id)
    if not fields:
        return get_truck(conn, truck_id)
    params.append(truck_id)
    conn.execute(f"UPDATE trucks SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    return get_truck(conn, truck_id)


def deactivate_truck(conn, truck_id):
    conn.execute("UPDATE trucks SET active=0 WHERE id=?", (truck_id,))
    conn.commit()
    return get_truck(conn, truck_id)
