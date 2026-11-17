def log_transaction(
    conn, tech_name="", truck_id="", warehouse_id="",
    part_id="", quantity_change=0,
    stock_before=0, stock_after=0, invoice_id="",
    confidence=1.0, raw_text="", source="email", format="", invoice_name="",
    wo_number="",
):
    conn.execute(
        """INSERT INTO log
           (tech_name, truck_id, warehouse_id, part_id,
            quantity_change, stock_before, stock_after,
            invoice_id, confidence, raw_text, source, format, invoice_name, wo_number)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tech_name, truck_id, warehouse_id, part_id,
         quantity_change, stock_before, stock_after,
         invoice_id, confidence, raw_text, source, format, invoice_name, wo_number),
    )


def get_logs(conn, tech_name=None, truck_id=None, part_id=None,
             stream=None, source=None, date_from=None, date_to=None,
             limit=100, offset=0, warehouse_id=None):
    conditions = []
    params = []

    if warehouse_id and warehouse_id != "all":
        conditions.append("warehouse_id=?")
        params.append(warehouse_id)
    if tech_name:
        conditions.append("tech_name=?")
        params.append(tech_name)
    if truck_id:
        conditions.append("truck_id=?")
        params.append(truck_id)
    if part_id:
        conditions.append("part_id=?")
        params.append(part_id)
    if stream:
        conditions.append("stream=?")
        params.append(stream)
    if source:
        conditions.append("source=?")
        params.append(source)
    if date_from:
        conditions.append("timestamp >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("timestamp <= ?")
        params.append(date_to)

    where = ""
    if conditions:
        where = "WHERE " + " AND ".join(conditions)

    return conn.execute(
        f"SELECT * FROM log {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()


def get_parts_used_by_wo(conn, wo_numbers):
    """Aggregate quantity_change (deductions only, i.e. quantity_change < 0)
    by part per wo_number. Backs the "what parts were used" side of a job's
    invoice — Nick's description: an out-of-pocket invoice reflects both the
    charge (job_captures.amount_charged) and the parts used, but those were
    two entirely separate systems before wo_number existed on `log`.

    Returns {wo_number: [{"part_id", "part_name", "qty"}, ...]}. Only
    wo_numbers actually tagged on a deduction appear as keys — most jobs
    captured before this existed (or where the tech didn't tag it) simply
    won't have an entry, not an empty list."""
    wo_numbers = [w for w in (wo_numbers or []) if w]
    if not wo_numbers:
        return {}
    placeholders = ",".join("?" for _ in wo_numbers)
    rows = conn.execute(
        f"""SELECT log.wo_number AS wo_number, log.part_id AS part_id,
                   parts.name AS part_name, SUM(-log.quantity_change) AS qty
            FROM log LEFT JOIN parts ON parts.id = log.part_id
            WHERE log.wo_number IN ({placeholders}) AND log.quantity_change < 0
            GROUP BY log.wo_number, log.part_id
            ORDER BY parts.name""",
        wo_numbers,
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["wo_number"], []).append({
            "part_id": r["part_id"],
            "part_name": r["part_name"] or r["part_id"],
            "qty": r["qty"],
        })
    return result
