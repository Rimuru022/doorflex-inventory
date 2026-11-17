import email
import json
import logging
from email.header import decode_header

from modules import techs, parts, inventory
from modules.name_matcher import match_part, match_package
from modules.pdf_extractor import processor as pdf_processor
from modules import events as events_mod

logger = logging.getLogger("email_poller")


def decode_mime_header(header_value):
    if not header_value:
        return ""
    decoded_parts = decode_header(header_value)
    result = []
    for part, charset in decoded_parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return " ".join(result)


def fetch_pdf_attachments(msg):
    pdfs = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "application/pdf":
                filename = part.get_filename()
                if filename:
                    filename = decode_mime_header(filename)
                pdf_bytes = part.get_payload(decode=True)
                if pdf_bytes:
                    pdfs.append({"filename": filename or "invoice.pdf", "data": pdf_bytes})
            elif part.get_content_type() == "application/octet-stream":
                filename = part.get_filename()
                if filename and filename.lower().endswith(".pdf"):
                    pdf_bytes = part.get_payload(decode=True)
                    if pdf_bytes:
                        pdfs.append({"filename": filename, "data": pdf_bytes})
    else:
        if msg.get_content_type() == "application/pdf":
            pdf_bytes = msg.get_payload(decode=True)
            if pdf_bytes:
                pdfs.append({"filename": "invoice.pdf", "data": pdf_bytes})
    return pdfs


def process_single_email(conn, msg, email_id):
    subject = decode_mime_header(msg.get("Subject", ""))
    msg_id = msg.get("Message-ID", email_id)
    invoice_id = msg_id

    cur = conn.execute(
        "INSERT OR IGNORE INTO processed_invoices (invoice_id, email_id) VALUES (?, ?)",
        (invoice_id, str(email_id)),
    )
    conn.commit()
    if cur.rowcount == 0:
        logger.info(f"Email {email_id}: Invoice {invoice_id} already processed, skipping")
        events_mod.log_event(
            conn, "info", "ingest", "ingest.duplicate",
            f"Duplicate invoice skipped: {subject[:80]}",
            ref=invoice_id,
        )
        conn.commit()
        return {"status": "skipped", "reason": "duplicate_invoice"}

    pdfs = fetch_pdf_attachments(msg)
    if not pdfs:
        logger.info(f"Email {email_id}: No PDF attachment found")
        events_mod.log_event(
            conn, "warn", "ingest", "ingest.no_pdf",
            f"Email had no PDF: {subject[:80]}",
            ref=invoice_id,
        )
        conn.commit()
        conn.execute("DELETE FROM processed_invoices WHERE invoice_id=?", (invoice_id,))
        conn.commit()
        return {"status": "skipped", "reason": "no_pdf"}

    results = []
    for pdf in pdfs:
        try:
            data = pdf_processor.extract(pdf["data"])
            result = _process_extracted_data(conn, data, invoice_id, invoice_name=pdf["filename"])
            results.append(result)
        except Exception as e:
            logger.error(f"Email {email_id}: PDF processing error: {e}")
            events_mod.log_event(
                conn, "error", "ingest", "ingest.pdf_error",
                f"PDF parse failed: {e}",
                detail={"filename": pdf.get("filename"), "error": str(e)},
                ref=invoice_id,
            )
            conn.commit()
            results.append({"status": "error", "error": str(e)})

    ok = any(r.get("status") != "error" for r in results)
    if not ok:
        conn.execute("DELETE FROM processed_invoices WHERE invoice_id=?", (invoice_id,))
        # Individual ingest.pdf_error events were already logged per attachment
        # above, but they're easy to miss among per-attachment noise. This one
        # summary event marks the whole invoice as unrecovered (every PDF
        # failed, nothing was deducted, row removed so a resend can retry).
        events_mod.log_event(
            conn, "warn", "ingest", "ingest.invoice_unparseable",
            f"Invoice could not be parsed from any attachment, discarded: {subject[:80]}",
            detail={"attempted": len(pdfs)},
            ref=invoice_id,
        )
        conn.commit()

    return {"status": "processed", "results": results, "invoice_id": invoice_id}


