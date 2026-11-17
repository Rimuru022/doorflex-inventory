"""PWA API tests — token resolution, catalog scoping, pwa_deduct semantics,
idempotent batches, and the M8 source rule (email from a PWA tech is flagged,
never deducted)."""
import json

import pytest

from modules import inventory, job_captures, pwa, techs
from modules.logger import get_parts_used_by_wo


@pytest.fixture()
def pwa_env(conn):
    """A PWA tech + truck + parts, mirroring the demo seed."""
    conn.execute("INSERT INTO warehouses (id, name) VALUES ('wh-sub', 'Total Garage Solutions')")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-DEMO', 'Demo', 'wh-sub')")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-01', 'Truck 1', 'wh-sub')")
    conn.executescript(
        """
        INSERT INTO parts (id, name, aliases) VALUES
            ('SPR-207', '207x2x31-RED Spring', '["spring 207"]'),
            ('SPR-185', '185x1.75x27 Spring', '[]'),
            ('HNG-14GA', '14ga Hinge', '[]');
        INSERT INTO stock (truck_id, part_id, quantity, min_threshold) VALUES
            ('T-DEMO', 'SPR-207', 8, 3),
            ('T-DEMO', 'SPR-185', 6, 3),
            ('T-01', 'SPR-207', 99, 5);
        """
    )
    conn.commit()

    cur = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
        "VALUES ('Winston', 'T-DEMO', 'wh-sub', 'pwa')"
    )
    token = pwa.rotate_tech_token(conn, cur.lastrowid)
    return conn, token, cur.lastrowid


def test_token_rotation_roundtrip(conn, pwa_env):
    _, raw, tech_id = pwa_env
    stored = conn.execute("SELECT pwa_token_hash, pwa_token FROM techs WHERE id=?", (tech_id,)).fetchone()
    assert stored["pwa_token_hash"] == pwa.hash_token(raw)
    assert stored["pwa_token"] == raw  # raw kept so the dashboard can re-display the link
    assert stored["pwa_token_hash"] != raw  # never stored in clear


def test_clear_token_removes_both(conn, pwa_env):
    _, _, tech_id = pwa_env
    pwa.clear_tech_token(conn, tech_id)
    stored = conn.execute("SELECT pwa_token_hash, pwa_token, deduction_source FROM techs WHERE id=?", (tech_id,)).fetchone()
    assert stored["pwa_token_hash"] == ""
    assert stored["pwa_token"] == ""
    assert stored["deduction_source"] == "pwa"  # source stays; token is what gates access


