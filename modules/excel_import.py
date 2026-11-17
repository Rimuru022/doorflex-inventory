"""Distributor Excel catalog import.

The Service Spring file has two sheets:
  - "2026 Monthly Usage": one row per unique component with Category, Part #,
    Component / Description, monthly qty (Jan..Aug), Total Qty, Avg / Month,
    Recorded Spend, Avg Unit Cost, Purchase Lines, Last Purchase, Last Unit Cost.
    The header is at row 8 (rows 1-7 are title + summary).
  - "Purchase Detail": raw purchase lines (Date, Month, Order #, Part #,
    Component / Description, Category, Qty, Unit Cost, Line Total).

We only consume the summary sheet for catalog creation. Idempotent: re-running
the same file produces 0 created / N updated / 0 warnings.

Canonical SKUs are auto-generated as ``P-NNNN`` so they stay independent of
any single distributor's numbering — that is what makes cross-distributor
homologation (alias merge) possible later.
"""
import io
import json


SUMMARY_SHEET = "2026 Monthly Usage"
EXPECTED_HEADERS = [
    "Category", "Part #", "Component / Description",
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
    "Total Qty", "Avg / Month", "Recorded Spend", "Avg Unit Cost",
    "Purchase Lines", "Last Purchase", "Last Unit Cost",
]


def parse_usage_sheet(xlsx_bytes):
    """Return a list of dicts (one per non-empty data row) from the summary
    sheet. Raises ValueError on missing sheet or malformed headers."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    if SUMMARY_SHEET not in wb.sheetnames:
        wb.close()
        raise ValueError(
            f"Sheet '{SUMMARY_SHEET}' not found. Found: {wb.sheetnames}"
        )

    ws = wb[SUMMARY_SHEET]

    header_row = None
    header_idx = None
    for idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
        if row and row[0] == "Category" and len(row) > 1 and row[1] == "Part #":
            header_row = list(row)
            header_idx = idx
            break
    if header_row is None:
        wb.close()
        raise ValueError(
            f"Could not locate the header row (looking for 'Category', 'Part #'). "
            f"Found {ws.max_row} rows."
        )

    rows = []
    for raw in ws.iter_rows(min_row=header_idx + 1, values_only=True):
        if not raw or all(v is None or (isinstance(v, str) and not v.strip()) for v in raw):
            continue
        rec = dict(zip(header_row, raw))
        part_no = (rec.get("Part #") or "").strip() if isinstance(rec.get("Part #"), str) else (
            "" if rec.get("Part #") is None else str(rec.get("Part #")).strip()
        )
        if not part_no:
            continue
        description = rec.get("Component / Description") or ""
        if not isinstance(description, str):
            description = str(description)
        description = description.strip()
        if not description:
            continue
        rows.append({
            "category": (rec.get("Category") or "").strip() if isinstance(rec.get("Category"), str) else "",
            "part_no": part_no,
            "description": description,
            "total_qty": rec.get("Total Qty") or 0,
            "avg_unit_cost": _to_float(rec.get("Avg Unit Cost")),
            "last_purchase": rec.get("Last Purchase"),
        })
    wb.close()
    return rows


def _to_float(v):
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def import_distributor_catalog(conn, distributor_id, rows):
    """For each row: look up an existing product_distributors mapping by
    (distributor_id, distributor_sku). If found, refresh the canonical part
    fields and the mapping. Otherwise create a new canonical part with an
    auto-generated ``P-NNNN`` id and a fresh mapping. Always merges aliases
    without duplication. Returns {created, updated, warnings}."""
    from modules import distributors as dist_mod
    from modules import parts as parts_mod

    if not dist_mod.get_distributor(conn, distributor_id):
        return {
            "created": 0, "updated": 0,
            "warnings": [],
            "errors": [f"Unknown distributor '{distributor_id}'"],
        }

    created = 0
    updated = 0
    warnings = []
    errors = []
    seen_part_nos = set()

    counter = conn.execute("SELECT COUNT(*) FROM parts").fetchone()[0] + 1

    for r in rows:
        part_no = r["part_no"]
        description = r["description"]
        category = r.get("category", "") or ""
        cost = r.get("avg_unit_cost", 0) or 0

        if part_no in seen_part_nos:
            warnings.append(
                f"Part # '{part_no}' appeared multiple times in the file — only the first was imported."
            )
            continue
        seen_part_nos.add(part_no)

        existing_map = conn.execute(
            "SELECT product_id FROM product_distributors "
            "WHERE distributor_id=? AND distributor_sku=?",
            (distributor_id, part_no),
        ).fetchone()

        try:
            if existing_map:
                product_id = existing_map["product_id"]
                parts_mod.update_part(
                    conn, product_id,
                    name=description, category=category, cost=cost,
                )
                new_aliases = _merge_aliases(conn, product_id, part_no, description)
                parts_mod.update_part(conn, product_id, aliases=new_aliases)
                dist_mod.upsert_mapping(
                    conn, product_id, distributor_id,
                    sku=part_no, part_name=description, is_active=True,
                )
                updated += 1
            else:
                product_id = f"P-{counter:04d}"
                counter += 1
                while conn.execute(
                    "SELECT 1 FROM parts WHERE id=?", (product_id,)
                ).fetchone():
                    product_id = f"P-{counter:04d}"
                    counter += 1
                aliases = []
                if part_no and part_no != description:
                    aliases.append(part_no)
                elif part_no:
                    aliases.append(part_no)
                if description and description != part_no:
                    aliases.append(description)
                parts_mod.create_part(
                    conn, product_id, name=description,
                    aliases=aliases, category=category, cost=cost,
                )
                dist_mod.upsert_mapping(
                    conn, product_id, distributor_id,
                    sku=part_no, part_name=description, is_active=True,
                )
                created += 1
        except Exception as e:
            errors.append(f"Row '{part_no}': {e}")

    conn.commit()
    return {
        "created": created,
        "updated": updated,
        "warnings": warnings,
        "errors": errors,
    }


def _merge_aliases(conn, product_id, part_no, description):
    """Merge part_no + description into the part's alias list, deduped, then
    return the final list (without committing)."""
    part = conn.execute(
        "SELECT aliases FROM parts WHERE id=?", (product_id,)
    ).fetchone()
    current = []
    if part and part["aliases"]:
        try:
            current = json.loads(part["aliases"])
        except (ValueError, TypeError):
            current = []
    seen = {a.strip().lower(): a for a in current if a and a.strip()}
    order = [a for a in current if a and a.strip()]
    for candidate in (part_no, description):
        if not candidate:
            continue
        c = candidate.strip()
        if not c:
            continue
        key = c.lower()
        if key in seen:
            continue
        seen[key] = c
        order.append(c)
    return order