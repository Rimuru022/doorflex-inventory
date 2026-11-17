import difflib


def list_techs(conn):
    return conn.execute("""
        SELECT t.*, w.name AS warehouse_name
        FROM techs t
        LEFT JOIN warehouses w ON w.id = t.warehouse_id
        WHERE t.active=1
        ORDER BY t.name
    """).fetchall()


def get_tech(conn, tech_id):
    return conn.execute("SELECT * FROM techs WHERE id=?", (tech_id,)).fetchone()


def create_tech(conn, name, truck_id, warehouse_id):
    cursor = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id) VALUES (?, ?, ?)",
        (name, truck_id, warehouse_id),
    )
    conn.commit()
    return get_tech(conn, cursor.lastrowid)


def update_tech(conn, tech_id, truck_id=None, warehouse_id=None, name=None):
    fields = []
    params = []
    if truck_id is not None:
        fields.append("truck_id=?")
        params.append(truck_id)
    if warehouse_id is not None:
        fields.append("warehouse_id=?")
        params.append(warehouse_id)
    if name is not None and name.strip():
        fields.append("name=?")
        params.append(name.strip())
    if not fields:
        return get_tech(conn, tech_id)
    params.append(tech_id)
    conn.execute(f"UPDATE techs SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    return get_tech(conn, tech_id)


def deactivate_tech(conn, tech_id):
    conn.execute("UPDATE techs SET active=0 WHERE id=?", (tech_id,))
    conn.commit()
    return get_tech(conn, tech_id)


def find_tech_by_name(conn, name):
    """Return (tech, confidence). A tech is only auto-assigned on an EXACT
    (case-insensitive, trimmed) name match — fuzzy matches require manual
    confirmation. Returns (None, best_ratio) when the name is not exact."""
    name = name.strip().lower()
    techs = conn.execute("SELECT * FROM techs WHERE active=1").fetchall()
    for tech in techs:
        if tech["name"].strip().lower() == name:
            return tech, 1.0

    best_ratio = 0
    for tech in techs:
        tech_lower = tech["name"].strip().lower()
        ratio = difflib.SequenceMatcher(None, name, tech_lower).ratio()
        name_parts = name.split()
        tech_parts = tech_lower.split()
        shared = len(set(name_parts) & set(tech_parts))
        if shared > 0:
            ratio = max(ratio, shared / max(len(name_parts), len(tech_parts)))
        best_ratio = max(best_ratio, ratio)

    return None, round(best_ratio, 2)


def _score_tech(tech, name):
    tech_lower = tech["name"].strip().lower()
    ratio = difflib.SequenceMatcher(None, name, tech_lower).ratio()
    name_parts = name.split()
    tech_parts = tech_lower.split()
    shared = len(set(name_parts) & set(tech_parts))
    if shared > 0:
        ratio = max(ratio, shared / max(len(name_parts), len(tech_parts)))
    return round(ratio, 2)


def score_techs_against(active_techs, name):
    """Score a pre-fetched list of active techs against `name`, best first.
    Use this when scoring many names against the same roster to avoid
    repeated SELECTs."""
    name = name.strip().lower()
    scored = [(_score_tech(t, name), t) for t in active_techs]
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored
