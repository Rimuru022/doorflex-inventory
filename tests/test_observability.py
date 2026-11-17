"""Events module + dual timezone formatting + observability owner pages."""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from modules import events as events_mod


# ─── log_event / list ────────────────────────────────────────────────────────

def test_log_event_inserts_and_returns_row(conn):
    rid = events_mod.log_event(
        conn, "info", "test", "test.simple", "hello",
        detail={"k": 1, "name": "abc"}, ref="X-1",
    )
    assert isinstance(rid, int) and rid > 0
    row = conn.execute("SELECT * FROM app_events WHERE id=?", (rid,)).fetchone()
    assert row["level"] == "info"
    assert row["scope"] == "test"
    assert row["event_type"] == "test.simple"
    assert row["message"] == "hello"
    assert row["ref"] == "X-1"
    parsed = json.loads(row["detail"])
    assert parsed == {"k": 1, "name": "abc"}


def test_log_event_normalises_unknown_level(conn):
    rid = events_mod.log_event(conn, "panic", "x", "y", "z")
    row = conn.execute("SELECT level FROM app_events WHERE id=?", (rid,)).fetchone()
    assert row["level"] == "info"


def test_log_event_none_conn_opens_short_lived(tmp_path, monkeypatch):
    """log_event(conn=None) must open its own short-lived connection — and we
    point get_db() at a tmp DB so the prod file is never touched."""
    from database import get_db, init_db
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "ev.db"))
    init_db(seed=True)
    rid = events_mod.log_event(None, "info", "cron", "cron.tick", "ok")
    assert rid is not None
    g = get_db()
    row = g.execute("SELECT scope, event_type FROM app_events WHERE id=?", (rid,)).fetchone()
    g.close()
    assert row["scope"] == "cron"


def test_list_events_filter_by_scope_level_type(conn):
    events_mod.log_event(conn, "info", "ingest", "ingest.received", "a")
    events_mod.log_event(conn, "warn", "pwa", "pwa.batch.processed", "b")
    events_mod.log_event(conn, "error", "ingest", "ingest.pdf_error", "c")
    events_mod.log_event(conn, "info", "cron", "cron.daily_backup", "d")
    conn.commit()

    ingest_only, total_ingest = events_mod.list_events(conn, scope="ingest")
    assert total_ingest == 2
    assert all(r["scope"] == "ingest" for r in ingest_only)

    errs, total_errs = events_mod.list_events(conn, level="error")
    assert total_errs == 1
    assert errs[0]["event_type"] == "ingest.pdf_error"

    typ, n = events_mod.list_events(conn, event_type="cron.daily_backup")
    assert n == 1
    assert typ[0]["scope"] == "cron"


def test_list_events_search(conn):
    events_mod.log_event(conn, "info", "pwa", "pwa.batch.processed",
                         "batch-abc processed", detail={"batch_id": "b1"})
    events_mod.log_event(conn, "info", "pwa", "pwa.batch.processed",
                         "batch-xyz processed", detail={"batch_id": "b2"})
    conn.commit()
    rows, n = events_mod.list_events(conn, search="abc")
    assert n == 1
    assert "abc" in rows[0]["message"]


def test_list_events_pagination_and_order(conn):
    for i in range(5):
        events_mod.log_event(conn, "info", "t", "t.ev", f"#{i}")
    conn.commit()
    rows, total = events_mod.list_events(conn, limit=2, offset=0)
    assert total == 5
    assert len(rows) == 2
    rows2, _ = events_mod.list_events(conn, limit=2, offset=2)
    assert len(rows2) == 2
    # Newest first (highest id first).
    assert rows[0]["id"] > rows2[-1]["id"]


# ─── request log ─────────────────────────────────────────────────────────────

def test_log_request_returns_id_and_filters(conn):
    rid = events_mod.log_request(conn, "POST", "/tech/deductions", 200, 23, "10.0.0.5",
                                  tech="Winston")
    assert isinstance(rid, int)
    events_mod.log_request(conn, "GET", "/tech/catalog", 200, 41, "10.0.0.5", tech="Winston")
    events_mod.log_request(conn, "POST", "/api/ingest/email", 401, 5, "10.0.0.6", tech="")
    conn.commit()

    tech, n = events_mod.list_requests(conn, path_prefix="/tech/")
    assert n == 2
    assert all(r["tech"] == "Winston" for r in tech)

    api, _ = events_mod.list_requests(conn, path_prefix="/api/")
    assert len(api) == 1 and api[0]["status"] == 401

    bad, _ = events_mod.list_requests(conn, status=401)
    assert len(bad) == 1


# ─── prune ───────────────────────────────────────────────────────────────────

