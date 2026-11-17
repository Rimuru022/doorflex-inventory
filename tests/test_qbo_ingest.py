"""QBO invoice ingest tests — payload normalization + end-to-end deduction
via the existing _process_extracted_data pipeline. No live Intuit calls;
fetch_invoice is monkeypatched to return canned payloads that mirror the
real QBO Invoice schema (verified against Intuit's public docs)."""
import json

import pytest

from modules import qbo_ingest, qbo_oauth


def _seed_catalog(conn):
    """Match the shape of Invoice_1350's line items so the normalizer's output
    hits the real fuzzy matcher paths, not just synthetic ones."""
    conn.executescript(
        """
        INSERT INTO warehouses (id, name) VALUES ('wh-main', 'DoorFlex');
        INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-01', 'Nick Truck', 'wh-main');
        INSERT INTO parts (id, name, aliases) VALUES
          ('SPR-207', 'Spring retention', '["spring"]'),
          ('BRG-14', 'Bearings', '["bearing"]'),
          ('RLR-STD', 'Rollers', '["roller"]');
        INSERT INTO stock (truck_id, part_id, quantity, min_threshold) VALUES
          ('T-01', 'SPR-207', 5, 2),
          ('T-01', 'BRG-14', 10, 3),
          ('T-01', 'RLR-STD', 30, 10);
        INSERT INTO techs (name, truck_id, warehouse_id, deduction_source)
          VALUES ('Nick H', 'T-01', 'wh-main', 'email');
        """
    )
    conn.commit()


# ─── normalize_invoice ─────────────────────────────────────────────────────────


def test_normalize_itemized_invoice_maps_line_items():
    """Real QBO Invoice payload shape — Line[].SalesItemLineDetail.{ItemRef, Qty}
    with a SalesRep and DocNumber. Mirrors Invoice_1350 from the repo."""
    payload = {
        "Invoice": {
            "Id": "42",
            "DocNumber": "112338330",
            "SalesRep": {"name": "Nick H"},
            "Line": [
                {"DetailType": "SalesItemLineDetail",
                 "SalesItemLineDetail": {"ItemRef": {"name": "Spring retention"}, "Qty": 1}},
                {"DetailType": "SalesItemLineDetail",
                 "SalesItemLineDetail": {"ItemRef": {"name": "Bearings"}, "Qty": 3}},
                {"DetailType": "SalesItemLineDetail",
                 "SalesItemLineDetail": {"ItemRef": {"name": "Rollers"}, "Qty": 12}},
                {"DetailType": "SubTotalLineDetail"},  # noise line — must be skipped
                {"DetailType": "DescriptionOnly",
                 "SalesItemLineDetail": {"ItemRef": {"name": "Not really an item"}}},
            ],
        }
    }
    data = qbo_ingest.normalize_invoice(payload)
    assert data["format"] == "itemized"
    assert data["technician_name"] == "Nick H"
    assert data["wo_number"] == "112338330"
    assert data["parts_list"] == [
        {"raw_name": "Spring retention", "quantity": 1},
        {"raw_name": "Bearings", "quantity": 3},
        {"raw_name": "Rollers", "quantity": 12},
    ]


def test_normalize_falls_back_to_custom_field_for_tech():
    """Not every QBO account carries the tech on SalesRep — Dispatch installs
    that don't use SalesRep typically use a Custom Field named 'Technician'."""
    payload = {"Invoice": {
        "DocNumber": "999",
        "CustomField": [{"Name": "Technician", "StringValue": "Nick H"}],
        "Line": [{"DetailType": "SalesItemLineDetail",
                  "SalesItemLineDetail": {"ItemRef": {"name": "Bearings"}, "Qty": 2}}],
    }}
    data = qbo_ingest.normalize_invoice(payload)
    assert data["technician_name"] == "Nick H"


def test_normalize_returns_none_for_labor_only_invoice():
    """Invoices with only non-item lines (all-labor, subtotals, discounts) have
    nothing to deduct — the normalizer returns None so the caller treats it as
    a legitimate no-op rather than an error."""
    payload = {"Invoice": {
        "DocNumber": "42",
        "SalesRep": {"name": "Nick H"},
        "Line": [
            {"DetailType": "DescriptionOnly"},
            {"DetailType": "SubTotalLineDetail"},
        ],
    }}
    assert qbo_ingest.normalize_invoice(payload) is None


def test_normalize_skips_zero_quantity_and_empty_name():
    """Guard against garbage lines that would corrupt stock — a 0-qty or
    unnamed line is silently dropped, not treated as a deduction."""
    payload = {"Invoice": {
        "DocNumber": "42",
        "SalesRep": {"name": "Nick H"},
        "Line": [
            {"DetailType": "SalesItemLineDetail",
             "SalesItemLineDetail": {"ItemRef": {"name": ""}, "Qty": 5}},
            {"DetailType": "SalesItemLineDetail",
             "SalesItemLineDetail": {"ItemRef": {"name": "Bearings"}, "Qty": 0}},
            {"DetailType": "SalesItemLineDetail",
             "SalesItemLineDetail": {"ItemRef": {"name": "Rollers"}, "Qty": 2}},
        ],
    }}
    data = qbo_ingest.normalize_invoice(payload)
    assert data["parts_list"] == [{"raw_name": "Rollers", "quantity": 2}]


