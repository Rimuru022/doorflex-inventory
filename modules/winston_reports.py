"""Winston's weekly payment report — parsed from a photo via OpenAI vision,
never trusted blindly. A report is 'draft' the moment it's parsed and only
becomes 'confirmed' (writing real winston_report_jobs rows) once a human has
reviewed the parse on the review screen and confirmed or corrected it. The
commission column in a confirmed report is authoritative — never recomputed,
only cross-checked (see commission_summary/validate_draft).

This module is self-contained (no imports from other new modules besides
events) so it can be deleted/disabled independently of job capture, per the
"modular hub" architecture direction.
"""
import json
import base64
import re

from modules import events as events_mod

WO_NUMBER_RE = re.compile(r"^\d{6,10}$")

EXTRACTION_PROMPT = """Extract all job data from this payment report image. Return JSON only, no other text:
{
  "tech_name": "string",
  "period_start": "YYYY-MM-DD",
  "period_end": "YYYY-MM-DD",
  "jobs": [
    {
      "date": "YYYY-MM-DD",
      "wo_number": "string",
      "total_sales": number or null,
      "payout": number or null,
      "commission": number or null,
      "notes": "string or null"
    }
  ],
  "subtotal_sales": number,
  "subtotal_payout": number,
  "parts_deduction": number,
  "total_commission": number
}

wo_number MUST always be a JSON string, never a number — leading digits and
varying lengths must be preserved exactly as shown. If a field is not visible
in the image, use null rather than guessing."""


def parse_report_image(image_bytes, content_type="image/jpeg"):
    """Calls OpenAI's vision-capable chat completions endpoint. Raises on API
    error or unparseable JSON — the caller decides how to surface that."""
    from openai import OpenAI
    from config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_VISION_MODEL
    from modules.image_utils import downscale_for_vision
    from config import DEMO_MODE
    if DEMO_MODE:
        from modules.demo_runtime import consume_vision_call
        consume_vision_call()

    # Report photos are dense tables (many job rows) — a slightly larger cap
    # than the Dispatch screenshot parse keeps small text legible.
    image_bytes, content_type = downscale_for_vision(image_bytes, content_type, max_dim=2000)
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, timeout=25.0)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    resp = client.chat.completions.create(
        model=OPENAI_VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": EXTRACTION_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
            ],
        }],
        response_format={"type": "json_object"},
        max_tokens=3000,
    )
    return json.loads(resp.choices[0].message.content)


