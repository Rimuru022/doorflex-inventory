"""Winston report parsing/reconciliation tests. The vision call itself
(parse_report_image) is never exercised here — only the draft/validate/
confirm/reconcile lifecycle, which is where the actual business logic and
money/WO# correctness live. Mirrors test_job_captures.py's fixture style."""
import pytest

from modules import winston_reports as wr
from modules import pwa


@pytest.fixture()
def tech_env(conn):
    conn.execute("INSERT INTO warehouses (id, name) VALUES ('wh-sub', 'Total Garage Solutions')")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-DEMO', 'Demo', 'wh-sub')")
    cur = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
        "VALUES ('Nick', 'T-DEMO', 'wh-sub', 'pwa')"
    )
    token = pwa.rotate_tech_token(conn, cur.lastrowid)
    conn.commit()
    tech = pwa.get_tech_by_token(conn, token)
    return conn, tech


SAMPLE_PARSED = {
    "tech_name": "Nick",
    "period_start": "2026-07-27",
    "period_end": "2026-08-01",
    "jobs": [
        {"date": "2026-07-27", "wo_number": "25699148", "total_sales": 839.32, "payout": 769.00, "commission": 384.50, "notes": None},
        {"date": "2026-07-28", "wo_number": "66992469", "total_sales": None, "payout": 100.00, "commission": 50.00, "notes": None},
    ],
    "subtotal_sales": 839.32,
    "subtotal_payout": 869.00,
    "parts_deduction": 0,
    "total_commission": 434.50,
}


# ─── Draft lifecycle ────────────────────────────────────────────────────────


def test_create_draft_does_not_write_report_jobs(conn):
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    report = wr.get_report(conn, report_id)
    assert report["status"] == "draft"
    assert report["tech_name"] == "Nick"
    jobs = wr.report_jobs(conn, report_id)
    assert jobs == []  # nothing written until confirm


def test_validate_draft_flags_malformed_wo_number():
    bad = {**SAMPLE_PARSED, "jobs": [{**SAMPLE_PARSED["jobs"][0], "wo_number": "ABC123"}]}
    warnings = wr.validate_draft(bad)
    assert any(w["field"] == "wo_number" for w in warnings)


def test_validate_draft_flags_arithmetic_mismatch():
    bad = {**SAMPLE_PARSED, "total_commission": 999.99}
    warnings = wr.validate_draft(bad)
    assert any(w["field"] == "total_commission" for w in warnings)


def test_validate_draft_clean_data_has_no_warnings():
    warnings = wr.validate_draft(SAMPLE_PARSED)
    assert warnings == []


def test_confirm_draft_uses_edited_values_not_raw_parse(conn):
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    edited_rows = [
        {"date": "2026-07-27", "wo_number": "25699149", "total_sales": 839.32, "payout": 769.00, "commission": 384.50, "notes": "corrected WO#"},
    ]
    edited_totals = {**SAMPLE_PARSED, "total_commission": 384.50, "parts_deduction": 0}
    report, err = wr.confirm_draft(conn, report_id, edited_rows, edited_totals)
    assert err is None
    assert report["status"] == "confirmed"

    jobs = wr.report_jobs(conn, report_id)
    assert len(jobs) == 1
    assert jobs[0]["wo_number"] == "25699149"  # the corrected value, not the original "25699148"
    assert jobs[0]["notes"] == "corrected WO#"


def test_confirm_draft_twice_is_rejected(conn):
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], SAMPLE_PARSED)
    report, err = wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], SAMPLE_PARSED)
    assert err == "already_confirmed"
    jobs = wr.report_jobs(conn, report_id)
    assert len(jobs) == 2  # not duplicated


def test_confirm_draft_not_found(conn):
    report, err = wr.confirm_draft(conn, 9999, [], {})
    assert report is None
    assert err == "not_found"


def test_find_overlapping_confirmed_report(conn):
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], SAMPLE_PARSED)

    overlap = wr.find_overlapping_confirmed_report(conn, "2026-07-29", "2026-08-03")
    assert overlap is not None
    assert overlap["id"] == report_id

    no_overlap = wr.find_overlapping_confirmed_report(conn, "2026-09-01", "2026-09-07")
    assert no_overlap is None


# ─── Reconciliation ─────────────────────────────────────────────────────────


def test_reconcile_matched_and_winston_only(tech_env):
    conn, tech = tech_env
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], SAMPLE_PARSED)

    conn.execute(
        "INSERT INTO job_captures (batch_id, wo_number, tech_id, payment_method, closing_notes) "
        "VALUES ('b1', '25699148', ?, 'cash', 'done')", (tech["id"],),
    )
    conn.commit()

    result = wr.reconcile_against_report(conn, report_id)
    assert {j["wo_number"] for j in result["matched"]} == {"25699148"}
    assert {j["wo_number"] for j in result["winston_only"]} == {"66992469"}
    assert result["captured_not_reported"] == []
    assert result["disputes"] == []