def _process_extracted_data(conn, data, invoice_id, invoice_name=""):
    tech_name = data.get("technician_name", "")
    if not tech_name:
        return {"status": "error", "reason": "no_technician_name"}

    tech, confidence = techs.find_tech_by_name(conn, tech_name)
    if not tech:
        pending = {
            "format": data.get("format", ""),
            "parts_list": data.get("parts_list", []),
            "package_description": data.get("package_description", ""),
        }
        conn.execute(
            "INSERT INTO unmatched_parts (invoice_id, tech_name, raw_name, quantity, truck_id, warehouse_id, kind, invoice_name, pending_data) "
            "VALUES (?, '', ?, 0, '', '', 'tech', ?, ?)",
            (invoice_id, tech_name, invoice_name, json.dumps(pending)),
        )
        events_mod.log_event(
            conn, "warn", "ingest", "ingest.tech_pending",
            f"Tech '{tech_name}' needs manual confirmation (best match={confidence})",
            detail={"tech_name": tech_name, "confidence": confidence, "invoice_name": invoice_name},
            ref=invoice_id,
        )
        conn.commit()
        return {
            "status": "tech_pending",
            "tech_name": tech_name,
            "confidence": confidence,
            "message": f"Technician '{tech_name}' needs manual confirmation.",
        }

    # PRD v3 M8 — source rule: a tech that deducts via the PWA must NOT be
    # deducted from email (anti double-deduction). Flag in unmatched instead.
    if tech["deduction_source"] == "pwa":
        note = "tech subcontratista: usar PWA"
        conn.execute(
            """INSERT INTO unmatched_parts (invoice_id, tech_name, raw_name, quantity,
               truck_id, warehouse_id, kind, invoice_name, pending_data)
               VALUES (?, ?, ?, 0, ?, ?, 'pwa_source', ?, ?)""",
            (invoice_id, tech_name, note, tech["truck_id"], tech["warehouse_id"],
             invoice_name, json.dumps({"note": note})),
        )
        events_mod.log_event(
            conn, "info", "ingest", "ingest.pwa_flagged",
            f"Tech '{tech_name}' uses PWA — invoice flagged, not deducted from email",
            detail={"tech": tech_name, "truck_id": tech["truck_id"]},
            ref=invoice_id,
        )
        conn.commit()
        return {
            "status": "pwa_source_flagged",
            "tech_name": tech_name,
            "message": "Tech uses the PWA — invoice flagged, not deducted.",
        }

    format_type = data.get("format", "")
    truck_id = tech["truck_id"]
    warehouse_id = tech["warehouse_id"]

    if format_type == "package_pricing":
        return _process_package_format(conn, data, tech, truck_id, warehouse_id, invoice_id, confidence, invoice_name)
    elif format_type == "itemized":
        return _process_itemized_format(conn, data, tech, truck_id, warehouse_id, invoice_id, confidence, invoice_name)
    else:
        return {"status": "error", "reason": "unknown_format"}


def reprocess_with_confirmed_tech(conn, row, tech):
    """Re-run the format handler for a queued kind='tech' row using the
    tech chosen by the user. Returns the same dict shape as the format
    handlers. Already-deducted parts (tracked in pending_data['deducted'])
    are skipped so re-confirming never double-deducts."""
    try:
        pending = json.loads(row["pending_data"] or "{}")
    except (ValueError, TypeError):
        return {"status": "error", "reason": "invalid_pending_data"}

    data = {
        "format": pending.get("format", ""),
        "parts_list": pending.get("parts_list", []),
        "package_description": pending.get("package_description", ""),
    }
    format_type = data.get("format", "")
    truck_id = tech["truck_id"]
    warehouse_id = tech["warehouse_id"]
    deducted = set(pending.get("deducted") or [])

    if format_type == "package_pricing":
        result = _process_package_format(
            conn, data, tech, truck_id, warehouse_id, row["invoice_id"], 1.0,
            row["invoice_name"], skip_part_ids=deducted,
        )
    elif format_type == "itemized":
        result = _process_itemized_format(
            conn, data, tech, truck_id, warehouse_id, row["invoice_id"], 1.0,
            row["invoice_name"], skip_part_ids=deducted,
        )
    else:
        return {"status": "error", "reason": "unknown_format"}

    newly_deducted = {
        d["part_id"] for d in (result.get("deductions") or [])
        if d.get("part_id") and d.get("result")
    }
    pending["deducted"] = sorted(deducted | newly_deducted)
    conn.execute(
        "UPDATE unmatched_parts SET pending_data=? WHERE id=?",
        (json.dumps(pending), row["id"]),
    )
    conn.commit()
    return result


def _process_package_format(conn, data, tech, truck_id, warehouse_id, invoice_id, tech_confidence, invoice_name="", skip_part_ids=None):
    description = data.get("package_description", "")
    if not description:
        return {"status": "error", "reason": "no_description"}

    pkg, expanded_parts, pkg_confidence = match_package(conn, description)
    if not pkg:
        conn.execute(
            """INSERT INTO unmatched_parts (invoice_id, tech_name, raw_name, quantity, truck_id, warehouse_id, kind, invoice_name)
               VALUES (?, ?, ?, 1, ?, ?, 'package', ?)""",
            (invoice_id, tech["name"], description, truck_id, warehouse_id, invoice_name),
        )
        events_mod.log_event(
            conn, "warn", "ingest", "ingest.unmatched_package",
            f"Package '{description}' not matched (best={pkg_confidence})",
            detail={"description": description, "confidence": pkg_confidence, "tech": tech["name"]},
            ref=invoice_id,
        )
        conn.commit()
        return {"status": "unmatched_package", "description": description, "confidence": pkg_confidence}

    skip = set(skip_part_ids or [])
    deductions = []
    blocked = []
    for part_entry in expanded_parts:
        part_id = part_entry["part_id"]
        qty = part_entry["qty"]
        if part_id in skip:
            deductions.append({"part_id": part_id, "qty": qty, "already_deducted": True})
            continue
        result, err = inventory.deduct(
            conn, truck_id, warehouse_id, part_id, qty,
            tech["name"], invoice_id, pkg_confidence, "package_pricing", raw_text=description,
            invoice_name=invoice_name,
        )
        if err:
            deductions.append({"part_id": part_id, "qty": qty, "error": err})
            blocked.append({"part_id": part_id, "qty": qty, "error": err})
        else:
            deductions.append({"part_id": part_id, "qty": qty, "result": result})

    if deducted_count := sum(1 for d in deductions if d.get("result")):
        events_mod.log_event(
            conn, "info", "ingest", "ingest.package_deducted",
            f"Package '{pkg['name']}' deducted: {deducted_count} item(s)",
            detail={"package": pkg["name"], "tech": tech["name"], "blocked": blocked},
            ref=invoice_id,
        )
    for b in blocked:
        events_mod.log_event(
            conn, "warn", "ingest", "ingest.deduct_blocked",
            f"Part '{b['part_id']}' x{b['qty']} blocked: {b['error'][:120]}",
            detail={"part_id": b["part_id"], "qty": b["qty"], "error": b["error"], "tech": tech["name"]},
            ref=invoice_id,
        )
    conn.commit()

    return {
        "status": "processed",
        "format": "package_pricing",
        "package": pkg["name"],
        "deductions": deductions,
        "tech": tech["name"],
        "truck_id": truck_id,
        "warehouse_id": warehouse_id,
    }


