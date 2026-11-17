"""Job capture tests — idempotency, expected_jobs manual entry, reconciliation
buckets, and the generic HTTP path. Mirrors the fixture/style of
test_door_orders.py; job captures never touch stock or door_orders."""
import pytest

from modules import job_captures, pwa


@pytest.fixture()
def tech_env(conn):
    conn.execute("INSERT INTO warehouses (id, name) VALUES ('wh-sub', 'Total Garage Solutions')")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-DEMO', 'Demo', 'wh-sub')")
    cur = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
        "VALUES ('Winston', 'T-DEMO', 'wh-sub', 'pwa')"
    )
    token = pwa.rotate_tech_token(conn, cur.lastrowid)
    conn.commit()
    tech = pwa.get_tech_by_token(conn, token)
    return conn, tech


def _payload(batch_id="batch-1", wo_number="WO-1", payment_method="cash"):
    return {
        "batch_id": batch_id,
        "wo_number": wo_number,
        "payment_method": payment_method,
        "amount_charged": 250,
        "closing_notes": "Replaced spring, tested door, customer satisfied.",
        "captured_at": "2026-08-19T10:00:00",
    }


# ─── expected_jobs (manual entry) ──────────────────────────────────────────


def test_create_expected_job(tech_env):
    conn, tech = tech_env
    job, err = job_captures.create_expected_job(conn, {
        "wo_number": "WO-1", "tech_id": tech["id"], "address": "123 Main St",
        "job_type": "In Person",
    })
    assert err is None
    assert job["wo_number"] == "WO-1"
    assert job["status"] == "pending"
    assert job["source"] == "manual"


def test_create_expected_job_duplicate_wo_rejected(tech_env):
    conn, tech = tech_env
    job_captures.create_expected_job(conn, {"wo_number": "WO-1"})
    job, err = job_captures.create_expected_job(conn, {"wo_number": "WO-1"})
    assert job is None
    assert err == "duplicate_wo_number"


def test_create_expected_job_missing_wo_rejected(tech_env):
    conn, tech = tech_env
    job, err = job_captures.create_expected_job(conn, {"wo_number": ""})
    assert job is None
    assert err == "missing_wo_number"


# ─── Persistence: idempotency + status flow ────────────────────────────────


def test_create_job_capture_never_touches_stock(tech_env):
    conn, tech = tech_env
    conn.execute("INSERT INTO parts (id, name) VALUES ('SPR-207', 'Spring')")
    conn.execute("INSERT INTO stock (truck_id, part_id, quantity) VALUES ('T-DEMO', 'SPR-207', 5)")
    conn.commit()

    capture, status = job_captures.create_job_capture(conn, tech, _payload())
    assert status == "processed"
    assert capture["wo_number"] == "WO-1"
    assert capture["payment_method"] == "cash"

    qty = conn.execute("SELECT quantity FROM stock WHERE part_id='SPR-207'").fetchone()["quantity"]
    assert qty == 5  # untouched
    log_rows = conn.execute("SELECT COUNT(*) c FROM log").fetchone()["c"]
    assert log_rows == 0


def test_batch_id_idempotency(tech_env):
    conn, tech = tech_env
    first, status1 = job_captures.create_job_capture(conn, tech, _payload("batch-dup"))
    assert status1 == "processed"
    second, status2 = job_captures.create_job_capture(conn, tech, _payload("batch-dup"))
    assert status2 == "already_processed"
    assert second["id"] == first["id"]

    count = conn.execute("SELECT COUNT(*) c FROM job_captures WHERE batch_id='batch-dup'").fetchone()["c"]
    assert count == 1


def test_get_captures_by_wo_returns_most_recent_per_wo(tech_env):
    conn, tech = tech_env
    job_captures.create_job_capture(conn, tech, _payload("batch-1", "WO-1", "cash"))
    # A second close for the same WO is now rejected outright (closing is
    # one-shot per WO) — confirm it's rejected and the original capture wins.
    _, status = job_captures.create_job_capture(conn, tech, _payload("batch-2", "WO-1", "card"))
    assert status == "wo_already_closed"
    job_captures.create_job_capture(conn, tech, _payload("batch-3", "WO-2", "check"))

    result = job_captures.get_captures_by_wo(conn, ["WO-1", "WO-2", "WO-NEVER-CAPTURED"])
    assert "WO-NEVER-CAPTURED" not in result
    assert result["WO-1"]["payment_method"] == "cash"
    assert result["WO-2"]["payment_method"] == "check"
    assert job_captures.get_captures_by_wo(conn, []) == {}


