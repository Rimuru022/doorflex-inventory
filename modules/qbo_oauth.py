"""Intuit QuickBooks Online OAuth 2.0 flow + token storage.

Owner clicks 'Connect QuickBooks' once, approves our app on Intuit's consent
page, and the callback stores the (access, refresh) pair for that realm_id
(QBO company). Every webhook-driven invoice fetch pulls a valid token via
`get_valid_token()`, which auto-refreshes if the access token has expired.

Not tied to any live QBO instance until QBO_ENABLED (see config.py). Every
public function raises `QboOAuthError` on any operational failure so the
route/webhook layer can decide how to surface it.
"""
import base64
import secrets
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import (
    QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_ENVIRONMENT, QBO_REDIRECT_URI,
)

AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"
# Refresh 5 min before the access token would otherwise expire — Intuit hands
# out 60-min access tokens, so this leaves plenty of clock-skew margin.
REFRESH_WINDOW_SECONDS = 300


class QboOAuthError(Exception):
    pass


def build_authorize_url(state):
    """Build the URL to redirect the owner's browser to. state is a random
    opaque string we store in the session and verify on callback (CSRF)."""
    params = {
        "client_id": QBO_CLIENT_ID,
        "response_type": "code",
        "scope": SCOPE,
        "redirect_uri": QBO_REDIRECT_URI,
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def new_state():
    return secrets.token_urlsafe(24)


def _basic_auth_header():
    raw = f"{QBO_CLIENT_ID}:{QBO_CLIENT_SECRET}".encode("ascii")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _post_form(url, form):
    """POST an application/x-www-form-urlencoded body with Basic auth. Raises
    QboOAuthError with the response body if Intuit returns non-2xx."""
    body = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", _basic_auth_header())
    req.add_header("Accept", "application/json")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            import json
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = str(e)
        raise QboOAuthError(f"Intuit returned {e.code}: {detail}") from e
    except Exception as e:
        raise QboOAuthError(f"Intuit token request failed: {e}") from e


def exchange_code_for_tokens(code):
    """OAuth callback → returns {access_token, refresh_token, expires_in}."""
    return _post_form(TOKEN_URL, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": QBO_REDIRECT_URI,
    })


def refresh_access_token(refresh_token):
    """Exchange a refresh_token for a new (access, refresh) pair. Intuit
    rotates the refresh_token roughly every 100 days, so we always overwrite
    both, not just the access token."""
    return _post_form(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })


def store_tokens(conn, realm_id, tokens):
    """Idempotent upsert into qbo_tokens keyed on realm_id. Called on both
    the initial exchange and every refresh — schema is designed so one row
    per company is the invariant."""
    expires_at = (
        datetime.now() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO qbo_tokens (realm_id, access_token, refresh_token, expires_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now','localtime'))
           ON CONFLICT(realm_id) DO UPDATE SET
             access_token=excluded.access_token,
             refresh_token=excluded.refresh_token,
             expires_at=excluded.expires_at,
             updated_at=datetime('now','localtime')""",
        (realm_id, tokens["access_token"], tokens["refresh_token"], expires_at),
    )
    conn.commit()


def clear_tokens(conn, realm_id=None):
    """Owner 'Disconnect' — removes stored tokens. If realm_id omitted,
    clears all rows (there should only ever be one in practice)."""
    if realm_id:
        conn.execute("DELETE FROM qbo_tokens WHERE realm_id=?", (realm_id,))
    else:
        conn.execute("DELETE FROM qbo_tokens")
    conn.commit()


def get_connected_realm(conn):
    """Return (realm_id, connection_info dict) for the connected company, or
    (None, None) if disconnected. Used by the owner-panel status view."""
    row = conn.execute(
        "SELECT * FROM qbo_tokens ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None, None
    d = dict(row)
    return d["realm_id"], {
        "expires_at": d["expires_at"],
        "last_webhook_at": d["last_webhook_at"],
        "updated_at": d["updated_at"],
    }


def get_valid_token(conn, realm_id):
    """Return a fresh access_token for realm_id, refreshing if necessary.
    Raises QboOAuthError if the realm is disconnected or refresh fails."""
    row = conn.execute(
        "SELECT access_token, refresh_token, expires_at FROM qbo_tokens WHERE realm_id=?",
        (realm_id,),
    ).fetchone()
    if not row:
        raise QboOAuthError(f"No QBO tokens stored for realm {realm_id}")

    expires_at = row["expires_at"]
    if expires_at:
        try:
            when = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            when = datetime.now()  # unparseable → force refresh
        if (when - datetime.now()).total_seconds() > REFRESH_WINDOW_SECONDS:
            return row["access_token"]

    # Expired (or about to be) — refresh.
    new_tokens = refresh_access_token(row["refresh_token"])
    store_tokens(conn, realm_id, new_tokens)
    return new_tokens["access_token"]


def record_webhook_received(conn, realm_id):
    """Owner-panel telemetry: 'last webhook received at ...'."""
    conn.execute(
        "UPDATE qbo_tokens SET last_webhook_at=datetime('now','localtime') WHERE realm_id=?",
        (realm_id,),
    )
    conn.commit()


def api_base_url():
    """Intuit's REST API base — sandbox vs production is set via env."""
    if QBO_ENVIRONMENT == "production":
        return "https://quickbooks.api.intuit.com"
    return "https://sandbox-quickbooks.api.intuit.com"