def _process_itemized_format(conn, data, tech, truck_id, warehouse_id, invoice_id, tech_confidence, invoice_name="", skip_part_ids=None):
    parts_list = data.get("parts_list", [])
    skip = set(skip_part_ids or [])
    deductions = []
    unmatched = []
    fuzzy_pending = []

    for item in parts_list:
        raw_name = item["raw_name"]
        qty = item["quantity"]
        part_id, confidence, match_type = match_part(conn, raw_name)

        if part_id in skip:
            deductions.append({"raw_name": raw_name, "qty": qty, "part_id": part_id, "already_deducted": True})
            continue

        if match_type == "exact" or match_type == "fuzzy_auto":
            result, err = inventory.deduct(
                conn, truck_id, warehouse_id, part_id, qty,
                tech["name"], invoice_id, confidence, "itemized", raw_text=raw_name,
                invoice_name=invoice_name,
            )
            if err:
                deductions.append({"raw_name": raw_name, "qty": qty, "part_id": part_id, "error": err})
                events_mod.log_event(
                    conn, "warn", "ingest", "ingest.deduct_blocked",
                    f"Part '{raw_name}' x{qty} blocked: {err[:120]}",
                    detail={"raw_name": raw_name, "qty": qty, "part_id": part_id, "error": err,
                            "tech": tech["name"], "match_type": match_type},
                    ref=invoice_id,
                )
            else:
                deductions.append({"raw_name": raw_name, "qty": qty, "result": result, "part_id": part_id})
        elif match_type == "fuzzy_pending":
            fuzzy_pending.append({"raw_name": raw_name, "quantity": qty, "confidence": confidence, "suggested_part_id": part_id})
            conn.execute(
                "INSERT INTO unmatched_parts (invoice_id, tech_name, raw_name, quantity, truck_id, warehouse_id, invoice_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (invoice_id, tech["name"], raw_name, qty, truck_id, warehouse_id, invoice_name),
            )
            events_mod.log_event(
                conn, "warn", "ingest", "ingest.fuzzy_pending",
                f"Fuzzy match '{raw_name}' -> {part_id} (conf={confidence}) needs confirmation",
                detail={"raw_name": raw_name, "qty": qty, "suggested": part_id,
                        "confidence": confidence, "tech": tech["name"]},
                ref=invoice_id,
            )
        else:
            unmatched.append({"raw_name": raw_name, "quantity": qty, "confidence": confidence})
            conn.execute(
                "INSERT INTO unmatched_parts (invoice_id, tech_name, raw_name, quantity, truck_id, warehouse_id, invoice_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (invoice_id, tech["name"], raw_name, qty, truck_id, warehouse_id, invoice_name),
            )
            events_mod.log_event(
                conn, "warn", "ingest", "ingest.unmatched_part",
                f"No match for '{raw_name}' (best={confidence})",
                detail={"raw_name": raw_name, "qty": qty, "confidence": confidence, "tech": tech["name"]},
                ref=invoice_id,
            )

    deducted_count = sum(1 for d in deductions if d.get("result"))
    if deducted_count or unmatched or fuzzy_pending:
        events_mod.log_event(
            conn, "info", "ingest", "ingest.itemized_processed",
            f"Itemized: {deducted_count} deducted, {len(unmatched)} unmatched, {len(fuzzy_pending)} fuzzy",
            detail={"tech": tech["name"], "deducted": deducted_count,
                    "unmatched": len(unmatched), "fuzzy_pending": len(fuzzy_pending)},
            ref=invoice_id,
        )
    conn.commit()

    return {
        "status": "processed",
        "format": "itemized",
        "deductions": deductions,
        "unmatched": unmatched,
        "fuzzy_pending": fuzzy_pending,
        "tech": tech["name"],
        "truck_id": truck_id,
        "warehouse_id": warehouse_id,
    }
