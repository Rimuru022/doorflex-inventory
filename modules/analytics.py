def top_movers(conn, warehouse_id=None, days=30, limit=10, order="desc"):
    # Usage regardless of origin — distributor-invoice deductions (source=
    # 'email') and tech PWA deductions (source='pwa') are both real stock
    # movement, so both count toward "top movers."
    where = "timestamp >= datetime('now', ?)"
    params = [f"-{days} days"]
    if warehouse_id and warehouse_id != "all":
        where += " AND warehouse_id=?"
        params.append(warehouse_id)
    order_sql = "DESC" if order == "desc" else "ASC"
    return conn.execute(
        f"""SELECT part_id, SUM(ABS(quantity_change)) AS total_used
            FROM log WHERE {where}
            GROUP BY part_id ORDER BY total_used {order_sql} LIMIT ?""",
        params + [limit],
    ).fetchall()


def usage_by_truck(conn, warehouse_id, days=30):
    return conn.execute(
        """SELECT l.truck_id, t.name AS truck_name,
                  COUNT(DISTINCT l.id) AS line_items,
                  SUM(ABS(l.quantity_change)) AS total_qty_used
           FROM log l
           JOIN trucks t ON t.id = l.truck_id
           WHERE l.warehouse_id=?
             AND l.timestamp >= datetime('now', ?)
           GROUP BY l.truck_id, t.name
           ORDER BY total_qty_used DESC""",
        (warehouse_id, f"-{days} days"),
    ).fetchall()


def usage_by_part(conn, truck_id, days=30):
    return conn.execute(
        """SELECT l.part_id, p.name AS part_name,
                  SUM(ABS(l.quantity_change)) AS total_qty,
                  MAX(l.timestamp) AS last_used
           FROM log l
           JOIN parts p ON p.id = l.part_id
           WHERE l.truck_id=?
             AND l.timestamp >= datetime('now', ?)
           GROUP BY l.part_id, p.name
           ORDER BY total_qty DESC, p.name""",
        (truck_id, f"-{days} days"),
    ).fetchall()


def usage_by_tech(conn, warehouse_id, days=30):
    return conn.execute(
        """SELECT l.tech_name,
                  COUNT(DISTINCT l.id) AS line_items,
                  SUM(ABS(l.quantity_change)) AS total_qty_used
           FROM log l
           WHERE l.warehouse_id=?
             AND l.tech_name != ''
             AND LOWER(l.tech_name) != 'system'
             AND l.timestamp >= datetime('now', ?)
           GROUP BY l.tech_name
           ORDER BY total_qty_used DESC, l.tech_name""",
        (warehouse_id, f"-{days} days"),
    ).fetchall()


def usage_by_part_for_tech(conn, tech_name, days=30):
    return conn.execute(
        """SELECT l.part_id, p.name AS part_name,
                  SUM(ABS(l.quantity_change)) AS total_qty,
                  MAX(l.timestamp) AS last_used
           FROM log l
           JOIN parts p ON p.id = l.part_id
           WHERE l.tech_name=?
             AND l.timestamp >= datetime('now', ?)
           GROUP BY l.part_id, p.name
           ORDER BY total_qty DESC, p.name""",
        (tech_name, f"-{days} days"),
    ).fetchall()
