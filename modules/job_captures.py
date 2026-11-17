"""Job capture — the independent evidence record for a completed job.

NOT a door order and NOT an inventory deduction: separate table, separate
workflow. A tech logs closing notes + payment info (+ receipt photo) for a
job in the field so DoorFlex has proof the job happened even if Winston
later deletes it from Dispatch.me.

`expected_jobs` is the "what should happen today" side. Primary source is now
a tech-uploaded screenshot of their own Dispatch "My Jobs" view, parsed via
OpenAI vision (see parse_dispatch_screenshot below) — no Dispatch.me scraper
(tech-role accounts appear to have no web login to scrape, and Winston report
reconciliation already provides a delayed safety net). Manual dashboard entry
still works as a fallback. A capture is matched to expected_jobs by
`wo_number` when present, but a capture is accepted even if no matching
`expected_jobs` row exists (a tech can always log a job that wasn't on the
list).
"""
import base64
import json
import re

from modules import events as events_mod

PAYMENT_METHODS = {"card", "cash", "check", "not_collected"}
WO_NUMBER_RE = re.compile(r"^\d{6,10}$")


def get_by_batch_id(conn, batch_id):
    row = conn.execute("SELECT * FROM job_captures WHERE batch_id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def is_wo_closed(conn, wo_number):
    """A WO is 'closed' once any job_captures row exists for it — closing is
    now one-shot per WO (enforced in create_job_capture below), so existence
    alone is sufficient. Used to reject late deductions/door orders/duplicate
    closes against a WO the tech already finished."""
    wo_number = str(wo_number or "").strip()
    if not wo_number:
        return False
    return conn.execute(
        "SELECT 1 FROM job_captures WHERE wo_number=? LIMIT 1", (wo_number,)
    ).fetchone() is not None


def get_captures_by_wo(conn, wo_numbers):
    """Most recent job_captures row per wo_number, for the given list. Backs
    the "Charged" column alongside logger.get_parts_used_by_wo's "Parts
    Used" — together they're the invoice-parity view (charge + parts) Nick
    described for out-of-pocket jobs. A WO with no capture yet just won't
    have a key here, not an empty/zero row."""
    wo_numbers = [w for w in (wo_numbers or []) if w]
    if not wo_numbers:
        return {}
    placeholders = ",".join("?" for _ in wo_numbers)
    rows = conn.execute(
        f"""SELECT * FROM job_captures WHERE wo_number IN ({placeholders})
            ORDER BY received_at DESC, id DESC""",
        wo_numbers,
    ).fetchall()
    result = {}
    for r in rows:
        d = dict(r)
        result.setdefault(d["wo_number"], d)  # first hit per WO is the most recent (ORDER BY above)
    return result


def create_job_capture(conn, tech, payload):
    """Idempotent on batch_id: a retry with the same batch_id returns the
    existing row instead of creating a duplicate (mirrors deduction_batches).
    """
    batch_id = str(payload.get("batch_id", "")).strip()
    if not batch_id:
        return None, "missing_batch_id"

    existing = get_by_batch_id(conn, batch_id)
    if existing:
        return existing, "already_processed"

    wo_number = str(payload.get("wo_number", "")).strip()
    if not wo_number:
        return None, "missing_wo_number"

    if is_wo_closed(conn, wo_number):
        return None, "wo_already_closed"

    payment_method = str(payload.get("payment_method", "")).strip()
    if payment_method not in PAYMENT_METHODS:
        return None, "invalid_payment_method"

    closing_notes = str(payload.get("closing_notes", "")).strip()
    if not closing_notes:
        return None, "missing_closing_notes"

    amount_charged = payload.get("amount_charged")
    try:
        amount_charged = float(amount_charged) if amount_charged not in (None, "") else None
    except (TypeError, ValueError):
        amount_charged = None

    conn.execute(
        """INSERT INTO job_captures (
            batch_id, wo_number, tech_id, amount_charged, payment_method,
            check_number, not_collected_reason, closing_notes,
            receipt_photo_path, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id,
            wo_number,
            tech["id"],
            amount_charged,
            payment_method,
            str(payload.get("check_number", "")).strip(),
            str(payload.get("not_collected_reason", "")).strip(),
            closing_notes,
            str(payload.get("receipt_photo_path", "")).strip(),
            str(payload.get("captured_at", "")).strip(),
        ),
    )
    conn.commit()

    new_status = "captured" if payment_method != "not_collected" else "not_collected"
    conn.execute(
        "UPDATE expected_jobs SET status=?, updated_at=datetime('now','localtime') WHERE wo_number=?",
        (new_status, wo_number),
    )
    conn.commit()

    capture = get_by_batch_id(conn, batch_id)
    events_mod.log_event(
        conn, "info", "pwa", "job_capture.captured",
        f"Job captured by {tech['name']}: WO {wo_number} ({payment_method})",
        detail={"tech": tech["name"], "wo_number": wo_number, "payment_method": payment_method},
        ref=wo_number,
    )
    conn.commit()
    return capture, "processed"


def set_photo_path(conn, batch_id, photo_path):
    conn.execute(
        "UPDATE job_captures SET receipt_photo_path=? WHERE batch_id=?",
        (photo_path, batch_id),
    )
    conn.commit()


# ─── Expected jobs (manual entry, replaces the deferred Dispatch scraper) ───


def list_jobs_for_tech_today(conn, tech_id, date_str):
    rows = conn.execute(
        """SELECT * FROM expected_jobs
           WHERE tech_id=? AND substr(window_start, 1, 10)=?
              OR (tech_id=? AND window_start='' AND substr(created_at, 1, 10)=?)
           ORDER BY window_start""",
        (tech_id, date_str, tech_id, date_str),
    ).fetchall()
    return [dict(r) for r in rows]


def list_jobs_for_date(conn, date_str):
    rows = conn.execute(
        """SELECT ej.*, t.name AS tech_name FROM expected_jobs ej
           LEFT JOIN techs t ON t.id = ej.tech_id
           WHERE substr(ej.window_start, 1, 10)=? OR (ej.window_start='' AND substr(ej.created_at, 1, 10)=?)
           ORDER BY ej.window_start, ej.created_at""",
        (date_str, date_str),
    ).fetchall()
    return [dict(r) for r in rows]


def create_expected_job(conn, payload):
    wo_number = str(payload.get("wo_number", "")).strip()
    if not wo_number:
        return None, "missing_wo_number"
    existing = conn.execute("SELECT id FROM expected_jobs WHERE wo_number=?", (wo_number,)).fetchone()
    if existing:
        return None, "duplicate_wo_number"

    tech_id = payload.get("tech_id") or None
    conn.execute(
        """INSERT INTO expected_jobs (
            wo_number, tech_id, address, city, state, zip,
            window_start, window_end, job_type, warranty_company, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual')""",
        (
            wo_number,
            tech_id,
            str(payload.get("address", "")).strip(),
            str(payload.get("city", "")).strip(),
            str(payload.get("state", "")).strip(),
            str(payload.get("zip", "")).strip(),
            str(payload.get("window_start", "")).strip(),
            str(payload.get("window_end", "")).strip(),
            str(payload.get("job_type", "")).strip(),
            str(payload.get("warranty_company", "")).strip(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM expected_jobs WHERE wo_number=?", (wo_number,)).fetchone()
    return dict(row), None


def mark_removed_by_dispatch(conn, expected_job_id):
    """Winston deleted this job — never actually deletes the row (that's the
    evidence trail), just flags it. Auto-creates a dispute if a capture
    already exists for this WO."""
    job = conn.execute("SELECT * FROM expected_jobs WHERE id=?", (expected_job_id,)).fetchone()
    if not job:
        return None, "not_found"
    conn.execute(
        "UPDATE expected_jobs SET status='removed_by_dispatch', "
        "removed_at=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE id=?",
        (expected_job_id,),
    )
    capture = conn.execute(
        "SELECT id FROM job_captures WHERE wo_number=? ORDER BY id DESC LIMIT 1", (job["wo_number"],)
    ).fetchone()
    conn.execute(
        "INSERT INTO disputes (wo_number, capture_id) VALUES (?, ?)",
        (job["wo_number"], capture["id"] if capture else None),
    )
    conn.commit()
    events_mod.log_event(
        conn, "warn", "job_captures", "dispute.created",
        f"WO {job['wo_number']} marked removed by Dispatch — dispute opened",
        detail={"wo_number": job["wo_number"], "has_capture": capture is not None},
        ref=job["wo_number"],
    )
    conn.commit()
    return job["wo_number"], None


# ─── Reconciliation ─────────────────────────────────────────────────────────


def compute_reconciliation(conn, date_str):
    """4 buckets per PRD §4.5, computed purely from expected_jobs + job_captures
    (no Winston report data needed for this — that's a separate, deferred
    dispute-matching flow in PRD §4.4.2)."""
    jobs = list_jobs_for_date(conn, date_str)
    captured_wos = {
        r["wo_number"] for r in conn.execute(
            "SELECT DISTINCT wo_number FROM job_captures WHERE substr(received_at, 1, 10)=?",
            (date_str,),
        ).fetchall()
    }

    matched, not_collected, uncaptured, removed = [], [], [], []
    for job in jobs:
        if job["status"] == "removed_by_dispatch":
            removed.append(job)
        elif job["wo_number"] in captured_wos and job["status"] == "captured":
            matched.append(job)
        elif job["status"] == "not_collected":
            not_collected.append(job)
        elif job["wo_number"] not in captured_wos:
            uncaptured.append(job)

    return {
        "date": date_str,
        "matched": matched,
        "not_collected": not_collected,
        "uncaptured": uncaptured,
        "removed": removed,
    }


def save_reconciliation_run(conn, result, report_sent=False):
    from datetime import datetime

    sent_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if report_sent else ""
    conn.execute(
        """INSERT INTO reconciliation_runs (
            run_date, matched_count, not_collected_count, uncaptured_count,
            removed_count, report_sent_at
        ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            result["date"],
            len(result["matched"]),
            len(result["not_collected"]),
            len(result["uncaptured"]),
            len(result["removed"]),
            sent_at,
        ),
    )
    conn.commit()


# ─── Techs with unclosed jobs (for the 6pm push reminder) ──────────────────


def techs_with_uncaptured_jobs_today(conn, date_str):
    """Server-side proxy for PRD §4.3.1's 'unsynced job_capture_queue' check
    (that IndexedDB store lives client-side, invisible to the server) —
    techs who have an expected job today with no matching capture yet."""
    rows = conn.execute(
        """SELECT DISTINCT ej.tech_id FROM expected_jobs ej
           WHERE (substr(ej.window_start, 1, 10)=? OR (ej.window_start='' AND substr(ej.created_at, 1, 10)=?))
             AND ej.tech_id IS NOT NULL
             AND ej.status NOT IN ('captured', 'not_collected', 'removed_by_dispatch')""",
        (date_str, date_str),
    ).fetchall()
    return [r["tech_id"] for r in rows]


# ─── Dispatch screenshot capture (replaces the scraper) ────────────────────
# The tech has full legitimate access to their own Dispatch "My Jobs" screen —
# a screenshot + vision parse is zero-ToS-risk, unlike scraping. Same
# parse -> review -> confirm shape as modules/winston_reports.py: nothing
# here writes to expected_jobs directly from the raw AI parse.

SCREENSHOT_PROMPT = """This is a screenshot of a technician's "My Jobs" list in the \
Dispatch.me app. Each job shows a title formatted as "[Type] - [WO#] [Company \
abbreviation]..." (e.g. "In Person - 73012439 ELC N..."), an address below it \
(street, city, state, zip), and a time window (e.g. "8:00am - 1:00pm MST"). \
Extract every job visible in the screenshot. Return JSON only, no other text:
{
  "jobs": [
    {
      "wo_number": "string",
      "job_type": "string or null",
      "address": "string or null",
      "city": "string or null",
      "state": "string or null",
      "zip": "string or null",
      "window_start": "string or null (as shown, e.g. '8:00am')",
      "window_end": "string or null"
    }
  ]
}

wo_number MUST always be a JSON string, never a number. If a field isn't
visible, use null rather than guessing."""


def parse_dispatch_screenshot(image_bytes, content_type="image/jpeg"):
    """Vision call extracting the tech's job list from their own Dispatch app
    screenshot. Raises on API error — caller decides how to surface it.
    Returns the parsed list only; nothing is written to expected_jobs here."""
    from openai import OpenAI
    from config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_VISION_MODEL
    from modules.image_utils import downscale_for_vision
    from config import DEMO_MODE
    if DEMO_MODE:
        from modules.demo_runtime import consume_vision_call
        consume_vision_call()

    image_bytes, content_type = downscale_for_vision(image_bytes, content_type)
    client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL, timeout=20.0)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    resp = client.chat.completions.create(
        model=OPENAI_VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": SCREENSHOT_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64}"}},
            ],
        }],
        response_format={"type": "json_object"},
        max_tokens=1500,
    )
    parsed = json.loads(resp.choices[0].message.content)
    return parsed.get("jobs", []) or []


def validate_screenshot_jobs(parsed_jobs):
    """Pure function — same shape as winston_reports.validate_draft. Flags,
    never blocks: a WO# that doesn't look like the expected digit pattern is
    still shown to the tech to confirm/correct, not silently dropped."""
    warnings = []
    for i, job in enumerate(parsed_jobs):
        wo = job.get("wo_number", "")
        if not WO_NUMBER_RE.match(str(wo).strip()):
            warnings.append({
                "row_index": i, "field": "wo_number",
                "message": f"'{wo}' doesn't look like a typical WO# — double-check against the screenshot.",
            })
    return warnings


def _parse_time_to_24h(time_str):
    """'8:00am', '1:00 PM', '13:00' -> (hour, minute) in 24h. None if unparseable."""
    if not time_str:
        return None
    s = str(time_str).strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)?", s)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def format_window_time(value):
    """Render a stored window_start/window_end value for display, e.g.
    '8:00 AM'. Tolerates the 'YYYY-MM-DDTHH:MM' format screenshot imports
    produce (_today_window above) as well as a bare time string from manual
    dashboard entry. Falls back to the raw value unchanged if neither
    parses — a formatting miss should never hide the underlying data."""
    value = str(value or "").strip()
    if not value:
        return ""
    time_part = value.split("T", 1)[1] if "T" in value else value
    parsed = _parse_time_to_24h(time_part)
    if not parsed:
        return value
    hour, minute = parsed
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {period}"


def _today_window(time_str):
    """Dispatch screenshots show a time-of-day only ('8:00am'), never a date —
    the screenshot always represents TODAY's schedule (that's the point of the
    feature), so the date component is always today's. This must produce a
    string starting with YYYY-MM-DD or list_jobs_for_tech_today/list_jobs_for_date's
    substr(window_start,1,10) date filter will silently never match it."""
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    parsed = _parse_time_to_24h(time_str)
    if not parsed:
        return today
    hour, minute = parsed
    return f"{today}T{hour:02d}:{minute:02d}"


def upsert_from_screenshot(conn, tech_id, confirmed_jobs):
    """Only ever called with the tech's CONFIRMED (possibly corrected) list —
    never the raw parse directly. Upserts expected_jobs by wo_number; leaves
    `status` alone on an existing row so a re-upload (routes change several
    times a day) never resets an in-progress/captured job back to pending."""
    created, updated = 0, 0
    for job in confirmed_jobs:
        wo_number = str(job.get("wo_number", "")).strip()
        if not wo_number:
            continue
        window_start = _today_window(job.get("window_start"))
        window_end = _today_window(job.get("window_end")) if job.get("window_end") else ""
        existing = conn.execute("SELECT id FROM expected_jobs WHERE wo_number=?", (wo_number,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE expected_jobs SET
                    tech_id=?, address=?, city=?, state=?, zip=?,
                    window_start=?, window_end=?, job_type=?,
                    updated_at=datetime('now','localtime')
                   WHERE wo_number=?""",
                (
                    tech_id,
                    str(job.get("address", "") or "").strip(),
                    str(job.get("city", "") or "").strip(),
                    str(job.get("state", "") or "").strip(),
                    str(job.get("zip", "") or "").strip(),
                    window_start,
                    window_end,
                    str(job.get("job_type", "") or "").strip(),
                    wo_number,
                ),
            )
            updated += 1
        else:
            conn.execute(
                """INSERT INTO expected_jobs (
                    wo_number, tech_id, address, city, state, zip,
                    window_start, window_end, job_type, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'screenshot')""",
                (
                    wo_number,
                    tech_id,
                    str(job.get("address", "") or "").strip(),
                    str(job.get("city", "") or "").strip(),
                    str(job.get("state", "") or "").strip(),
                    str(job.get("zip", "") or "").strip(),
                    window_start,
                    window_end,
                    str(job.get("job_type", "") or "").strip(),
                ),
            )
            created += 1
    conn.commit()
    events_mod.log_event(
        conn, "info", "job_captures", "jobs.screenshot_confirmed",
        f"Tech screenshot upload: {created} created, {updated} updated",
        detail={"tech_id": tech_id, "created": created, "updated": updated},
    )
    conn.commit()
    return {"created": created, "updated": updated}