def test_is_wo_closed(tech_env):
    conn, tech = tech_env
    assert job_captures.is_wo_closed(conn, "WO-1") is False
    job_captures.create_job_capture(conn, tech, _payload("batch-1", "WO-1", "cash"))
    assert job_captures.is_wo_closed(conn, "WO-1") is True
    assert job_captures.is_wo_closed(conn, "") is False


def test_create_job_capture_second_close_rejected_even_with_different_batch_id(tech_env):
    conn, tech = tech_env
    capture, status = job_captures.create_job_capture(conn, tech, _payload("batch-1", "WO-1", "cash"))
    assert status == "processed"
    result, status = job_captures.create_job_capture(conn, tech, _payload("batch-2", "WO-1", "check"))
    assert result is None
    assert status == "wo_already_closed"


def test_missing_batch_id_rejected(tech_env):
    conn, tech = tech_env
    payload = _payload()
    payload["batch_id"] = ""
    capture, status = job_captures.create_job_capture(conn, tech, payload)
    assert capture is None
    assert status == "missing_batch_id"


def test_missing_closing_notes_rejected(tech_env):
    conn, tech = tech_env
    payload = _payload()
    payload["closing_notes"] = ""
    capture, status = job_captures.create_job_capture(conn, tech, payload)
    assert capture is None
    assert status == "missing_closing_notes"


def test_invalid_payment_method_rejected(tech_env):
    conn, tech = tech_env
    payload = _payload(payment_method="bitcoin")
    capture, status = job_captures.create_job_capture(conn, tech, payload)
    assert capture is None
    assert status == "invalid_payment_method"


def test_capture_updates_matching_expected_job_status(tech_env):
    conn, tech = tech_env
    job_captures.create_expected_job(conn, {"wo_number": "WO-1"})
    job_captures.create_job_capture(conn, tech, _payload(payment_method="not_collected", batch_id="b2"))
    row = conn.execute("SELECT status FROM expected_jobs WHERE wo_number='WO-1'").fetchone()
    assert row["status"] == "not_collected"


def test_mark_removed_by_dispatch_opens_dispute_with_evidence(tech_env):
    conn, tech = tech_env
    job, _ = job_captures.create_expected_job(conn, {"wo_number": "WO-1"})
    capture, _ = job_captures.create_job_capture(conn, tech, _payload())

    wo, err = job_captures.mark_removed_by_dispatch(conn, job["id"])
    assert err is None
    assert wo == "WO-1"

    updated = conn.execute("SELECT * FROM expected_jobs WHERE id=?", (job["id"],)).fetchone()
    assert updated["status"] == "removed_by_dispatch"
    assert updated["removed_at"]

    dispute = conn.execute("SELECT * FROM disputes WHERE wo_number='WO-1'").fetchone()
    assert dispute is not None
    assert dispute["capture_id"] == capture["id"]
    assert dispute["status"] == "new"


def test_mark_removed_by_dispatch_never_deletes_the_row(tech_env):
    conn, tech = tech_env
    job, _ = job_captures.create_expected_job(conn, {"wo_number": "WO-1"})
    job_captures.mark_removed_by_dispatch(conn, job["id"])
    count = conn.execute("SELECT COUNT(*) c FROM expected_jobs WHERE wo_number='WO-1'").fetchone()["c"]
    assert count == 1  # evidence trail — never actually deleted


# ─── Reconciliation ─────────────────────────────────────────────────────────