def test_reconcile_captured_not_reported(tech_env):
    conn, tech = tech_env
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], SAMPLE_PARSED)

    # A capture for a WO not in the Winston report at all — pending, not a dispute.
    conn.execute(
        "INSERT INTO job_captures (batch_id, wo_number, tech_id, payment_method, closing_notes) "
        "VALUES ('b2', 'WO-NOT-YET-PAID', ?, 'cash', 'done')", (tech["id"],),
    )
    conn.commit()

    result = wr.reconcile_against_report(conn, report_id)
    assert any(c["wo_number"] == "WO-NOT-YET-PAID" for c in result["captured_not_reported"])
    assert result["disputes"] == []


def test_reconcile_creates_dispute_for_removed_by_dispatch_capture(tech_env):
    conn, tech = tech_env
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], SAMPLE_PARSED)

    conn.execute(
        "INSERT INTO expected_jobs (wo_number, status) VALUES ('WO-REMOVED', 'removed_by_dispatch')"
    )
    conn.execute(
        "INSERT INTO job_captures (batch_id, wo_number, tech_id, payment_method, closing_notes) "
        "VALUES ('b3', 'WO-REMOVED', ?, 'cash', 'done')", (tech["id"],),
    )
    conn.commit()

    result = wr.reconcile_against_report(conn, report_id)
    assert any(c["wo_number"] == "WO-REMOVED" for c in result["disputes"])
    dispute = conn.execute("SELECT * FROM disputes WHERE wo_number='WO-REMOVED'").fetchone()
    assert dispute is not None


def test_reconcile_does_not_duplicate_existing_dispute(tech_env):
    """If job_captures.mark_removed_by_dispatch already opened a dispute
    (the manual dashboard flow), reconciliation must not create a second one."""
    conn, tech = tech_env
    from modules import job_captures as jc

    job, _ = jc.create_expected_job(conn, {"wo_number": "WO-REMOVED"})
    jc.create_job_capture(conn, tech, {
        "batch_id": "b4", "wo_number": "WO-REMOVED", "payment_method": "cash",
        "closing_notes": "done", "captured_at": "2026-07-27T10:00:00",
    })
    jc.mark_removed_by_dispatch(conn, job["id"])
    assert conn.execute("SELECT COUNT(*) c FROM disputes").fetchone()["c"] == 1

    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], SAMPLE_PARSED)
    wr.reconcile_against_report(conn, report_id)

    assert conn.execute("SELECT COUNT(*) c FROM disputes").fetchone()["c"] == 1


# ─── Commission summary ─────────────────────────────────────────────────────


def test_commission_summary_matches_stated_total(conn):
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], SAMPLE_PARSED)
    summary = wr.commission_summary(conn, report_id)
    assert summary["job_commission_sum"] == 434.50
    assert summary["computed_net"] == 434.50
    assert summary["mismatch"] is False


def test_commission_summary_flags_mismatch(conn):
    report_id = wr.create_draft(conn, SAMPLE_PARSED, "photo.jpg")
    bad_totals = {**SAMPLE_PARSED, "total_commission": 999.99}
    wr.confirm_draft(conn, report_id, SAMPLE_PARSED["jobs"], bad_totals)
    summary = wr.commission_summary(conn, report_id)
    assert summary["mismatch"] is True


# ─── HTTP endpoints ─────────────────────────────────────────────────────────


def test_http_commission_upload_review_confirm(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("DISABLE_BACKGROUND_JOBS", "1")

    from database import init_db
    init_db(seed=True)

    import main as main_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main_app.winston_reports_mod, "parse_report_image", lambda *a, **kw: SAMPLE_PARSED)
    client = TestClient(main_app.app)

    from config import ADMIN_PASSWORD
    r = client.post("/login", data={"password": ADMIN_PASSWORD})
    assert r.status_code == 200

    files = {"file": ("winston.jpg", b"fakejpeg", "image/jpeg")}
    r = client.post("/dashboard/commission/upload", files=files)
    assert r.status_code == 200
    report_id = str(r.url).rstrip("/").split("/")[-1]

    r = client.get(f"/dashboard/commission/review/{report_id}")
    assert r.status_code == 200
    assert "25699148" in r.text

    form = {
        "tech_name": "Nick", "period_start": "2026-07-27", "period_end": "2026-08-01",
        "subtotal_sales": "839.32", "subtotal_payout": "869.00", "parts_deduction": "0", "total_commission": "434.50",
        "row_count": "2",
        "row-0-date": "2026-07-27", "row-0-wo_number": "25699148", "row-0-total_sales": "839.32",
        "row-0-payout": "769.00", "row-0-commission": "384.50", "row-0-notes": "",
        "row-1-date": "2026-07-28", "row-1-wo_number": "66992469", "row-1-total_sales": "",
        "row-1-payout": "100.00", "row-1-commission": "50.00", "row-1-notes": "",
    }
    r = client.post(f"/dashboard/commission/confirm/{report_id}", data=form)
    assert r.status_code == 200

    r = client.get(f"/dashboard/commission/review/{report_id}")
    assert "Confirmed Report" in r.text

    r = client.get(f"/dashboard/commission/export.csv?report_id={report_id}")
    assert r.status_code == 200
    assert "25699148" in r.text
