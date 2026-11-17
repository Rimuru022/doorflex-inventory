"""PWA tech-facing API — catalog + offline-first batch deductions.

Tokens: each tech may have a `pwa_token_hash` (SHA-256 of the raw token).
The raw token is only ever shown at provisioning time; the DB only stores the
hash. A token resolves to the tech and therefore to ITS truck + warehouse.
"""
import hashlib
import json
import logging
import secrets

from modules import events as events_mod
from modules import job_captures as job_captures_mod

log = logging.getLogger("doorflex.pwa")

DEDUCTION_SOURCE_PWA = "pwa"


def hash_token(raw_token):
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def generate_token():
    """Return a fresh raw token (caller persists the hash)."""
    return secrets.token_urlsafe(32)


def get_tech_by_token(conn, raw_token):
    """Resolve a raw token to its tech. Returns None if unknown/inactive."""
    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    row = conn.execute(
        "SELECT * FROM techs WHERE pwa_token_hash=? AND active=1 AND deduction_source=?",
        (token_hash, DEDUCTION_SOURCE_PWA),
    ).fetchone()
    if not row:
        events_mod.log_event(
            conn, "warn", "pwa", "pwa.auth.invalid_token",
            "PWA request with invalid/expired token",
            detail={"token_hash_prefix": token_hash[:8]},
        )
        conn.commit()
    return row


def set_tech_token(conn, tech_id, raw_token):
    """Persist a raw token (plus its hash for lookup) on a tech. Returns the
    raw token. The raw value is kept so the dashboard can re-display the
    install link without rotating (which would revoke the current one).

    Granting a PWA token switches the tech to `deduction_source='pwa'` and
    reactivates it: a token only makes sense for a tech that deducts via the
    PWA, and `get_tech_by_token` requires that source (anti double-deduction
    with the email pipeline, M8)."""
    conn.execute(
        "UPDATE techs SET pwa_token_hash=?, pwa_token=?, deduction_source=?, active=1 WHERE id=?",
        (hash_token(raw_token), raw_token, DEDUCTION_SOURCE_PWA, tech_id),
    )
    conn.commit()
    return raw_token


def rotate_tech_token(conn, tech_id):
    """Generate a fresh token for a tech, storing both raw + hash."""
    raw = generate_token()
    set_tech_token(conn, tech_id, raw)
    return raw


def clear_tech_token(conn, tech_id):
    conn.execute(
        "UPDATE techs SET pwa_token_hash='', pwa_token='' WHERE id=?",
        (tech_id,),
    )
    conn.commit()


def get_truck_catalog(conn, tech):
    """Parts of the canonical catalog (active only) with THIS truck's stock.
    `part_id` is the canonical SKU. Never aggregates other trucks/warehouses."""
    rows = conn.execute(
        """SELECT p.id AS part_id, p.name, p.aliases, p.category, p.unit,
                  COALESCE(s.quantity, 0) AS stock,
                  COALESCE(s.min_threshold, 0) AS min_threshold
           FROM parts p
           LEFT JOIN stock s ON s.part_id = p.id AND s.truck_id = ?
           WHERE p.active = 1
           ORDER BY p.name""",
        (tech["truck_id"],),
    ).fetchall()

    parts = []
    for r in rows:
        aliases = []
        try:
            aliases = json.loads(r["aliases"])
        except (ValueError, TypeError):
            pass
        parts.append({
            "part_id": r["part_id"],
            "name": r["name"],
            "aliases": aliases,
            "category": r["category"],
            "unit": r["unit"],
            "stock": r["stock"],
            "min_threshold": r["min_threshold"],
        })
    return parts


def _items_hash(items):
    """Deterministic hash of the sorted normalized item list. Two batches with
    the same items (regardless of order) get the same hash."""
    norm = sorted([
        {"sku": str(i.get("sku", "")).strip(), "qty": int(i.get("qty", 0))}
        for i in items
    ], key=lambda x: (x["sku"], x["qty"]))
    return hashlib.sha256(json.dumps(norm, sort_keys=True).encode("utf-8")).hexdigest()


