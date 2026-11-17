import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
DATABASE_PATH = os.environ.get("DATABASE_PATH", str(Path(__file__).parent / "data" / "inventory.db"))
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
NICK_EMAIL = os.environ.get("NICK_EMAIL", "nick@doorflexaz.com")
# Maria (admin) — recipient for door-order notifications alongside Nick. Not
# configured yet as her own dashboard account (PRD: single shared login is
# fine), just an email address for this notification.
MARIA_EMAIL = os.environ.get("MARIA_EMAIL", "")
# Verified sender domain in Resend (the key must be authorized for it)
REPORT_FROM = os.environ.get("REPORT_FROM", "DoorFlex Inventory <reports@doorflexaz.com>")
SECRET_KEY = os.environ.get("SECRET_KEY", ADMIN_PASSWORD)

# Fixed public URL for links embedded in outbound emails (door-order notices,
# etc.). Deliberately NOT derived from the incoming request's Host header —
# that varies by client (reverse proxy, test client, curl with a bogus Host)
# and produced broken links like "http://testserver/..." in real emails.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://dooflexinventory.otomatelabs.com").rstrip("/")

# Email ingestion via Cloudflare Email Worker webhook
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Web Push (VAPID) for the tech PWA's end-of-day sync reminder on iOS 16.4+.
# Generate a keypair once (e.g. `python -c "from pywebpush import webpush; ..."`
# or `vapid --gen`) and set these — leaving them unset just disables push
# (subscribe calls no-op, the 6pm reminder loop is never armed).
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", NICK_EMAIL)

# Vision parsing (Winston report photos, Dispatch job-list screenshots) — both
# go through an OpenAI-compatible chat completions endpoint. Model name is
# env-overridable since it changes faster than this codebase does.
# Leave OPENAI_BASE_URL unset to hit OpenAI directly. To route through
# OpenRouter instead, set OPENAI_BASE_URL=https://openrouter.ai/api/v1,
# OPENAI_API_KEY to the OpenRouter key, and OPENAI_VISION_MODEL to an
# OpenRouter model slug (e.g. "openai/gpt-4o") — the OpenAI SDK works
# unmodified against any OpenAI-compatible base URL.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "") or None
OPENAI_VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "openai/gpt-5-nano")

# Monthly report recipients (comma-separated). The owner Reports page lets you override per send.
REPORT_RECIPIENTS = [e.strip() for e in os.environ.get("REPORT_RECIPIENTS", "").split(",") if e.strip()]

# Weekly reorder/alert recipients (comma-separated). Friday run, C.2 internal
# summary + C.4 Amazon picks + weekly alert all go here. Defaults to the PRD
# nobody (set it explicitly); supports multiple.
ALERT_RECIPIENTS = [e.strip() for e in os.environ.get(
    "ALERT_RECIPIENTS", "").split(",") if e.strip()]

# Seconds between automated emails in the Friday run (deliverability throttle).
EMAIL_SEND_DELAY = int(os.environ.get("EMAIL_SEND_DELAY", "60"))

# ─── Owner panel (secret path + required password) ─────────────────────────────
# Everything owner-only lives under ADMIN_PATH and is password-protected by
# ADMIN_ACCESS_PASSWORD. If either is missing/invalid the whole panel is disabled
# (all owner routes 404) and a warning is logged at startup.
ADMIN_ACCESS_PASSWORD = os.environ.get("ADMIN_ACCESS_PASSWORD", "")


def _normalize_admin_path(raw):
    p = (raw or "").strip()
    if not p:
        return ""
    if not p.startswith("/"):
        p = "/" + p
    while len(p) > 1 and p.endswith("/"):
        p = p[:-1]
    if p == "/" or " " in p or ".." in p:
        return ""
    return p


ADMIN_PATH = _normalize_admin_path(os.environ.get("ADMIN_PATH", ""))
ADMIN_ENABLED = bool(ADMIN_ACCESS_PASSWORD) and bool(ADMIN_PATH)

# ─── QuickBooks Online integration ─────────────────────────────────────────────
# Every DoorFlex invoice a tech closes in Dispatch is already synced to
# QuickBooks Online (Nick's Dispatch account has the QBO add-on). We consume
# QBO invoice webhooks so stock deduction is fully automated end-to-end with
# zero tech-app behavior: no PWA for DoorFlex, no email hop, no OCR — the
# QBO Invoice object already carries the exact fields we need (line items
# with qty, tech name, WO#).
#
# All four are required for the QBO ingest to actually turn on. When any is
# unset the webhook route returns 501 and the owner-panel Connect button
# surfaces the config error — it never silently no-ops.
QBO_CLIENT_ID = os.environ.get("QBO_CLIENT_ID", "")
QBO_CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "")
QBO_WEBHOOK_VERIFIER_TOKEN = os.environ.get("QBO_WEBHOOK_VERIFIER_TOKEN", "")
# 'sandbox' during controlled testing (against an Intuit-provided fake QBO
# company); flip to 'production' when connecting Nick's real account.
QBO_ENVIRONMENT = os.environ.get("QBO_ENVIRONMENT", "sandbox").strip().lower()
# Where Intuit sends the browser after the OAuth consent screen. Must match
# the URI registered in the Intuit developer app EXACTLY (including scheme).
QBO_REDIRECT_URI = os.environ.get("QBO_REDIRECT_URI", f"{PUBLIC_URL}{ADMIN_PATH}/qbo/callback")
QBO_ENABLED = bool(QBO_CLIENT_ID and QBO_CLIENT_SECRET and QBO_WEBHOOK_VERIFIER_TOKEN)

# ─── Public demo mode ─────────────────────────────────────────────────────────
# DEMO_MODE=1 turns this deploy into a self-contained public demo:
# - the DB is regenerated with synthetic data (modules/demo_seed.py) on every
#   start and every night at DEMO_RESET_HOUR, replacing whatever is there;
# - nothing leaves the server: emails are logged as simulated sends, push and
#   QuickBooks are off, the owner panel is not mounted;
# - vision parsing (Winston report photos, Dispatch screenshots) stays live if
#   OPENAI_API_KEY is set, capped at DEMO_VISION_DAILY_LIMIT calls per day.
# Never point a DEMO_MODE deploy at a real customer's DATABASE_PATH.
DEMO_MODE = os.environ.get("DEMO_MODE", "").strip().lower() in ("1", "true", "yes")
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo")
DEMO_PWA_TOKEN = os.environ.get("DEMO_PWA_TOKEN", "demo-tech-andre")
DEMO_RESET_HOUR = int(os.environ.get("DEMO_RESET_HOUR", "3"))
DEMO_VISION_DAILY_LIMIT = int(os.environ.get("DEMO_VISION_DAILY_LIMIT", "40"))

if DEMO_MODE:
    ADMIN_PASSWORD = DEMO_PASSWORD
    # Non-empty so every email flow runs end to end; email_service and
    # email_reporter short-circuit on DEMO_MODE and never call Resend.
    RESEND_API_KEY = "demo-simulated"
    NICK_EMAIL = "nick@doorflex.example"
    MARIA_EMAIL = "maria@doorflex.example"
    REPORT_RECIPIENTS = ["office@doorflex.example"]
    ALERT_RECIPIENTS = ["office@doorflex.example"]
    EMAIL_SEND_DELAY = 0
    VAPID_PUBLIC_KEY = VAPID_PRIVATE_KEY = ""
    QBO_ENABLED = False
    ADMIN_ENABLED = False
    WEBHOOK_SECRET = ""
