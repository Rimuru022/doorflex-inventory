"""Public demo (DEMO_MODE): the synthetic dataset and the demo deploy's
guarantees - every screen renders with data, and nothing leaves the server."""
import importlib
import json
from datetime import date, timedelta

import pytest

from modules import demo_seed

TODAY = date(2026, 11, 17)  # a Tuesday


@pytest.fixture(scope="module")
def demo_conn(tmp_path_factory):
    import sqlite3
    from database import SCHEMA, _migrate, _seed_distributors

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    _seed_distributors(conn)
    summary = demo_seed.build_demo(conn, today=TODAY, assets_dir=tmp_path_factory.mktemp("assets"))
    yield conn, summary
    conn.close()


def scalar(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_history_spans_six_months_and_never_reaches_the_future(demo_conn):
    conn, summary = demo_conn
    assert summary["invoices"] > 500
    first, last = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM log").fetchone()
    assert first[:10] <= (TODAY - timedelta(days=175)).isoformat()
    assert last[:10] == TODAY.isoformat()
    for table, col in (("log", "timestamp"), ("app_events", "ts"), ("job_captures", "received_at"),
                       ("expected_jobs", "created_at"), ("winston_reports", "received_at")):
        assert scalar(conn, f"SELECT MAX({col}) FROM {table}")[:10] <= TODAY.isoformat(), table


def test_every_removed_job_has_a_dispute_with_capture_evidence(demo_conn):
    conn, _ = demo_conn
    removed = scalar(conn, "SELECT COUNT(*) FROM expected_jobs WHERE status='removed_by_dispatch'")
    assert removed >= 2
    rows = conn.execute(
        """SELECT d.status, jc.amount_charged, jc.closing_notes, jc.receipt_photo_path
           FROM disputes d JOIN job_captures jc ON jc.id = d.capture_id""").fetchall()
    assert len(rows) == removed
    assert {r["status"] for r in rows} >= {"new", "sent", "resolved"}
    assert all(r["amount_charged"] and r["closing_notes"] and r["receipt_photo_path"] for r in rows)


def test_latest_winston_report_waits_for_review_with_a_flagged_misread(demo_conn):
    from modules import winston_reports

    conn, _ = demo_conn
    confirmed = winston_reports.list_reports(conn, status="confirmed")
    drafts = winston_reports.list_reports(conn, status="draft")
    assert len(confirmed) >= 2 and len(drafts) == 1
    warnings = winston_reports.validate_draft(json.loads(drafts[0]["parsed_json"]))
    assert [w["field"] for w in warnings] == ["wo_number"]
    for r in confirmed:
        assert not winston_reports.commission_summary(conn, r["id"])["mismatch"]


def test_disputed_jobs_are_missing_from_winston_reports(demo_conn):
    conn, _ = demo_conn
    leaked = scalar(conn, """SELECT COUNT(*) FROM winston_report_jobs wrj
                             JOIN expected_jobs ej ON ej.wo_number = wrj.wo_number
                             WHERE ej.status='removed_by_dispatch'""")
    assert leaked == 0


def test_unmatched_queue_holds_human_decisions_only(demo_conn):
    conn, _ = demo_conn
    open_rows = conn.execute("SELECT kind, raw_name FROM unmatched_parts WHERE mapped_to_part_id IS NULL").fetchall()
    names = {r["raw_name"] for r in open_rows}
    # A different spring size must wait for a human, never auto-deduct.
    assert "Torsion Spring .243 x 2 x 36" in names
    assert "Brandon K" in names  # unknown tech
    assert scalar(conn, "SELECT COUNT(*) FROM log WHERE raw_text='Torsion Spring .243 x 2 x 36'") == 0


def test_only_recent_shortfalls_stay_open(demo_conn):
    conn, _ = demo_conn
    stale = scalar(conn, "SELECT COUNT(*) FROM stock_alerts WHERE resolved=0 AND created_at < ?",
                   (TODAY - timedelta(days=5)).isoformat())
    assert stale == 0


def test_door_orders_cover_the_whole_workflow(demo_conn):
    conn, _ = demo_conn
    statuses = {r[0] for r in conn.execute("SELECT status FROM door_orders")}
    assert statuses == {"captured", "ordered", "installed"}


def test_build_is_deterministic(tmp_path):
    import sqlite3
    from database import SCHEMA, _migrate, _seed_distributors

    def build():
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript(SCHEMA)
        _migrate(c)
        _seed_distributors(c)
        demo_seed.build_demo(c, today=TODAY, assets_dir=None)
        rows = c.execute("SELECT part_id, quantity_change, timestamp FROM log ORDER BY id").fetchall()
        return [tuple(r) for r in rows]

    assert build() == build()


# ─── The demo deploy itself ───────────────────────────────────────────────────


@pytest.fixture()
def demo_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "inventory.db"))
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("SECRET_KEY", "test")
    import config as cfg
    import database as db
    import main as main_mod
    for m in (cfg, db, main_mod):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    with TestClient(main_mod.app) as client:
        yield client
    monkeypatch.delenv("DEMO_MODE")
    for m in (cfg, db, main_mod):
        importlib.reload(m)


