"""One-time reconcile: mark stock_alerts resolved when the requested deduction
already happened in the log (e.g. re-mapped in Unmatched, package, or tech-confirm).

Usage:
    uv run python reconcile_stock_alerts.py [path/to/inventory.db]
"""
import sqlite3
import sys

DB = sys.argv[1] if len(sys.argv) > 1 else "data/inventory.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

open_alerts = conn.execute(
    "SELECT COUNT(*) c FROM stock_alerts WHERE resolved=0"
).fetchone()["c"]

cur = conn.execute(
    """UPDATE stock_alerts SET resolved=1
       WHERE resolved=0 AND EXISTS (
           SELECT 1 FROM log
           WHERE log.part_id=stock_alerts.part_id
             AND log.truck_id=stock_alerts.truck_id
             AND log.invoice_id=stock_alerts.invoice_id
             AND log.quantity_change=-stock_alerts.requested_qty
       )"""
)
conn.commit()

print(f"Open alerts before: {open_alerts}")
print(f"Reconciled (deduction already in log): {cur.rowcount}")
remaining = conn.execute(
    "SELECT COUNT(*) c FROM stock_alerts WHERE resolved=0"
).fetchone()["c"]
print(f"Still open (genuinely unfulfilled): {remaining}")
