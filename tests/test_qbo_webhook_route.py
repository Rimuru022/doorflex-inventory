"""HTTP-level tests for the QBO invoice webhook route: signature validation,
disabled-config short-circuit, and end-to-end handoff into the ingest module
(which itself is unit-tested in test_qbo_ingest.py).

Mirrors the reload-config-then-TestClient pattern used by test_observability.py
and test_backups.py — the app is already imported at module import time with
whatever env was live then, so we monkeypatch env, reload config→database→main,
then hand TestClient the fresh app."""
import base64
import hashlib
import hmac
import importlib
import json

import pytest


def _sign(body_bytes, token):
    return base64.b64encode(
        hmac.new(token.encode("utf-8"), body_bytes, hashlib.sha256).digest()
    ).decode("ascii")


def _reload_app_with_qbo(tmp_path, monkeypatch, *, enabled):
    """Set QBO env vars (or clear them) then reload the whole app so the
    webhook route sees the current QBO_ENABLED value at import time."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "inventory.db"))
    if enabled:
        monkeypatch.setenv("QBO_CLIENT_ID", "cid")
        monkeypatch.setenv("QBO_CLIENT_SECRET", "csecret")
        monkeypatch.setenv("QBO_WEBHOOK_VERIFIER_TOKEN", "wht")
    else:
        for k in ("QBO_CLIENT_ID", "QBO_CLIENT_SECRET", "QBO_WEBHOOK_VERIFIER_TOKEN"):
            monkeypatch.delenv(k, raising=False)
    import config as cfg
    importlib.reload(cfg)
    import database as db
    importlib.reload(db)
    db.init_db(seed=True)
    import main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient
    return TestClient(main_mod.app), main_mod, cfg


def test_webhook_returns_501_when_qbo_not_configured(tmp_path, monkeypatch):
    client, _, _ = _reload_app_with_qbo(tmp_path, monkeypatch, enabled=False)
    r = client.post("/webhooks/qbo/invoice", content=b"{}",
                    headers={"intuit-signature": "whatever"})
    assert r.status_code == 501
    assert r.json()["error"] == "qbo_not_configured"


def test_webhook_rejects_missing_signature(tmp_path, monkeypatch):
    client, _, _ = _reload_app_with_qbo(tmp_path, monkeypatch, enabled=True)
    r = client.post("/webhooks/qbo/invoice", content=b"{}")
    assert r.status_code == 401
    assert r.json()["error"] == "missing_signature"


def test_webhook_rejects_bad_signature(tmp_path, monkeypatch):
    client, _, _ = _reload_app_with_qbo(tmp_path, monkeypatch, enabled=True)
    body = b'{"eventNotifications":[]}'
    r = client.post("/webhooks/qbo/invoice", content=body,
                    headers={"intuit-signature": "AAAA=="})
    assert r.status_code == 401
    assert r.json()["error"] == "bad_signature"


def test_webhook_accepts_valid_signature_and_routes_invoice(tmp_path, monkeypatch):
    client, main_mod, _ = _reload_app_with_qbo(tmp_path, monkeypatch, enabled=True)
    # Stub the ingest so the test doesn't need real Intuit API access; assert
    # the route unpacked the notification correctly and passed the right ids.
    calls = []
    monkeypatch.setattr(main_mod.qbo_ingest_mod, "process_invoice_event",
                        lambda conn, realm_id, invoice_id:
                            calls.append((realm_id, invoice_id)) or {"status": "processed"})

    body = json.dumps({
        "eventNotifications": [{
            "realmId": "R42",
            "dataChangeEvent": {"entities": [
                {"name": "Invoice", "id": "1350", "operation": "Create"},
                {"name": "Customer", "id": "999", "operation": "Create"},  # skip
                {"name": "Invoice", "id": "1351", "operation": "Delete"},  # skip
            ]},
        }],
    }).encode("utf-8")
    r = client.post("/webhooks/qbo/invoice", content=body,
                    headers={"intuit-signature": _sign(body, "wht")})
    assert r.status_code == 200
    j = r.json()
    assert j == {"status": "ok", "processed": 1, "skipped": 2, "errors": []}
    assert calls == [("R42", "1350")]


def test_webhook_reports_ingest_errors_as_partial_success(tmp_path, monkeypatch):
    """Intuit retries the whole batch on any non-2xx, so per-invoice errors
    must NOT bubble up as HTTP 5xx — they're reported in the response body."""
    client, main_mod, _ = _reload_app_with_qbo(tmp_path, monkeypatch, enabled=True)
    def raise_ingest(conn, realm_id, invoice_id):
        raise main_mod.qbo_ingest_mod.QboIngestError("simulated fetch failure")
    monkeypatch.setattr(main_mod.qbo_ingest_mod, "process_invoice_event", raise_ingest)

    body = json.dumps({
        "eventNotifications": [{
            "realmId": "R42",
            "dataChangeEvent": {"entities": [
                {"name": "Invoice", "id": "1350", "operation": "Update"},
            ]},
        }],
    }).encode("utf-8")
    r = client.post("/webhooks/qbo/invoice", content=body,
                    headers={"intuit-signature": _sign(body, "wht")})
    assert r.status_code == 200
    j = r.json()
    assert j["processed"] == 0
    assert len(j["errors"]) == 1
    assert "simulated fetch failure" in j["errors"][0]["error"]