def test_reconciliation_buckets(tech_env):
    conn, tech = tech_env
    date_str = "2026-08-19"
    conn.execute(
        "INSERT INTO expected_jobs (wo_number, window_start, status) VALUES "
        "('WO-A', '2026-08-19T08:00', 'captured'), "
        "('WO-B', '2026-08-19T09:00', 'not_collected'), "
        "('WO-C', '2026-08-19T10:00', 'pending'), "
        "('WO-D', '2026-08-19T11:00', 'removed_by_dispatch')"
    )
    conn.execute(
        "INSERT INTO job_captures (batch_id, wo_number, tech_id, payment_method, closing_notes, received_at) "
        "VALUES ('b-a', 'WO-A', ?, 'cash', 'done', '2026-08-19 10:00:00')",
        (tech["id"],),
    )
    conn.commit()

    result = job_captures.compute_reconciliation(conn, date_str)
    assert {j["wo_number"] for j in result["matched"]} == {"WO-A"}
    assert {j["wo_number"] for j in result["not_collected"]} == {"WO-B"}
    assert {j["wo_number"] for j in result["uncaptured"]} == {"WO-C"}
    assert {j["wo_number"] for j in result["removed"]} == {"WO-D"}


def test_save_reconciliation_run(tech_env):
    conn, tech = tech_env
    result = job_captures.compute_reconciliation(conn, "2026-08-19")
    job_captures.save_reconciliation_run(conn, result, report_sent=True)
    row = conn.execute("SELECT * FROM reconciliation_runs").fetchone()
    assert row["run_date"] == "2026-08-19"
    assert row["report_sent_at"]


# ─── HTTP endpoints ─────────────────────────────────────────────────────────