# ─── End-to-end deduction (mocked fetch) ───────────────────────────────────────


def _canned_invoice(invoice_id="42", tech="Nick H", wo="112338330"):
    return {"Invoice": {
        "Id": invoice_id,
        "DocNumber": wo,
        "SalesRep": {"name": tech},
        "Line": [
            {"DetailType": "SalesItemLineDetail",
             "SalesItemLineDetail": {"ItemRef": {"name": "Bearings"}, "Qty": 3}},
            {"DetailType": "SalesItemLineDetail",
             "SalesItemLineDetail": {"ItemRef": {"name": "Rollers"}, "Qty": 12}},
        ],
    }}


def test_process_invoice_event_deducts_stock(conn, monkeypatch):
    _seed_catalog(conn)
    # Stub the token store so get_valid_token doesn't try to refresh.
    conn.execute(
        "INSERT INTO qbo_tokens (realm_id, access_token, refresh_token, expires_at) "
        "VALUES ('R1', 'tok', 'refresh', datetime('now','+1 hour','localtime'))"
    )
    conn.commit()
    monkeypatch.setattr(qbo_ingest, "fetch_invoice",
                        lambda c, r, i: _canned_invoice(invoice_id=i))

    result = qbo_ingest.process_invoice_event(conn, "R1", "42")

    # Bearings 10 → 7 (deducted 3); Rollers 30 → 18 (deducted 12).
    rows = {r["part_id"]: r["quantity"] for r in
            conn.execute("SELECT part_id, quantity FROM stock WHERE truck_id='T-01'").fetchall()}
    assert rows["BRG-14"] == 7
    assert rows["RLR-STD"] == 18
    # log row source is 'email' downstream (format_type='itemized'); the ingest
    # tags invoice_id with a 'qbo:' prefix so the origin is still traceable.
    logs = [dict(r) for r in conn.execute(
        "SELECT invoice_id FROM log WHERE part_id='BRG-14'").fetchall()]
    assert logs[0]["invoice_id"] == "qbo:42"


def test_process_invoice_event_labor_only_is_a_noop(conn, monkeypatch):
    _seed_catalog(conn)
    conn.execute("INSERT INTO qbo_tokens (realm_id, access_token, refresh_token, expires_at) "
                 "VALUES ('R1', 'tok', 'refresh', datetime('now','+1 hour','localtime'))")
    conn.commit()
    monkeypatch.setattr(qbo_ingest, "fetch_invoice", lambda c, r, i: {
        "Invoice": {"DocNumber": "42", "SalesRep": {"name": "Nick H"},
                    "Line": [{"DetailType": "SubTotalLineDetail"}]},
    })
    result = qbo_ingest.process_invoice_event(conn, "R1", "42")
    assert result["status"] == "skipped"
    # Stock untouched.
    row = conn.execute(
        "SELECT quantity FROM stock WHERE part_id='BRG-14' AND truck_id='T-01'").fetchone()
    assert row["quantity"] == 10


# ─── qbo_oauth token store ─────────────────────────────────────────────────────


def test_store_and_get_valid_token_roundtrip(conn):
    qbo_oauth.store_tokens(conn, "R1", {
        "access_token": "atk1", "refresh_token": "rtk1", "expires_in": 3600,
    })
    assert qbo_oauth.get_valid_token(conn, "R1") == "atk1"


def test_get_valid_token_refreshes_when_expired(conn, monkeypatch):
    qbo_oauth.store_tokens(conn, "R1", {
        "access_token": "old", "refresh_token": "rtk", "expires_in": 1,
    })
    # Force expiry by rewriting expires_at into the past.
    conn.execute(
        "UPDATE qbo_tokens SET expires_at=datetime('now','-1 hour','localtime') WHERE realm_id='R1'"
    )
    conn.commit()
    calls = []
    def fake_refresh(refresh_token):
        calls.append(refresh_token)
        return {"access_token": "new", "refresh_token": "rtk-rotated", "expires_in": 3600}
    monkeypatch.setattr(qbo_oauth, "refresh_access_token", fake_refresh)
    assert qbo_oauth.get_valid_token(conn, "R1") == "new"
    assert calls == ["rtk"]
    # Rotated refresh_token was persisted too, not just the access token.
    row = conn.execute("SELECT refresh_token FROM qbo_tokens WHERE realm_id='R1'").fetchone()
    assert row["refresh_token"] == "rtk-rotated"


def test_get_valid_token_raises_when_no_tokens(conn):
    with pytest.raises(qbo_oauth.QboOAuthError):
        qbo_oauth.get_valid_token(conn, "R-nonexistent")


def test_clear_tokens_removes_row(conn):
    qbo_oauth.store_tokens(conn, "R1", {
        "access_token": "atk", "refresh_token": "rtk", "expires_in": 3600,
    })
    qbo_oauth.clear_tokens(conn)
    assert conn.execute("SELECT COUNT(*) c FROM qbo_tokens").fetchone()["c"] == 0