def create_draft(conn, parsed, raw_image_path):
    """Persists the raw parse immediately (status='draft') — the durable
    save-point right after upload, so nothing is lost if the browser closes
    before the review screen is confirmed. Does NOT write winston_report_jobs."""
    cur = conn.execute(
        """INSERT INTO winston_reports (
            tech_name, period_start, period_end, raw_image_path, parsed_json,
            subtotal_sales, subtotal_payout, parts_deduction, total_commission, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')""",
        (
            str(parsed.get("tech_name", "")).strip(),
            str(parsed.get("period_start", "")).strip(),
            str(parsed.get("period_end", "")).strip(),
            raw_image_path,
            json.dumps(parsed, ensure_ascii=False),
            parsed.get("subtotal_sales"),
            parsed.get("subtotal_payout"),
            parsed.get("parts_deduction"),
            parsed.get("total_commission"),
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_report(conn, report_id):
    row = conn.execute("SELECT * FROM winston_reports WHERE id=?", (report_id,)).fetchone()
    return dict(row) if row else None


def list_reports(conn, status=None):
    if status:
        rows = conn.execute(
            "SELECT * FROM winston_reports WHERE status=? ORDER BY received_at DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM winston_reports ORDER BY received_at DESC").fetchall()
    return [dict(r) for r in rows]


def _looks_like_wo_number(s):
    return bool(WO_NUMBER_RE.match(str(s).strip()))


def validate_draft(parsed):
    """Pure function — no DB access. Returns a list of {row_index, field,
    message} warnings to render inline on the review screen. Never blocks,
    never auto-corrects — purely informational flags for a human to glance at."""
    warnings = []
    jobs = parsed.get("jobs", []) or []

    for i, job in enumerate(jobs):
        wo = job.get("wo_number", "")
        if not _looks_like_wo_number(wo):
            warnings.append({
                "row_index": i, "field": "wo_number",
                "message": f"'{wo}' doesn't look like a typical WO# (expected 6-10 digits) — double-check against the photo.",
            })
        payout = job.get("payout")
        commission = job.get("commission")
        if isinstance(payout, (int, float)) and isinstance(commission, (int, float)) and payout:
            expected = payout * 0.5
            if abs(commission - expected) > max(1.0, abs(expected) * 0.05):
                warnings.append({
                    "row_index": i, "field": "commission",
                    "message": f"Commission ${commission:.2f} isn't close to 50% of payout (${payout:.2f}) — "
                                "this is fine if the photo really shows that, just worth a glance.",
                })

    subtotal_payout = parsed.get("subtotal_payout")
    parts_deduction = parsed.get("parts_deduction") or 0
    total_commission = parsed.get("total_commission")
    job_commission_sum = sum(
        j.get("commission") or 0 for j in jobs if isinstance(j.get("commission"), (int, float))
    )
    if isinstance(total_commission, (int, float)):
        computed = job_commission_sum - parts_deduction
        if abs(computed - total_commission) > 0.01:
            warnings.append({
                "row_index": None, "field": "total_commission",
                "message": f"Sum of job commissions (${job_commission_sum:.2f}) minus parts deduction "
                            f"(${parts_deduction:.2f}) = ${computed:.2f}, which doesn't match the report's "
                            f"stated total (${total_commission:.2f}) — check for a missed or misread row.",
            })
    if subtotal_payout is not None and not isinstance(subtotal_payout, (int, float)):
        warnings.append({"row_index": None, "field": "subtotal_payout", "message": "Subtotal payout wasn't read as a number."})

    return warnings


def find_overlapping_confirmed_report(conn, period_start, period_end):
    """A confirmed report whose date range overlaps the given one — surfaced
    as a possible duplicate-upload warning, never auto-blocked (Winston does
    report jobs late, so overlapping periods aren't necessarily wrong)."""
    if not period_start or not period_end:
        return None
    row = conn.execute(
        """SELECT * FROM winston_reports
           WHERE status='confirmed' AND period_start<=? AND period_end>=?""",
        (period_end, period_start),
    ).fetchone()
    return dict(row) if row else None


def confirm_draft(conn, report_id, edited_rows, edited_totals):
    """The ONLY path that writes real winston_report_jobs rows. edited_rows /
    edited_totals come from the review screen's form — pre-filled from the
    parse, but may contain human corrections. Runs reconciliation immediately
    after, since the whole point of confirming is to know where things stand."""
    report = get_report(conn, report_id)
    if not report:
        return None, "not_found"
    if report["status"] == "confirmed":
        return report, "already_confirmed"

    for row in edited_rows:
        conn.execute(
            """INSERT INTO winston_report_jobs (report_id, report_date, wo_number, total_sales, payout, commission, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                report_id,
                str(row.get("date", "")).strip(),
                str(row.get("wo_number", "")).strip(),
                row.get("total_sales"),
                row.get("payout"),
                row.get("commission"),
                str(row.get("notes", "") or "").strip(),
            ),
        )

    conn.execute(
        """UPDATE winston_reports SET
            status='confirmed', processed_at=datetime('now','localtime'),
            tech_name=?, period_start=?, period_end=?,
            subtotal_sales=?, subtotal_payout=?, parts_deduction=?, total_commission=?
           WHERE id=?""",
        (
            str(edited_totals.get("tech_name", report["tech_name"])).strip(),
            str(edited_totals.get("period_start", report["period_start"])).strip(),
            str(edited_totals.get("period_end", report["period_end"])).strip(),
            edited_totals.get("subtotal_sales", report["subtotal_sales"]),
            edited_totals.get("subtotal_payout", report["subtotal_payout"]),
            edited_totals.get("parts_deduction", report["parts_deduction"]),
            edited_totals.get("total_commission", report["total_commission"]),
            report_id,
        ),
    )
    conn.commit()

    events_mod.log_event(
        conn, "info", "winston_reports", "winston_report.confirmed",
        f"Winston report #{report_id} confirmed ({len(edited_rows)} jobs)",
        detail={"report_id": report_id, "job_count": len(edited_rows)},
        ref=str(report_id),
    )
    conn.commit()

    reconcile_against_report(conn, report_id)
    return get_report(conn, report_id), None


def report_jobs(conn, report_id):
    rows = conn.execute(
        "SELECT * FROM winston_report_jobs WHERE report_id=? ORDER BY report_date, wo_number", (report_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def reconcile_against_report(conn, report_id):
    """Implements AGENT_CONTEXT §14 / PRD §4.4.2. Run only against a confirmed
    report. captured_not_reported is intentionally a standing (all-time)
    query, not scoped to this report's period — Winston can report a job
    weeks late, so a capture with no match yet isn't necessarily wrong."""
    jobs = report_jobs(conn, report_id)
    job_wos = {j["wo_number"] for j in jobs}

    matched, winston_only = [], []
    for j in jobs:
        capture = conn.execute(
            "SELECT * FROM job_captures WHERE wo_number=? LIMIT 1", (j["wo_number"],)
        ).fetchone()
        if capture:
            matched.append(j)
        else:
            winston_only.append(j)

    all_report_wos = {
        r["wo_number"] for r in conn.execute(
            """SELECT DISTINCT wrj.wo_number FROM winston_report_jobs wrj
               JOIN winston_reports wr ON wr.id = wrj.report_id
               WHERE wr.status='confirmed'"""
        ).fetchall()
    }
    captures = conn.execute("SELECT * FROM job_captures").fetchall()

    captured_not_reported, disputes_found = [], []
    for c in captures:
        c = dict(c)
        if c["wo_number"] in all_report_wos:
            continue
        expected = conn.execute(
            "SELECT * FROM expected_jobs WHERE wo_number=?", (c["wo_number"],)
        ).fetchone()
        if expected and expected["status"] == "removed_by_dispatch":
            existing_dispute = conn.execute(
                "SELECT id FROM disputes WHERE wo_number=?", (c["wo_number"],)
            ).fetchone()
            if not existing_dispute:
                conn.execute(
                    "INSERT INTO disputes (wo_number, capture_id) VALUES (?, ?)",
                    (c["wo_number"], c["id"]),
                )
                conn.commit()
            disputes_found.append(c)
        else:
            captured_not_reported.append(c)

    return {
        "matched": matched,
        "winston_only": winston_only,
        "captured_not_reported": captured_not_reported,
        "disputes": disputes_found,
    }


def commission_summary(conn, report_id):
    """Winston's commission column is authoritative — never recomputed, only
    cross-checked against the report's own stated total (per AGENT_CONTEXT §13)."""
    jobs = report_jobs(conn, report_id)
    report = get_report(conn, report_id)
    job_sum = sum(j["commission"] or 0 for j in jobs)
    parts_deduction = (report or {}).get("parts_deduction") or 0
    computed_net = job_sum - parts_deduction
    stated_total = (report or {}).get("total_commission")
    mismatch = (
        stated_total is not None and abs(computed_net - stated_total) > 0.01
    )
    return {
        "job_commission_sum": job_sum,
        "parts_deduction": parts_deduction,
        "computed_net": computed_net,
        "stated_total": stated_total,
        "mismatch": mismatch,
    }