def test_http_job_capture_endpoints(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("DISABLE_BACKGROUND_JOBS", "1")

    from database import init_db
    init_db(seed=True)

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-DEMO', 'Demo', 'wh-sub')")
    cur = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
        "VALUES ('Winston', 'T-DEMO', 'wh-sub', 'pwa')")
    tech_id = cur.lastrowid
    raw = pwa.rotate_tech_token(conn, tech_id)
    conn.execute(
        "INSERT INTO expected_jobs (wo_number, tech_id, address, window_start) VALUES "
        "('HTTP-WO-1', ?, '123 Main St, Phoenix, AZ', datetime('now','localtime'))",
        (tech_id,),
    )
    conn.commit()
    conn.close()

    import main as main_app
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main_app, "send_html", lambda *a, **kw: {"id": "test-fake-id"})
    client = TestClient(main_app.app)

    r = client.get("/tech/jobs/today", params={"token": raw})
    assert r.status_code == 200
    assert len(r.json()["jobs"]) == 1
    assert r.json()["jobs"][0]["wo_number"] == "HTTP-WO-1"

    body = {**_payload("http-batch-1", "HTTP-WO-1"), "token": raw}
    r = client.post("/tech/job-captures", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "processed"
    assert data["capture"]["wo_number"] == "HTTP-WO-1"

    # duplicate submission is idempotent
    r = client.post("/tech/job-captures", json=body)
    assert r.json()["status"] == "already_processed"

    r = client.post("/tech/job-captures", json={**body, "token": "bad"})
    assert r.status_code == 401

    # photo upload
    files = {"file": ("receipt.jpg", b"fakejpegbytes", "image/jpeg")}
    r = client.post(
        "/tech/job-captures/photo",
        params={"token": raw},
        data={"batch_id": "http-batch-1"},
        files=files,
    )
    assert r.status_code == 200
    assert r.json()["receipt_photo_path"] == "http-batch-1.jpg"

    # push subscribe round trip (no VAPID key configured in tests — endpoint
    # still accepts and stores the subscription)
    r = client.post("/tech/push/subscribe", json={
        "token": raw, "endpoint": "https://push.example/xyz",
        "keys": {"p256dh": "x", "auth": "y"},
    })
    assert r.status_code == 200


# ─── Dispatch screenshot capture (replaces the scraper) ────────────────────


SCREENSHOT_JOBS = [
    {"wo_number": "73012439", "address": "123 Main St", "city": "Chandler", "state": "AZ",
     "zip": "85225", "window_start": "8:00am", "window_end": "1:00pm", "job_type": "In Person"},
    {"wo_number": "65503879", "address": "456 Oak Ave", "city": "Mesa", "state": "AZ",
     "zip": "85201", "window_start": "1:00pm", "window_end": "5:00pm", "job_type": "First Time Dispatch"},
]


def test_validate_screenshot_jobs_flags_malformed_wo(tech_env):
    bad = [{**SCREENSHOT_JOBS[0], "wo_number": "N/A"}]
    warnings = job_captures.validate_screenshot_jobs(bad)
    assert len(warnings) == 1
    assert warnings[0]["field"] == "wo_number"


def test_validate_screenshot_jobs_clean_has_no_warnings(tech_env):
    assert job_captures.validate_screenshot_jobs(SCREENSHOT_JOBS) == []


def test_upsert_from_screenshot_creates_new_jobs(tech_env):
    conn, tech = tech_env
    result = job_captures.upsert_from_screenshot(conn, tech["id"], SCREENSHOT_JOBS)
    assert result == {"created": 2, "updated": 0}
    rows = conn.execute("SELECT * FROM expected_jobs ORDER BY wo_number").fetchall()
    assert len(rows) == 2
    assert rows[0]["source"] == "screenshot"
    assert rows[0]["status"] == "pending"


def test_upsert_from_screenshot_reupload_does_not_reset_status(tech_env):
    """Routes change several times a day — a re-upload must never reset an
    in-progress/captured job back to 'pending'."""
    conn, tech = tech_env
    job_captures.upsert_from_screenshot(conn, tech["id"], SCREENSHOT_JOBS)
    conn.execute("UPDATE expected_jobs SET status='captured' WHERE wo_number='73012439'")
    conn.commit()

    result = job_captures.upsert_from_screenshot(conn, tech["id"], SCREENSHOT_JOBS)
    assert result == {"created": 0, "updated": 2}
    row = conn.execute("SELECT status FROM expected_jobs WHERE wo_number='73012439'").fetchone()
    assert row["status"] == "captured"  # untouched by the re-upload


def test_upsert_from_screenshot_ignores_blank_wo_number(tech_env):
    conn, tech = tech_env
    result = job_captures.upsert_from_screenshot(conn, tech["id"], [{"wo_number": "  "}])
    assert result == {"created": 0, "updated": 0}


def test_http_screenshot_parse_and_confirm(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("DISABLE_BACKGROUND_JOBS", "1")

    from database import init_db
    init_db(seed=True)

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-DEMO', 'Demo', 'wh-sub')")
    cur = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
        "VALUES ('Winston', 'T-DEMO', 'wh-sub', 'pwa')")
    raw = pwa.rotate_tech_token(conn, cur.lastrowid)
    conn.commit()
    conn.close()

    import main as main_app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main_app.job_captures_mod, "parse_dispatch_screenshot", lambda *a, **kw: SCREENSHOT_JOBS)
    client = TestClient(main_app.app)

    files = {"file": ("schedule.jpg", b"fakejpeg", "image/jpeg")}
    r = client.post("/tech/jobs/parse-screenshot", params={"token": raw}, files=files)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert len(data["jobs"]) == 2

    r = client.post("/tech/jobs/confirm-screenshot", json={"token": raw, "jobs": data["jobs"]})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "created": 2, "updated": 0}

    r = client.get("/tech/jobs/today", params={"token": raw})
    assert len(r.json()["jobs"]) == 2

    r = client.post("/tech/jobs/parse-screenshot", params={"token": "bad"}, files=files)
    assert r.status_code == 401


# ─── format_window_time ─────────────────────────────────────────────────────


def test_format_window_time_handles_screenshot_import_format():
    assert job_captures.format_window_time("2026-08-20T08:00") == "8:00 AM"
    assert job_captures.format_window_time("2026-08-20T13:00") == "1:00 PM"
    assert job_captures.format_window_time("2026-08-20T00:30") == "12:30 AM"
    assert job_captures.format_window_time("2026-08-20T12:00") == "12:00 PM"


def test_format_window_time_handles_bare_time_string():
    assert job_captures.format_window_time("8:00am") == "8:00 AM"
    assert job_captures.format_window_time("1:00 PM") == "1:00 PM"


def test_format_window_time_falls_back_to_raw_value_when_unparseable():
    assert job_captures.format_window_time("2026-08-20") == "2026-08-20"
    assert job_captures.format_window_time("whenever works") == "whenever works"
    assert job_captures.format_window_time("") == ""
    assert job_captures.format_window_time(None) == ""
