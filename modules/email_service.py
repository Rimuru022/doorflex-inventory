"""Email sending via Resend with retry/backoff, throttling and email_log.

Single delivery path for every automated email (reorders, weekly summary,
internal confirmation). Failures retry with backoff (5/30 min), then are
marked failed and reported to the ALERT_RECIPIENTS address.
"""
import logging
import sqlite3
import time

import requests

from modules import events as events_mod

logger = logging.getLogger("email_service")

RESEND_URL = "https://api.resend.com/emails"


def log_email(conn, email_type, subject, to_address, status, part_id="",
              distributor_id="", attempts=0, response=""):
    conn.execute(
        """INSERT INTO email_log (email_type, subject, to_address, part_id,
                                 distributor_id, status, attempts, response)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (email_type, subject, to_address, part_id, distributor_id,
         status, attempts, response),
    )
    conn.commit()
    return conn.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 1").fetchone()


def send_html(api_key, from_addr, to_list, subject, html):
    """Send one HTML email via Resend. Raises on HTTP error."""
    from config import DEMO_MODE
    if DEMO_MODE:
        # Public demo: the whole flow runs and is logged, nothing is sent.
        return {"id": "demo-simulated", "demo": True, "to": list(to_list)}
    resp = requests.post(
        RESEND_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": from_addr, "to": to_list, "subject": subject, "html": html},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def send_with_retry(conn, api_key, from_addr, to_list, subject, html,
                    email_type, part_id="", distributor_id=""):
    """Send with 2 retries (5min / 30min backoff). On final failure, log as
    failed and notify ALERT_RECIPIENTS (if configured) with the error.
    Returns (ok: bool, detail: dict)."""
    from config import ALERT_RECIPIENTS

    attempts = 0
    backoffs = [300, 1800]
    last_err = None
    for attempt in range(3):
        attempts = attempt + 1
        try:
            resp = send_html(api_key, from_addr, to_list, subject, html)
            log_email(conn, email_type, subject, ",".join(to_list), "sent",
                      part_id=part_id, distributor_id=distributor_id,
                      attempts=attempts, response=str(resp)[:500])
            return True, {"attempts": attempts, "response": resp}
        except Exception as e:
            last_err = e
            logger.error(f"email {email_type} attempt {attempts} failed: {e}")
            if attempt < 2:
                time.sleep(backoffs[attempt])
    log_email(conn, email_type, subject, ",".join(to_list), "failed",
              part_id=part_id, distributor_id=distributor_id,
              attempts=attempts, response=str(last_err)[:500])
    events_mod.log_event(
        conn, "error", "email", f"email.{email_type}.failed",
        f"{email_type} email failed after {attempts} attempts: {last_err}",
        detail={"to": to_list, "subject": subject, "error": str(last_err),
                "attempts": attempts, "distributor_id": distributor_id, "part_id": part_id},
        ref=distributor_id or part_id,
    )
    conn.commit()
    if ALERT_RECIPIENTS:
        try:
            send_html(
                api_key, from_addr, ALERT_RECIPIENTS,
                "[DoorFlex Inventario] Email delivery failed",
                f"<p>The following automated email could not be delivered after 3 attempts:</p>"
                f"<p><b>Type:</b> {email_type}<br><b>Subject:</b> {subject}<br>"
                f"<b>To:</b> {to_list}<br><b>Error:</b> {last_err}</p>",
            )
            events_mod.log_event(
                conn, "info", "email", "email.delivery_failure_notification.sent",
                f"Failure notification sent to {ALERT_RECIPIENTS}",
                detail={"original_type": email_type, "error": str(last_err)},
            )
            conn.commit()
        except Exception as notify_err:
            logger.error(f"failed-delivery notification could not be sent: {notify_err}")
            events_mod.log_event(
                conn, "error", "email", "email.delivery_failure_notification.failed",
                f"Failure notification itself failed: {notify_err}",
                detail={"original_type": email_type, "notify_error": str(notify_err)},
            )
            conn.commit()
    return False, {"attempts": attempts, "error": str(last_err)}