def test_demo_deploy_renders_every_screen_and_sends_nothing(demo_client, monkeypatch):
    import requests

    def no_network(*a, **k):
        raise AssertionError("demo mode must never call an external service")
    monkeypatch.setattr(requests, "post", no_network)

    assert demo_client.get("/", follow_redirects=False).headers["location"] == "/demo"
    landing = demo_client.get("/demo")
    assert landing.status_code == 200 and "Entrar al dashboard" in landing.text
    assert demo_client.get("/demo/samples/invoice.pdf").content[:4] == b"%PDF"
    assert demo_client.get("/demo/samples/winston-report.jpg").content[:2] == b"\xff\xd8"
    assert demo_client.get("/tech/pwa/?t=demo-tech-andre").status_code == 200

    r = demo_client.post("/demo/enter", data={"next": "/dashboard/disputes"}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard/disputes"
    for path in ("/dashboard", "/dashboard/disputes", "/dashboard/commission", "/dashboard/unmatched",
                 "/dashboard/jobs-today", "/dashboard/overview-today", "/dashboard/stock", "/dashboard/log",
                 "/dashboard/door-orders", "/dashboard/reorders", "/dashboard/alerts", "/dashboard/usage",
                 "/dashboard/top-movers", "/dashboard/parts", "/dashboard/techs"):
        page = demo_client.get(path)
        assert page.status_code == 200, path
        assert "Datos ficticios" in page.text, path

    # The sample invoice goes through the real parser and deducts stock;
    # its off-catalog line lands in Unmatched.
    pdf = demo_client.get("/demo/samples/invoice.pdf").content
    up = demo_client.post("/dashboard/upload-invoice", files={"file": ("Invoice_1999_demo.pdf", pdf, "application/pdf")})
    assert up.status_code == 200
    import database as db
    conn = db.get_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM log WHERE invoice_name='Invoice_1999_demo.pdf'").fetchone()[0] >= 3
        assert conn.execute("SELECT COUNT(*) FROM unmatched_parts WHERE raw_name='Hinge #4 heavy duty' "
                            "AND invoice_name='Invoice_1999_demo.pdf'").fetchone()[0] == 1
    finally:
        conn.close()


def test_demo_entry_cannot_redirect_off_site(demo_client):
    r = demo_client.post("/demo/enter", data={"next": "https://evil.example/"}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard"


def test_demo_routes_do_not_exist_outside_demo_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "inventory.db"))
    monkeypatch.delenv("DEMO_MODE", raising=False)
    import config as cfg
    import database as db
    import main as main_mod
    for m in (cfg, db, main_mod):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    client = TestClient(main_mod.app)
    assert client.get("/demo").status_code == 404
    assert client.post("/demo/enter").status_code == 404
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
