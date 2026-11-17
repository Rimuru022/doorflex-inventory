"""Email service tests — retry/backoff logic with Resend mocked out.
No real network calls."""
import pytest

import modules.email_service as email_service


def _fake_conn(conn):
    return conn


def test_log_email_inserts_row(conn):
    row = email_service.log_email(
        conn, "reorder", "Subject", "a@example.com", "sent", part_id="spring-4x36")
    assert row["status"] == "sent"
    assert row["email_type"] == "reorder"


def test_send_html_posts_to_resend(monkeypatch, conn):
    calls = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"id": "abc"}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        return FakeResp()

    monkeypatch.setattr(email_service.requests, "post", fake_post)
    out = email_service.send_html("key", "from@x.com", ["a@example.com"], "Sub", "<p>hi</p>")
    assert calls["url"] == "https://api.resend.com/emails"
    assert calls["json"]["to"] == ["a@example.com"]
    assert out == {"id": "abc"}


def test_send_with_retry_succeeds_first_try(monkeypatch, conn):
    monkeypatch.setattr(
        email_service, "send_html",
        lambda *a, **k: {"id": "ok"},
    )
    ok, detail = email_service.send_with_retry(
        conn, "key", "from@x.com", ["a@example.com"], "Sub", "<p>hi</p>",
        email_type="reorder", distributor_id="dist_a",
    )
    assert ok is True
    assert detail["attempts"] == 1
    row = conn.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "sent"


def test_send_with_retry_marks_failed_and_notifies(monkeypatch, conn):
    import config
    monkeypatch.setattr(config, "ALERT_RECIPIENTS", ["ops@example.com"])
    # Force all attempts to fail (skip the 5/30 min sleeps)
    monkeypatch.setattr(email_service, "time", type("T", (), {"sleep": staticmethod(lambda s: None)})())
    monkeypatch.setattr(
        email_service, "send_html",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("smtp down")),
    )
    notified = []

    def fake_notify(api_key, from_addr, to, subject, html):
        notified.append((to, subject))

    monkeypatch.setattr(email_service, "send_html", fake_notify)
    # send_html raises on every real attempt, but the notify path uses send_html too —
    # so fake the notify by intercepting based on subject
    real_fail = {"n": 0}

    def flaky(api_key, from_addr, to_list, subject, html):
        if subject.startswith("[DoorFlex Inventario] Email delivery failed"):
            notified.append((to_list, subject))
            return {"id": "notify"}
        real_fail["n"] += 1
        raise RuntimeError("smtp down")

    monkeypatch.setattr(email_service, "send_html", flaky)
    monkeypatch.setattr(email_service.time, "sleep", lambda s: None)

    ok, detail = email_service.send_with_retry(
        conn, "key", "from@x.com", ["a@example.com"], "Sub", "<p>hi</p>",
        email_type="reorder",
    )
    assert ok is False
    assert real_fail["n"] == 3
    assert detail["attempts"] == 3
    row = conn.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed"
    assert notified, "failure notification must be sent"
