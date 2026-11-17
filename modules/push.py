"""Web Push (VAPID) — end-of-day sync reminder for techs on iOS 16.4+, where
Background Sync isn't supported (see tech_pwa/sw.js's `deductions` tag).

Kept isolated from modules/pwa.py: subscriptions are keyed by browser
endpoint (one tech can have multiple, e.g. a phone reinstall), and a send
failure (expired/revoked subscription) is expected routine cleanup, not an
error worth failing a cron run over.
"""
import json
import logging

from config import VAPID_PRIVATE_KEY, VAPID_CLAIMS_EMAIL

logger = logging.getLogger("doorflex.push")


def push_enabled():
    return bool(VAPID_PRIVATE_KEY)


def upsert_subscription(conn, tech_id, endpoint, p256dh, auth, user_agent=""):
    existing = conn.execute(
        "SELECT id FROM push_subscriptions WHERE endpoint=?", (endpoint,)
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE push_subscriptions SET tech_id=?, p256dh=?, auth=?, user_agent=? WHERE endpoint=?",
            (tech_id, p256dh, auth, user_agent, endpoint),
        )
    else:
        conn.execute(
            """INSERT INTO push_subscriptions (tech_id, endpoint, p256dh, auth, user_agent)
               VALUES (?, ?, ?, ?, ?)""",
            (tech_id, endpoint, p256dh, auth, user_agent),
        )
    conn.commit()


def subscriptions_for_tech(conn, tech_id):
    rows = conn.execute(
        "SELECT * FROM push_subscriptions WHERE tech_id=?", (tech_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def send_reminder(conn, subscription, title, body):
    """Sends one push notification. Returns True on success. On a 404/410
    (expired subscription) the row is deleted; other failures are logged and
    left in place for the next attempt."""
    from pywebpush import webpush, WebPushException

    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{VAPID_CLAIMS_EMAIL}"},
        )
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            conn.execute("DELETE FROM push_subscriptions WHERE id=?", (subscription["id"],))
            conn.commit()
        logger.warning("push send failed (tech_id=%s status=%s): %s", subscription["tech_id"], status, e)
        return False
    except Exception as e:
        logger.warning("push send failed (tech_id=%s): %s", subscription["tech_id"], e)
        return False