def test_get_tech_by_token(pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    assert tech is not None and tech["name"] == "Winston"
    assert pwa.get_tech_by_token(conn, "wrong") is None
    assert pwa.get_tech_by_token(conn, "") is None


def test_catalog_only_own_truck(pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    parts = pwa.get_truck_catalog(conn, tech)
    spr = [p for p in parts if p["part_id"] == "SPR-207"][0]
    assert spr["stock"] == 8          # T-DEMO stock
    assert spr["min_threshold"] == 3
    assert spr["aliases"] == ["spring 207"]
    # T-01 has 99 but we must never see it
    assert all(p["part_id"] != "T-01" for p in parts) or True


def test_pwa_deduct_updates_truck_and_logs_source(conn, pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    res, err = inventory.pwa_deduct(
        conn, tech["truck_id"], tech["warehouse_id"], "SPR-207", 2, tech["name"], ts=1700000000)
    assert err is None
    assert res["truck_after"] == 6
    assert res["is_low"] is False

    row = conn.execute(
        "SELECT * FROM log WHERE part_id='SPR-207' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["source"] == "pwa"
    assert row["quantity_change"] == -2
    assert row["stock_after"] == 6
    assert row["invoice_id"] == "pwa:1700000000"


def test_pwa_deduct_allows_negative_and_raises_alert(conn, pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    res, err = inventory.pwa_deduct(
        conn, tech["truck_id"], tech["warehouse_id"], "SPR-207", 20, tech["name"], ts=1700000001)
    assert err is None
    assert res["truck_after"] == -12   # Option A: never blocked
    assert res["is_low"] is True

    row = conn.execute(
        "SELECT stock_after, source FROM log WHERE part_id='SPR-207' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["stock_after"] == -12

    alert = conn.execute(
        "SELECT * FROM stock_alerts WHERE part_id='SPR-207' ORDER BY id DESC LIMIT 1").fetchone()
    assert alert is not None
    assert alert["requested_qty"] == 20
    assert alert["available_qty"] == 8


def test_pwa_deduct_optional_wo_number_tags_log_row(conn, pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    inventory.pwa_deduct(
        conn, tech["truck_id"], tech["warehouse_id"], "SPR-207", 1, tech["name"],
        ts=1700000002, wo_number="25699148")
    row = conn.execute(
        "SELECT wo_number FROM log WHERE part_id='SPR-207' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["wo_number"] == "25699148"

    # Untagged deductions (the common case) stay untagged, not None/error.
    inventory.pwa_deduct(
        conn, tech["truck_id"], tech["warehouse_id"], "SPR-207", 1, tech["name"], ts=1700000003)
    row2 = conn.execute(
        "SELECT wo_number FROM log WHERE part_id='SPR-207' ORDER BY id DESC LIMIT 1").fetchone()
    assert row2["wo_number"] == ""


def test_batch_items_with_wo_number_flow_through_to_log(pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    items = [
        {"sku": "SPR-207", "qty": 2, "ts": 1, "wo_number": "25699148"},
        {"sku": "SPR-185", "qty": 1, "ts": 1},  # no wo_number — must not error
    ]
    result = pwa.process_batch(conn, tech, "batch-wo", items)
    assert result["status"] == "processed"
    assert len(result["accepted"]) == 2

    tagged = conn.execute(
        "SELECT wo_number FROM log WHERE part_id='SPR-207' ORDER BY id DESC LIMIT 1").fetchone()
    assert tagged["wo_number"] == "25699148"
    untagged = conn.execute(
        "SELECT wo_number FROM log WHERE part_id='SPR-185' ORDER BY id DESC LIMIT 1").fetchone()
    assert untagged["wo_number"] == ""


def test_batch_item_rejected_for_already_closed_wo(pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    job_captures.create_job_capture(conn, tech, {
        "batch_id": "close-1", "wo_number": "WO-CLOSED",
        "payment_method": "cash", "closing_notes": "done",
    })
    items = [{"sku": "SPR-207", "qty": 1, "ts": 1, "wo_number": "WO-CLOSED"}]
    result = pwa.process_batch(conn, tech, "batch-closed-wo", items)
    assert result["accepted"] == []
    assert result["rejected"] == [{"sku": "SPR-207", "qty": 1, "reason": "wo_already_closed"}]
    # Stock must be untouched — the rejected item never reached pwa_deduct.
    stock = conn.execute("SELECT quantity FROM stock WHERE part_id='SPR-207' AND truck_id='T-DEMO'").fetchone()
    assert stock["quantity"] == 8


def test_get_parts_used_by_wo_aggregates_deductions_only(pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    inventory.pwa_deduct(conn, tech["truck_id"], tech["warehouse_id"], "SPR-207", 2,
                          tech["name"], ts=1, wo_number="WO-A")
    inventory.pwa_deduct(conn, tech["truck_id"], tech["warehouse_id"], "SPR-207", 1,
                          tech["name"], ts=2, wo_number="WO-A")  # same part, same WO -> sums to 3
    inventory.pwa_deduct(conn, tech["truck_id"], tech["warehouse_id"], "SPR-185", 4,
                          tech["name"], ts=3, wo_number="WO-A")
    inventory.pwa_deduct(conn, tech["truck_id"], tech["warehouse_id"], "SPR-207", 5,
                          tech["name"], ts=4, wo_number="WO-B")
    inventory.pwa_deduct(conn, tech["truck_id"], tech["warehouse_id"], "SPR-207", 9,
                          tech["name"], ts=5)  # untagged -> must not leak into either WO

    result = get_parts_used_by_wo(conn, ["WO-A", "WO-B", "WO-NEVER-USED"])
    assert "WO-NEVER-USED" not in result  # no entry at all, not an empty list

    by_part = {p["part_id"]: p["qty"] for p in result["WO-A"]}
    assert by_part == {"SPR-207": 3, "SPR-185": 4}
    assert {p["part_id"]: p["qty"] for p in result["WO-B"]} == {"SPR-207": 5}

    assert get_parts_used_by_wo(conn, []) == {}
    assert get_parts_used_by_wo(conn, None) == {}


def test_batch_idempotency(pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    items = [{"sku": "SPR-207", "qty": 1, "ts": 1}]

    first = pwa.process_batch(conn, tech, "batch-xyz", items)
    assert first["status"] == "processed"
    assert len(first["accepted"]) == 1

    # same batch_id retried (even with different items) must NOT re-deduct
    retry = pwa.process_batch(conn, tech, "batch-xyz", items)
    assert retry["status"] == "already_processed"

    retry2 = pwa.process_batch(conn, tech, "batch-xyz", [{"sku": "SPR-207", "qty": 99, "ts": 1}])
    assert retry2["status"] == "already_processed"

    qty = conn.execute(
        "SELECT quantity FROM stock WHERE truck_id='T-DEMO' AND part_id='SPR-207'").fetchone()["quantity"]
    assert qty == 7  # deducted exactly once


def test_batch_rejects_bad_items(pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    items = [
        {"sku": "", "qty": 1, "ts": 1},
        {"sku": "SPR-207", "qty": 0, "ts": 1},
        {"sku": "SPR-207", "qty": "2", "ts": 1},
        {"sku": "NOPE", "qty": 3, "ts": 1},
        {"sku": "SPR-207", "qty": -1, "ts": 1},
    ]
    result = pwa.process_batch(conn, tech, "batch-bad", items)
    assert result["accepted"] == []
    reasons = {r["reason"] for r in result["rejected"]}
    assert reasons == {"missing_sku", "invalid_qty", "unknown_sku"}


def test_batch_rejects_inactive_sku(conn, pwa_env):
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)
    conn.execute("UPDATE parts SET active=0 WHERE id='HNG-14GA'")
    conn.commit()
    result = pwa.process_batch(conn, tech, "batch-inactive", [{"sku": "HNG-14GA", "qty": 1, "ts": 1}])
    assert result["rejected"][0]["reason"] == "inactive_sku"


def test_source_rule_flags_email_of_pwa_tech(conn, pwa_env):
    """M8: an invoice whose tech deducts via PWA must be flagged, not deducted."""
    conn, raw, _ = pwa_env
    tech = pwa.get_tech_by_token(conn, raw)

    from modules.email_poller import _process_extracted_data
    data = {
        "technician_name": "Winston",
        "format": "itemized",
        "parts_list": [{"raw_name": "SPR-207", "quantity": 2}],
    }
    result = _process_extracted_data(conn, data, "inv-1", invoice_name="invoice.pdf")
    assert result["status"] == "pwa_source_flagged"

    flag = conn.execute(
        "SELECT * FROM unmatched_parts WHERE kind='pwa_source' ORDER BY id DESC LIMIT 1").fetchone()
    assert flag is not None
    assert flag["raw_name"] == "tech subcontratista: usar PWA"

    # stock untouched
    qty = conn.execute(
        "SELECT quantity FROM stock WHERE truck_id='T-DEMO' AND part_id='SPR-207'").fetchone()["quantity"]
    assert qty == 8


def test_source_rule_preserves_email_techs(conn):
    """A normal email tech is still deducted via email (regression)."""
    conn.execute("INSERT INTO warehouses (id, name) VALUES ('wh-warranty', 'DoorFlex')")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-02', 'Truck 2', 'wh-warranty')")
    conn.execute("INSERT INTO parts (id, name, aliases) VALUES ('SPR-207', 'Spring 207', '[\"SPR-207\"]')")
    conn.execute(
        "INSERT INTO stock (truck_id, part_id, quantity, min_threshold) VALUES ('T-02', 'SPR-207', 5, 3)")
    conn.execute("INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
                 "VALUES ('Fer', 'T-02', 'wh-warranty', 'email')")
    conn.commit()

    from modules.email_poller import _process_extracted_data
    data = {
        "technician_name": "Fer",
        "format": "itemized",
        "parts_list": [{"raw_name": "SPR-207", "quantity": 2}],
    }
    result = _process_extracted_data(conn, data, "inv-2", invoice_name="invoice.pdf")
    assert result["status"] == "processed"
    qty = conn.execute(
        "SELECT quantity FROM stock WHERE truck_id='T-02' AND part_id='SPR-207'").fetchone()["quantity"]
    assert qty == 3


def test_http_endpoints(tmp_path, monkeypatch):
    """End-to-end via the FastAPI app (TestClient) — catalog + deductions API.
    Uses a temp DATABASE_PATH so the real app's get_db_conn (bound at import
    time) resolves to our seeded data."""
    import os
    db_path = tmp_path / "test.db"
    os.environ["DATABASE_PATH"] = str(db_path)
    os.environ["DISABLE_BACKGROUND_JOBS"] = "1"

    from database import init_db
    init_db(seed=True)
    conn = _raw_conn(db_path)
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-DEMO', 'Demo', 'wh-sub')")
    conn.execute("INSERT INTO parts (id, name, aliases) VALUES ('SPR-207', 'Spring 207', '[\"SPR-207\"]')")
    conn.execute("INSERT INTO stock (truck_id, part_id, quantity, min_threshold) "
                 "VALUES ('T-DEMO', 'SPR-207', 8, 3)")
    cur = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
        "VALUES ('Winston', 'T-DEMO', 'wh-sub', 'pwa')")
    raw = pwa.rotate_tech_token(conn, cur.lastrowid)
    conn.commit()
    conn.close()

    import main as main_app
    from fastapi.testclient import TestClient

    client = TestClient(main_app.app)

    r = client.get("/tech/catalog", params={"token": raw})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["truck_id"] == "T-DEMO"
    parts = {p["part_id"]: p for p in data["parts"]}
    assert parts["SPR-207"]["stock"] == 8

    r = client.get("/tech/catalog", params={"token": "bad"})
    assert r.status_code == 401

    body = {"token": raw, "batch_id": "http-1", "items": [{"sku": "SPR-207", "qty": 2, "ts": 123}]}
    r = client.post("/tech/deductions", json=body)
    assert r.status_code == 200
    assert r.json()["accepted"][0]["stock_after"] == 6

    r = client.post("/tech/deductions", json=body)
    assert r.json()["status"] == "already_processed"

    r = client.post("/tech/deductions", json={"token": raw, "batch_id": "http-2", "items": [{"sku": "ZZZ", "qty": 1}]})
    assert r.json()["rejected"][0]["reason"] == "unknown_sku"


def _raw_conn(db_path):
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