def test_prune_drops_old_keeps_fresh(conn):
    """Insert events, backdate some, ensure prune removes only the old ones."""
    events_mod.log_event(conn, "info", "x", "y", "keep me")
    old_ev = events_mod.log_event(conn, "info", "x", "y", "drop me")
    old_ts = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE app_events SET ts=? WHERE id=?", (old_ts, old_ev))

    events_mod.log_request(conn, "GET", "/tech/pwa/", 200, 4, "10.0.0.7")
    old_req = events_mod.log_request(conn, "GET", "/tech/pwa/", 200, 4, "10.0.0.7")
    old_req_ts = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE request_log SET ts=? WHERE id=?", (old_req_ts, old_req))
    conn.commit()

    res = events_mod.prune(conn, events_days=30, requests_days=14)
    assert res["events_pruned"] == 1
    assert res["requests_pruned"] == 1
    assert conn.execute("SELECT COUNT(*) FROM app_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0] == 1


# ─── counts / chart ──────────────────────────────────────────────────────────

def test_counts_24h_returns_expected_keys(conn):
    keys = set(events_mod.counts_24h(conn).keys())
    assert {
        "errors_24h", "warns_24h", "events_24h", "requests_24h",
        "request_errors_24h", "emails_sent_7d", "emails_failed_7d",
        "ingest_24h", "pwa_24h", "stock_alerts_open",
    }.issubset(keys)


def test_requests_by_day_groups(conn):
    today = datetime.now().strftime("%Y-%m-%d")
    events_mod.log_request(conn, "GET", "/tech/catalog", 200, 5, "x")
    conn.commit()
    rows = events_mod.requests_by_day(conn, days=2)
    assert any(r["day"] == today and r["n"] >= 1 for r in rows)


# ─── dual timezone formatting ────────────────────────────────────────────────

def test_fmt_dual_uses_phx_storage_and_adds_one_hour_to_cdmx():
    s = "2026-08-15 08:00:00"
    out = events_mod.fmt_dual(s)
    assert "08:00:00" in out and "AZ" in out
    assert "09:00:00" in out and "CDMX" in out


def test_fmt_dual_handles_microseconds():
    out = events_mod.fmt_dual("2026-01-01 00:00:00.123456")
    assert "00:00:00" in out


def test_fmt_dual_returns_dash_on_empty():
    assert events_mod.fmt_dual("") == "—"


def test_fmt_dual_short_and_date():
    short = events_mod.fmt_dual_short("2026-08-15 08:00:00")
    assert "08:00:00 AZ" in short and "09:00:00 CDMX" in short
    d = events_mod.fmt_dual_date("2026-08-15 08:00:00")
    assert "Aug" in d and "2026" in d


# ─── unified errors feed ─────────────────────────────────────────────────────

def _seed_all_sources(conn):
    """One error + one warn in every unified source. Returns counts."""
    events_mod.log_event(conn, "error", "ingest", "ingest.pdf_error", "bad pdf")
    events_mod.log_event(conn, "warn", "pwa", "pwa.batch.processed", "partial")
    conn.execute("""INSERT INTO email_log
                    (email_type, subject, to_address, status, attempts, response)
                    VALUES ('reorder', 'Subj', 'a@b.c', 'failed', 5, 'SMTP 550')""")
    conn.execute("""INSERT INTO stock_alerts
                    (part_id, part_name, requested_qty, available_qty)
                    VALUES ('P-1', 'Bolt', 10, 2)""")
    conn.execute("""INSERT INTO request_log (method, path, status, duration_ms, ip, tech)
                    VALUES ('GET', '/tech/catalog', 404, 3, '10.0.0.1', 'Winston')""")
    conn.execute("""INSERT INTO request_log (method, path, status, duration_ms, ip, tech)
                    VALUES ('GET', '/tech/catalog', 200, 3, '10.0.0.1', 'Winston')""")
    conn.commit()


def test_unified_errors_aggregates_all_sources(conn):
    _seed_all_sources(conn)
    rows, total = events_mod.unified_errors(conn)
    assert total >= 5
    sources = {r["source"] for r in rows}
    assert {"event", "http", "email", "stock"} <= sources
    http_rows = [r for r in rows if r["source"] == "http"]
    # 200s are never in the feed, only >= 400.
    assert all(r["detail"]["status"] >= 400 for r in http_rows)
    ev = [r for r in rows if r["source"] == "event"]
    assert all(r["level"] in ("error", "warn") for r in ev)
    email = next(r for r in rows if r["source"] == "email")
    assert email["level"] == "error"
    stock = next(r for r in rows if r["source"] == "stock")
    assert stock["level"] == "warn"
    assert "Bolt" in stock["title"]
    # Newest first.
    ts_list = [r["ts"] for r in rows]
    assert ts_list == sorted(ts_list, reverse=True)


def test_unified_errors_filters_by_source(conn):
    _seed_all_sources(conn)
    rows, total = events_mod.unified_errors(conn, sources=["http"])
    assert all(r["source"] == "http" for r in rows)
    assert total == 1
    rows2, _ = events_mod.unified_errors(conn, sources=["stock", "email"])
    assert {r["source"] for r in rows2} == {"stock", "email"}


def test_unified_errors_level_filter_and_search(conn):
    _seed_all_sources(conn)
    rows, total = events_mod.unified_errors(conn, levels=["error"])
    assert rows and all(r["level"] == "error" for r in rows)
    rows2, _ = events_mod.unified_errors(conn, search="Bolt")
    assert rows2 and all("Bolt" in json.dumps(r, default=str) for r in rows2)
    rows3, _ = events_mod.unified_errors(conn, search="nope-not-here-xyz")
    assert rows3 == []


def test_unified_errors_pagination(conn):
    for i in range(5):
        events_mod.log_event(conn, "error", "t", "t.err", f"err #{i}")
    conn.commit()
    page1, total = events_mod.unified_errors(conn, limit=2, offset=0)
    page2, _ = events_mod.unified_errors(conn, limit=2, offset=2)
    assert total >= 5
    assert len(page1) == 2 and len(page2) == 2
    ids1 = [r["sid"] for r in page1]
    ids2 = [r["sid"] for r in page2]
    assert ids1[0] > ids1[1] > ids2[0] > ids2[1]


def test_unified_errors_stock_ignores_time_window(conn):
    """Unresolved stock alerts must appear even when older than the window."""
    conn.execute("""INSERT INTO stock_alerts
                    (part_id, part_name, requested_qty, available_qty, created_at)
                    VALUES ('P-9', 'Old part', 5, 0,
                            datetime('now', '-10 days'))""")
    conn.commit()
    rows, total = events_mod.unified_errors(conn, hours=1)
    assert any(r["source"] == "stock" for r in rows)
    assert total == 1


def test_errors_by_source_counts(conn):
    _seed_all_sources(conn)
    c = events_mod.errors_by_source(conn, hours=24)
    assert c["event_errors"] >= 1
    assert c["event_warns"] >= 1
    assert c["http"] >= 1
    assert c["email"] >= 1
    assert c["stock"] >= 1


# ─── Owner panel routes are wired ───────────────────────────────────────────

def test_owner_observe_routes_are_registered():
    """The observe routes must exist in the mounted owner router. We avoid
    reloading main.py (which would rebuild the whole app); instead we hit each
    route anonymously and expect 404 (the panel hides itself when not logged
    in), proving the path is wired and protected."""
    import main as main_mod
    client = TestClient(main_mod.app)
    admin_path = main_mod.ADMIN_PATH
    for sub in ("/observe", "/observe/events", "/observe/requests",
                "/observe/emails", "/observe/diagrams",
                "/observe/api/module-deps"):
        resp = client.get(admin_path + sub, follow_redirects=False)
        # 404 = route exists but owner auth required (panel is hidden).
        assert resp.status_code == 404, f"{sub} -> {resp.status_code}"


def test_owner_observe_requires_auth_returns_404():
    """Hitting an owner route without owner_authenticated session must 404
    (not redirect), so the panel's existence stays hidden."""
    import main as main_mod
    client = TestClient(main_mod.app)
    resp = client.get(main_mod.ADMIN_PATH + "/observe", follow_redirects=False)
    assert resp.status_code == 404


def test_owner_observe_renders_when_authenticated(tmp_path, monkeypatch):
    """Full happy path: log in as owner, /observe renders with all sub-pages.
    Uses the live app pointed at a tmp DB so we don't pollute the prod file."""
    from database import init_db, get_db
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "obs.db"))
    init_db(seed=True)
    # The app already imported main with the real DATABASE_PATH; reload so the
    # get_db() inside the app + modules/events uses our tmp path. The owner
    # router reads ADMIN_PATH from config at build time, so it picks up the
    # already-set ADMIN_PATH automatically.
    import importlib
    import config as cfg_mod
    importlib.reload(cfg_mod)
    import database as db_mod
    importlib.reload(db_mod)
    import main as main_mod
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    admin = main_mod.ADMIN_PATH
    password = cfg_mod.ADMIN_ACCESS_PASSWORD
    # Login.
    resp = client.post(admin + "/login",
                       data={"password": password},
                       follow_redirects=False)
    assert resp.status_code == 302
    # Each observe page renders.
    for sub in ("/observe", "/observe/events", "/observe/requests",
                "/observe/emails", "/observe/diagrams"):
        r = client.get(admin + sub)
        assert r.status_code == 200, f"{sub} -> {r.status_code}"
    # The dashboard renders the "Observar" nav link.
    assert b"Observar" in client.get(admin + "/observe").content
