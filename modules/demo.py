"""Synthetic demo data for the Winston-tech PWA demo (PRD v3 M10).

Safe to run on any DB: creates the T-DEMO truck, the Winston tech (deduction
source = 'pwa' with a printed install token), and 3-5 synthetic parts with
truck + warehouse stock. Idempotent: rerunning updates instead of duplicating.

Run with:  python -m modules.demo
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import get_db  # noqa: E402
from modules import pwa  # noqa: E402

DEMO_TRUCK = "T-DEMO"
DEMO_TECH_NAME = "Winston"
DEMO_WAREHOUSE = "wh-sub"

DEMO_PARTS = [
    # (part_id, name, aliases, truck_qty, truck_min, wh_qty, wh_min)
    ("SPR-207", "207x2x31-RED Spring", ["spring 207", "207x2x31 red"], 8, 3, 12, 6),
    ("SPR-185", "185x1.75x27 Spring", ["spring 185", "185 spring"], 6, 3, 10, 6),
    ("CAB-18FT", "18ft Cable", ["cable 18ft", "18 ft cable"], 10, 4, 20, 8),
    ("RLR-2IN-NYLON-LS", "2in Nylon Roller (LS)", ["2in roller", "nylon roller"], 14, 5, 24, 10),
    ("HNG-14GA", "14ga Hinge", ["hinge 14ga", "14 ga hinge"], 16, 6, 28, 12),
]


def seed_demo(conn, print_token=True):
    warehouses = {r["id"] for r in conn.execute("SELECT id FROM warehouses").fetchall()}
    if DEMO_WAREHOUSE not in warehouses:
        raise RuntimeError(
            f"Warehouse '{DEMO_WAREHOUSE}' missing — run database.init_db() first.")

    conn.execute(
        "INSERT INTO trucks (id, name, warehouse_id) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET warehouse_id=excluded.warehouse_id",
        (DEMO_TRUCK, "Truck Demo (Winston)", DEMO_WAREHOUSE),
    )

    tech = conn.execute("SELECT * FROM techs WHERE name=? COLLATE NOCASE", (DEMO_TECH_NAME,)).fetchone()
    token = None
    if not tech:
        cur = conn.execute(
            "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
            "VALUES (?, ?, ?, 'pwa')",
            (DEMO_TECH_NAME, DEMO_TRUCK, DEMO_WAREHOUSE),
        )
        tech_id = cur.lastrowid
    else:
        tech_id = tech["id"]
        conn.execute(
            "UPDATE techs SET truck_id=?, warehouse_id=?, active=1, deduction_source='pwa' WHERE id=?",
            (DEMO_TRUCK, DEMO_WAREHOUSE, tech_id),
        )

    token = pwa.rotate_tech_token(conn, tech_id)

    for part_id, name, aliases, tqty, tmin, wqty, wmin in DEMO_PARTS:
        conn.execute(
            """INSERT INTO parts (id, name, aliases, unit, category, active)
               VALUES (?, ?, ?, 'piece', 'Demo', 1)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                   aliases=excluded.aliases, active=1, category='Demo'""",
            (part_id, name, __import__("json").dumps(aliases)),
        )
        conn.execute(
            """INSERT INTO stock (truck_id, part_id, quantity, min_threshold)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(truck_id, part_id) DO UPDATE SET quantity=?, min_threshold=?,
                   updated_at=datetime('now', 'localtime')""",
            (DEMO_TRUCK, part_id, tqty, tmin, tqty, tmin),
        )
        conn.execute(
            """INSERT INTO warehouse_stock (warehouse_id, part_id, quantity, min_threshold)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(warehouse_id, part_id) DO UPDATE SET quantity=?, min_threshold=?,
                   updated_at=datetime('now', 'localtime')""",
            (DEMO_WAREHOUSE, part_id, wqty, wmin, wqty, wmin),
        )

    conn.commit()

    if print_token:
        print("=" * 64)
        print("Demo PWA seeded. Install link:")
        print(f"  /tech/pwa/?t={token}")
        print(f"  (full URL: https://<your-domain>/tech/pwa/?t={token})")
        print(f"Tech: {DEMO_TECH_NAME} | Truck: {DEMO_TRUCK} | Warehouse: {DEMO_WAREHOUSE}")
        print("=" * 64)
    return token


if __name__ == "__main__":
    conn = get_db()
    try:
        seed_demo(conn)
    finally:
        conn.close()