def is_batch_processed(conn, batch_id):
    """True if this batch_id was already processed. Idempotency is keyed on the
    batch_id itself: a retry (same id, even with different items) must NEVER
    re-deduct, so a batch is processed at most once."""
    row = conn.execute(
        "SELECT 1 FROM deduction_batches WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    return row is not None


def mark_batch_processed(conn, tech_id, batch_id, items):
    conn.execute(
        "INSERT OR IGNORE INTO deduction_batches (batch_id, tech_id, items_hash) VALUES (?, ?, ?)",
        (batch_id, tech_id, _items_hash(items)),
    )
    conn.commit()


def validate_deduction_items(conn, items):
    """Validate + resolve item SKUs against the canonical catalog.
    Returns (validated, rejected). validated items are dicts ready for pwa_deduct."""
    from modules import inventory

    validated = []
    rejected = []
    for item in items or []:
        sku = str(item.get("sku", "")).strip()
        qty = item.get("qty", 0)
        ts = item.get("ts", 0)

        if not sku:
            rejected.append({"sku": sku, "qty": qty, "reason": "missing_sku"})
            continue
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            rejected.append({"sku": sku, "qty": qty, "reason": "invalid_qty"})
            continue

        part = conn.execute("SELECT id, name, active FROM parts WHERE id=?", (sku,)).fetchone()
        if not part:
            rejected.append({"sku": sku, "qty": qty, "reason": "unknown_sku"})
            continue
        if not part["active"]:
            rejected.append({"sku": sku, "qty": qty, "reason": "inactive_sku"})
            continue

        wo_number = str(item.get("wo_number", "")).strip()
        if wo_number and job_captures_mod.is_wo_closed(conn, wo_number):
            rejected.append({"sku": sku, "qty": qty, "reason": "wo_already_closed"})
            continue

        validated.append({
            "part_id": sku,
            "part_name": part["name"],
            "qty": qty,
            "ts": int(ts or 0),
            # Which job this deduction belongs to, so the office sees "parts
            # used" alongside that job's charge. The PWA now sends its active
            # WO on every item; still tolerated as empty here for any older
            # client build with queued, untagged batches.
            "wo_number": wo_number,
        })
    return validated, rejected


def process_batch(conn, tech, batch_id, items):
    """Run one idempotent deduction batch for a tech. Returns the API result."""
    from modules import inventory

    if is_batch_processed(conn, batch_id):
        log.warning("PWA batch %s already processed (tech=%s)", batch_id, tech["name"])
        events_mod.log_event(
            conn, "info", "pwa", "pwa.batch.duplicate",
            f"Batch {batch_id} already processed (idempotent skip)",
            detail={"tech": tech["name"], "truck_id": tech["truck_id"]},
            ref=batch_id,
        )
        conn.commit()
        return {
            "status": "already_processed",
            "accepted": [],
            "rejected": [],
            "batch_id": batch_id,
        }

    validated, rejected = validate_deduction_items(conn, items)
    accepted = []
    deduct_rejected = []
    for it in validated:
        res, err = inventory.pwa_deduct(
            conn, tech["truck_id"], tech["warehouse_id"],
            it["part_id"], it["qty"], tech["name"], ts=it["ts"],
            wo_number=it.get("wo_number", ""),
        )
        if err:
            rejected.append({"sku": it["part_id"], "qty": it["qty"], "reason": err})
            deduct_rejected.append({"sku": it["part_id"], "qty": it["qty"], "reason": err})
        else:
            accepted.append({
                "sku": it["part_id"],
                "name": it["part_name"],
                "qty": it["qty"],
                "stock_after": res["truck_after"],
                "is_low": res["is_low"],
            })

    mark_batch_processed(conn, tech["id"], batch_id, items)
    log.info(
        "PWA batch %s (tech=%s truck=%s): %d accepted, %d rejected%s",
        batch_id, tech["name"], tech["truck_id"],
        len(accepted), len(rejected),
        f" reasons={rejected}" if rejected else "",
    )

    level = "info" if deduct_rejected or rejected else "info"
    events_mod.log_event(
        conn, level, "pwa", "pwa.batch.processed",
        f"PWA batch {batch_id}: {len(accepted)} accepted, {len(rejected)} rejected",
        detail={
            "tech": tech["name"],
            "truck_id": tech["truck_id"],
            "accepted": [{"sku": a["sku"], "qty": a["qty"]} for a in accepted],
            "rejected": rejected,
        },
        ref=batch_id,
    )
    conn.commit()
    return {
        "status": "processed",
        "accepted": accepted,
        "rejected": rejected,
        "batch_id": batch_id,
    }
