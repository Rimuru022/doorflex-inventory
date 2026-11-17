"""QBO invoice ingest — fetch a QuickBooks Online invoice by id, normalize
its line items into the shape `_process_extracted_data` already accepts, and
hand it off. Every existing downstream tracker (per-tech usage, per-truck,
per-part, low-stock alerts, unmatched-parts review) auto-populates from the
resulting `log` rows.

Called by the QBO webhook route (main.py:/webhooks/qbo/invoice), never
directly. Webhooks from Intuit are only 'ping' notifications — the full
invoice is fetched here via the REST API.

Field mapping decisions we're pinning until we see Nick's first real invoice:
- technician_name ← invoice's SalesRep (fallback: CustomerRef.name)
- wo_number      ← invoice's DocNumber (Dispatch's Job ID #, per the PDF
                    invoices already in the repo — same field either way)
- parts_list[i]  ← Line[i].SalesItemLineDetail.{ItemRef.name, Qty}

When Nick provides a real QBO invoice, only this module needs adjustment —
the webhook route and everything downstream stay the same.
"""
import json
import urllib.error
import urllib.request

from modules import events as events_mod
from modules import qbo_oauth
from modules.email_poller import _process_extracted_data


class QboIngestError(Exception):
    pass


def fetch_invoice(conn, realm_id, invoice_id):
    """GET /v3/company/{realm}/invoice/{id} using the stored OAuth token,
    auto-refreshing if it's expired. Returns the parsed JSON dict."""
    token = qbo_oauth.get_valid_token(conn, realm_id)
    url = (
        f"{qbo_oauth.api_base_url()}/v3/company/{realm_id}/invoice/{invoice_id}"
        f"?minorversion=73"
    )
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = str(e)
        raise QboIngestError(f"QBO GET invoice {invoice_id} → {e.code}: {body}") from e
    except Exception as e:
        raise QboIngestError(f"QBO GET invoice {invoice_id} failed: {e}") from e


def normalize_invoice(qbo_response):
    """Map QBO's Invoice JSON into the `data` dict `_process_extracted_data`
    expects. See the module docstring for the field-mapping rationale.

    Returns None if this invoice has no ingestable line items — e.g. a pure
    labor invoice with no parts. The caller treats None as 'nothing to do'
    (still 200 OK to Intuit; not an error).
    """
    invoice = qbo_response.get("Invoice", qbo_response)
    if not invoice:
        return None

    parts_list = []
    for line in invoice.get("Line", []) or []:
        detail_type = line.get("DetailType", "")
        # QBO puts subtotal lines and description-only lines in the same array;
        # skip everything that isn't a real sales-item deduction.
        if detail_type != "SalesItemLineDetail":
            continue
        detail = line.get("SalesItemLineDetail", {}) or {}
        item_ref = detail.get("ItemRef", {}) or {}
        name = str(item_ref.get("name", "")).strip()
        qty_raw = detail.get("Qty", 0)
        try:
            qty = int(qty_raw)
        except (TypeError, ValueError):
            try:
                qty = int(float(qty_raw))
            except (TypeError, ValueError):
                qty = 0
        if not name or qty <= 0:
            continue
        parts_list.append({"raw_name": name, "quantity": qty})

    if not parts_list:
        return None

    tech = ""
    sales_rep = invoice.get("SalesRep", {}) or {}
    if isinstance(sales_rep, dict):
        tech = str(sales_rep.get("name", "")).strip()
    # Some Dispatch→QBO configs put the tech in a top-level CustomField
    # named "Technician" or similar. Try those before falling back.
    if not tech:
        for cf in invoice.get("CustomField", []) or []:
            name = str(cf.get("Name", "")).strip().lower()
            if name in ("technician", "tech", "tech name", "technician name"):
                tech = str(cf.get("StringValue", "")).strip()
                break

    wo_number = str(invoice.get("DocNumber", "")).strip()

    return {
        "format": "itemized",
        "technician_name": tech,
        "parts_list": parts_list,
        "wo_number": wo_number,
    }


def process_invoice_event(conn, realm_id, invoice_id):
    """End-to-end handler for one webhook notification. Fetches the invoice,
    normalizes it, dispatches to the existing deduction pipeline, logs an
    event with the outcome. Returns the result dict from
    _process_extracted_data (or a synthetic 'skipped' one)."""
    try:
        payload = fetch_invoice(conn, realm_id, invoice_id)
    except QboIngestError as e:
        events_mod.log_event(
            conn, "error", "qbo", "qbo.fetch_failed",
            f"Failed to fetch QBO invoice {invoice_id}: {e}",
            detail={"realm_id": realm_id, "invoice_id": invoice_id, "error": str(e)},
            ref=str(invoice_id),
        )
        conn.commit()
        raise

    data = normalize_invoice(payload)
    if data is None:
        events_mod.log_event(
            conn, "info", "qbo", "qbo.invoice_skipped",
            f"QBO invoice {invoice_id} has no deductible line items — skipping.",
            detail={"realm_id": realm_id, "invoice_id": invoice_id},
            ref=str(invoice_id),
        )
        conn.commit()
        return {"status": "skipped", "reason": "no_parts"}

    # invoice_id is the idempotency key downstream (deduction_batches equivalent).
    # invoice_name uses the human-friendly DocNumber if present.
    invoice_name = f"QBO-{data.get('wo_number') or invoice_id}"
    result = _process_extracted_data(
        conn, data, invoice_id=f"qbo:{invoice_id}", invoice_name=invoice_name,
    )
    qbo_oauth.record_webhook_received(conn, realm_id)
    return result
