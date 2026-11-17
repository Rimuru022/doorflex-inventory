import csv
import email
import io
import json
import os
import shutil
import sqlite3
import threading
import time as time_mod
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import jinja2
from typing import List
from fastapi import FastAPI, Request, HTTPException, Form, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from config import ADMIN_PASSWORD, SECRET_KEY, DATABASE_PATH, RESEND_API_KEY, NICK_EMAIL, MARIA_EMAIL, WEBHOOK_SECRET, REPORT_RECIPIENTS, ADMIN_PATH, ADMIN_ENABLED, ALERT_RECIPIENTS, EMAIL_SEND_DELAY, PUBLIC_URL, VAPID_PUBLIC_KEY
from config import DEMO_MODE, DEMO_PASSWORD, DEMO_PWA_TOKEN
from database import get_db, ensure_db, backup_db, init_db
from modules import demo_runtime
from modules import techs, trucks, parts, packages, inventory, warehouses
from modules.logger import get_logs, get_parts_used_by_wo
from modules.email_poller import process_single_email, reprocess_with_confirmed_tech
from modules import analytics
from modules import distributors, reorder
from modules import excel_import
from modules import pwa as pwa_mod
from modules import door_orders as door_orders_mod
from modules import job_captures as job_captures_mod
from modules import push as push_mod
from modules import winston_reports as winston_reports_mod
from modules import qbo_ingest as qbo_ingest_mod
from modules import qbo_oauth as qbo_oauth_mod
from modules.email_builders import friday_alert_html, reorder_html, internal_summary_html, door_order_html, reconciliation_html, get_report_from
from modules.email_service import send_with_retry, send_html, log_email
from modules import events as events_mod
from modules.owner import build_owner_router
import logging
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("main")


# ─── Template helpers ─────────────────────────────────────────────────────────


def get_selected_warehouse(request: Request, conn):
    """Return selected warehouse id ('all' or a warehouse id), persisting in session."""
    wh_id = request.query_params.get("wh")
    if wh_id:
        if wh_id == "all":
            request.session.pop("selected_warehouse", None)
            return "all"
        exists = conn.execute("SELECT 1 FROM warehouses WHERE id=?", (wh_id,)).fetchone()
        if exists:
            request.session["selected_warehouse"] = wh_id
            return wh_id
    wh_id = request.session.get("selected_warehouse")
    if wh_id:
        exists = conn.execute("SELECT 1 FROM warehouses WHERE id=?", (wh_id,)).fetchone()
        if exists:
            return wh_id
        request.session.pop("selected_warehouse", None)
    return "all"


def flash(request: Request, kind: str, msg: str):
    """Store a one-shot flash message (success/error/warning) in the session.
    Rendered by dashboard_base.html — keeps messages out of the URL."""
    request.session["_flash"] = {"kind": kind, "msg": msg}


def render_template(request: Request, name: str, context: dict, conn=None):
    base_context = {"request": request, "demo_mode": DEMO_MODE,
                    "demo_pwa_url": f"/tech/pwa/?t={DEMO_PWA_TOKEN}" if DEMO_MODE else ""}
    _flash = request.session.pop("_flash", None)
    if _flash:
        base_context["flash"] = _flash
    if conn is not None:
        wh_id = get_selected_warehouse(request, conn)
        warehouses = conn.execute("SELECT * FROM warehouses WHERE active=1 ORDER BY name").fetchall()
        selected_name = "Combined"
        if wh_id != "all":
            row = conn.execute("SELECT name FROM warehouses WHERE id=?", (wh_id,)).fetchone()
            selected_name = row["name"] if row else "Combined"
    base_context.update({
        "warehouses": warehouses,
        "selected_warehouse": wh_id,
        "selected_warehouse_name": selected_name,
    })
    base_context.update(context)
    return templates.TemplateResponse(request, name, base_context)

def _seconds_until(target_local: datetime) -> float:
    now = datetime.now()
    return max(60.0, (target_local - now).total_seconds())


def _daily_backup_loop():
    """Run backup_db() every day at 00:00 local time (TZ=America/Phoenix)."""
    while True:
        now = datetime.now()
        target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time_mod.sleep(_seconds_until(target))
        try:
            path = backup_db()
            logger.info(f"daily backup: {Path(path).name if path else 'skipped (no DB)'}")
            events_mod.log_event(
                None, "info", "cron", "cron.daily_backup",
                f"Daily backup created: {Path(path).name if path else 'skipped (no DB)'}",
                detail={"path": path},
            )
        except Exception as e:
            logger.error(f"daily backup failed: {e}")
            events_mod.log_event(
                None, "error", "cron", "cron.daily_backup.failed",
                f"Daily backup failed: {e}",
                detail={"error": str(e)},
            )
        # Prune observability tables (events 30d, requests 14d). Own conn.
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            conn.execute("PRAGMA foreign_keys=ON")
            res = events_mod.prune(conn)
            conn.commit()
            conn.close()
            logger.info(
                f"daily prune: events={res['events_pruned']} requests={res['requests_pruned']}")
            events_mod.log_event(
                None, "info", "cron", "cron.daily_prune",
                f"Pruned observability: {res['events_pruned']} events, {res['requests_pruned']} requests",
                detail=res,
            )
        except Exception as e:
            logger.error(f"daily prune failed: {e}")
            events_mod.log_event(
                None, "error", "cron", "cron.daily_prune.failed",
                f"Daily prune failed: {e}",
            )


def _monthly_report_loop():
    """On the 1st of each month at 00:05 local, build & email the PDF to REPORT_RECIPIENTS."""
    _last_sent_key = "_last_sent_month"
    sent_month = None
    while True:
        now = datetime.now()
        if now.day == 1 and now.hour == 0 and now.minute < 6 and sent_month != (now.year, now.month):
            try:
                conn = sqlite3.connect(DATABASE_PATH)
                conn.row_factory = sqlite3.Row
                data = build_report_data(conn)
                conn.close()
                pdf = render_report_pdf(data)
                send_report_pdf(pdf, REPORT_RECIPIENTS, data["month_label"], RESEND_API_KEY)
                logger.info(f"monthly report sent to {REPORT_RECIPIENTS} for {data['month_label']}")
                events_mod.log_event(
                    None, "info", "email", "email.monthly_report.sent",
                    f"Monthly report sent to {REPORT_RECIPIENTS} for {data['month_label']}",
                    detail={"month": data["month_label"], "recipients": REPORT_RECIPIENTS, "size_bytes": len(pdf)},
                )
                sent_month = (now.year, now.month)
            except Exception as e:
                logger.error(f"monthly report failed: {e}")
                events_mod.log_event(
                    None, "error", "email", "email.monthly_report.failed",
                    f"Monthly report failed: {e}",
                    detail={"error": str(e)},
                )
            time_mod.sleep(360)
            continue
        next_first = (now.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=5, second=0, microsecond=0)
        time_mod.sleep(_seconds_until(next_first))


def _send_weekly_emails():
    """One Friday run: C.1 per distributor, C.2+C.4 internal summary, weekly
    alert (A). Emails are spaced by EMAIL_SEND_DELAY for deliverability."""
    from datetime import date as date_cls

    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        result = reorder.run_weekly_reorder(conn, send_fn=None)
        date_label = date_cls.today().isoformat()
        from_addr = get_report_from()
        sent = 0
        failures = []

        # C.1 — one email per distributor with an email set
        for r in result["reorders"]:
            if not r["email"]:
                continue
            html = reorder_html(r["distributor_name"], r["items"], _warehouse_label(conn, r["items"]))
            subject = f"[DoorFlex Order] Reorder — {r['distributor_name']}"
            ok, detail = send_with_retry(
                conn, RESEND_API_KEY, from_addr, [r["email"]], subject, html,
                email_type="reorder", distributor_id=r["distributor_id"],
            )
            if ok:
                for rid in r["reorder_ids"]:
                    conn.execute(
                        "UPDATE reorder_requests SET status='sent', email_log_id=(SELECT MAX(id) FROM email_log) WHERE id=?",
                        (rid,),
                    )
                conn.commit()
                events_mod.log_event(
                    conn, "info", "email", "email.reorder.sent",
                    f"Reorder email sent to {r['distributor_name']} ({len(r['items'])} items)",
                    detail={"items": len(r["items"]), "total_qty": sum(it["qty"] for it in r["items"])},
                    ref=r["distributor_id"],
                )
            else:
                failures.append({"kind": "reorder", "to": r["distributor_name"], "error": detail.get("error")})
                events_mod.log_event(
                    conn, "error", "email", "email.reorder.failed",
                    f"Reorder email failed for {r['distributor_name']}: {detail.get('error')}",
                    detail={"error": detail.get("error"), "attempts": detail.get("attempts")},
                    ref=r["distributor_id"],
                )
            conn.commit()
            sent += 1
            time_mod.sleep(EMAIL_SEND_DELAY)

        # C.2+C.4 — single internal email (orders sent + Amazon picks)
        has_internal = result["reorders"] or result["amazon_items"] or result["unassigned"]
        if has_internal:
            html = internal_summary_html(
                result["reorders"], result["amazon_items"], result["unassigned"], date_label)
            subject = f"[DoorFlex] Weekly orders & Amazon picks — {date_label}"
            ok, detail = send_with_retry(
                conn, RESEND_API_KEY, from_addr, ALERT_RECIPIENTS, subject, html,
                email_type="internal_summary",
            )
            if ok:
                events_mod.log_event(
                    conn, "info", "email", "email.internal_summary.sent",
                    f"Internal weekly summary sent",
                    detail={"totals": {
                        "reorders": len(result["reorders"]),
                        "amazon": len(result["amazon_items"]),
                        "unassigned": len(result["unassigned"]),
                    }},
                )
            else:
                failures.append({"kind": "internal_summary", "error": detail.get("error")})
                events_mod.log_event(
                    conn, "error", "email", "email.internal_summary.failed",
                    f"Internal summary email failed: {detail.get('error')}",
                    detail={"error": detail.get("error")},
                )
            conn.commit()
            sent += 1
            time_mod.sleep(EMAIL_SEND_DELAY)

        # A — weekly low-stock alert (only if there are alerts)
        sections, total_low = reorder.build_weekly_alert_data(conn)
        if total_low:
            html = friday_alert_html(sections, total_low, date_label)
            subject = f"[DoorFlex Inventario] Alertas semanales — {date_label}"
            ok, detail = send_with_retry(
                conn, RESEND_API_KEY, from_addr, ALERT_RECIPIENTS, subject, html,
                email_type="weekly_alert",
            )
            if ok:
                events_mod.log_event(
                    conn, "info", "email", "email.weekly_alert.sent",
                    f"Weekly low-stock alert sent ({total_low} items)",
                    detail={"total_low": total_low},
                )
            else:
                failures.append({"kind": "weekly_alert", "error": detail.get("error")})
                events_mod.log_event(
                    conn, "error", "email", "email.weekly_alert.failed",
                    f"Weekly alert email failed: {detail.get('error')}",
                    detail={"error": detail.get("error")},
                )
            conn.commit()
            sent += 1

        events_mod.log_event(
            conn, "info", "cron", "cron.weekly_reorder",
            f"Weekly reorder run: {sent} email(s), {result['total_low']} low item(s), {len(failures)} failure(s)",
            detail={
                "sent": sent,
                "failures": failures,
                "reorders": len(result["reorders"]),
                "amazon": len(result["amazon_items"]),
                "unassigned": len(result["unassigned"]),
            },
        )
        conn.commit()
        logger.info(f"weekly reorder run done: {sent} email(s), {result['total_low']} low item(s)")
        return result
    finally:
        conn.close()


def _warehouse_label(conn, items):
    labels = []
    for it in items:
        w = it.get("warehouse_name") or it.get("warehouse_id") or ""
        if w and w not in labels:
            labels.append(w)
    return ", ".join(labels) if labels else ""


def _weekly_reorder_loop():
    """Every Friday at 08:00 AZ: run the reorder + alerts (one cron, all emails).
    Skips the week if the server wasn't up at 08:00."""
    sent_week = None
    while True:
        now = datetime.now()
        if now.weekday() == 4 and now.hour == 8 and now.minute == 0 and sent_week != reorder.week_key(now):
            try:
                _send_weekly_emails()
            except Exception as e:
                logger.error(f"weekly reorder run failed: {e}")
            sent_week = reorder.week_key(now)
            time_mod.sleep(60)
            continue
        target = now + timedelta(days=1)
        target = target.replace(hour=8, minute=0, second=0, microsecond=0)
        time_mod.sleep(_seconds_until(target))


def _reconciliation_loop():
    """Every day at 17:30 AZ: email the captured/not-collected/uncaptured/
    removed-by-dispatch buckets to Nick + Maria (PRD §4.5)."""
    sent_day = None
    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour == 17 and now.minute == 30 and sent_day != today:
            try:
                conn = sqlite3.connect(DATABASE_PATH)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                result = job_captures_mod.compute_reconciliation(conn, today)
                to_list = [e for e in [NICK_EMAIL, MARIA_EMAIL] if e]
                sent = False
                if to_list and RESEND_API_KEY:
                    html = reconciliation_html(result, today)
                    subject = f"[DoorFlex] Daily job reconciliation — {today}"
                    ok, _detail = send_with_retry(
                        conn, RESEND_API_KEY, get_report_from(), to_list, subject, html,
                        email_type="reconciliation",
                    )
                    sent = ok
                job_captures_mod.save_reconciliation_run(conn, result, report_sent=sent)
                events_mod.log_event(
                    conn, "info", "cron", "cron.reconciliation",
                    f"Daily reconciliation: {len(result['matched'])} matched, "
                    f"{len(result['not_collected'])} not collected, "
                    f"{len(result['uncaptured'])} uncaptured, {len(result['removed'])} removed",
                    detail={k: len(v) for k, v in result.items() if k != "date"},
                )
                conn.commit()
                conn.close()
                logger.info(f"daily reconciliation sent for {today}")
            except Exception as e:
                logger.error(f"daily reconciliation failed: {e}")
                events_mod.log_event(
                    None, "error", "cron", "cron.reconciliation.failed",
                    f"Daily reconciliation failed: {e}",
                    detail={"error": str(e)},
                )
            sent_day = today
            time_mod.sleep(60)
            continue
        target = now.replace(hour=17, minute=30, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time_mod.sleep(_seconds_until(target))


def _push_reminder_loop():
    """Every day at 18:00 AZ: push a reminder to techs with an expected job
    today that has no matching capture yet (server-side proxy for PRD §4.3.1
    — the client's job_capture_queue IndexedDB store isn't visible here)."""
    sent_day = None
    while True:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour == 18 and now.minute == 0 and sent_day != today:
            try:
                conn = sqlite3.connect(DATABASE_PATH)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                tech_ids = job_captures_mod.techs_with_uncaptured_jobs_today(conn, today)
                sent, failed = 0, 0
                for tech_id in tech_ids:
                    for sub in push_mod.subscriptions_for_tech(conn, tech_id):
                        ok = push_mod.send_reminder(
                            conn, sub, "DoorFlex",
                            "You have jobs not yet closed. Open DoorFlex to sync before end of day.",
                        )
                        sent += 1 if ok else 0
                        failed += 0 if ok else 1
                events_mod.log_event(
                    conn, "info", "cron", "cron.push_reminder",
                    f"End-of-day push reminder: {sent} sent, {failed} failed, {len(tech_ids)} tech(s) with unclosed jobs",
                    detail={"tech_ids": tech_ids, "sent": sent, "failed": failed},
                )
                conn.commit()
                conn.close()
                logger.info(f"push reminder run: {sent} sent, {failed} failed")
            except Exception as e:
                logger.error(f"push reminder run failed: {e}")
                events_mod.log_event(
                    None, "error", "cron", "cron.push_reminder.failed",
                    f"Push reminder run failed: {e}",
                    detail={"error": str(e)},
                )
            sent_day = today
            time_mod.sleep(60)
            continue
        target = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time_mod.sleep(_seconds_until(target))


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(Path(DATABASE_PATH).parent, exist_ok=True)
    if DEMO_MODE:
        # Fresh synthetic dataset on every start, then nightly. No backups,
        # crons or outbound integrations: see config.DEMO_MODE.
        init_db(seed=False)
        demo_runtime.rebuild()
        threading.Thread(target=demo_runtime.nightly_reset_loop, name="demo-reset", daemon=True).start()
        yield
        return
    ensure_db(seed=True)
    if os.environ.get("DISABLE_BACKGROUND_JOBS", "").lower() not in ("1", "true", "yes"):
        threading.Thread(target=_daily_backup_loop, name="daily-backup", daemon=True).start()
        if REPORT_RECIPIENTS and RESEND_API_KEY and not RESEND_API_KEY.startswith("your-"):
            threading.Thread(target=_monthly_report_loop, name="monthly-report", daemon=True).start()
            logger.info(f"monthly report cron armed for {REPORT_RECIPIENTS}")
        if ALERT_RECIPIENTS and RESEND_API_KEY and not RESEND_API_KEY.startswith("your-"):
            threading.Thread(target=_weekly_reorder_loop, name="weekly-reorder", daemon=True).start()
            logger.info(f"weekly reorder cron armed (Friday 08:00 AZ) for {ALERT_RECIPIENTS}")
        threading.Thread(target=_reconciliation_loop, name="reconciliation", daemon=True).start()
        logger.info("daily job reconciliation cron armed (17:30 AZ)")
        if push_mod.push_enabled():
            threading.Thread(target=_push_reminder_loop, name="push-reminder", daemon=True).start()
            logger.info("end-of-day push reminder cron armed (18:00 AZ)")
    yield


app = FastAPI(title="DoorFlex Inventory", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax")
jinja_env = jinja2.Environment(loader=jinja2.FileSystemLoader("templates"), autoescape=jinja2.select_autoescape())
templates = Jinja2Templates(env=jinja_env)

# PWA for the Winston tech (offline-first catalog + tap-to-deduct). Served from
# the repo's tech_pwa/ directory at /tech/pwa. Installable via manifest+SW.


@app.get("/tech/pwa/manifest.json")
async def tech_pwa_manifest(request: Request):
    """Dynamic manifest — registered ahead of the static mount below so it
    wins for this exact path. iOS "Add to Home Screen" launches go to the
    manifest's start_url, and a standalone-launched web app can run in a
    storage partition separate from the Safari tab that saved the tech's
    token (see app.js's getToken() comment) — so the token must survive in
    start_url itself, not rely on shared localStorage. app.js rewrites the
    <link rel="manifest"> href to include ?t=<token> once it has one, so by
    the time a tech actually adds the app to their home screen, this route
    embeds that same token into start_url."""
    token = request.query_params.get("t", "")
    start_url = f"./?t={token}" if token else "./"
    return JSONResponse({
        "name": "DoorFlex Tech",
        "short_name": "DoorFlex",
        "description": "Catálogo y deducciones del técnico",
        "start_url": start_url,
        "scope": "./",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#f7f4f2",
        "theme_color": "#130e0b",
        "icons": [
            {"src": "icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        ],
    })


_tech_pwa_dir = Path(__file__).parent / "tech_pwa"
if _tech_pwa_dir.is_dir():
    app.mount("/tech/pwa", StaticFiles(directory=str(_tech_pwa_dir), html=True), name="tech_pwa")

# Vendored static assets (Mermaid for the owner observability diagrams).
_static_dir = Path(__file__).parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.middleware("http")
async def _pwa_cache_headers(request, call_next):
    """Never let proxies (Cloudflare) cache PWA assets: a stale sw.js/app.css
    means techs keep the old UI. Cache-busting query strings (?v=N) handle the
    transition; no-store guarantees it going forward."""
    resp = await call_next(request)
    if request.url.path.startswith("/tech/pwa/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


import time as _time_mod

@app.middleware("http")
async def _observe_requests(request, call_next):
    """Record request_log for PWA / ingest activity + any 4xx/5xx. This is how
    production debugging looks at how the PWA connects — catalog fetches every
    2 min, syncs, auth failures, ingest hits — without flooding the table with
    static assets and health checks."""
    path = request.url.path
    # Skip noise: health checks + static PWA assets (the SW catalog fetches the
    # PWA shell; those are already captured by the GSW path, not interesting
    # at the request level) + dashboard/static.
    if (path == "/health"
            or path.startswith("/static/")
            or path.startswith("/dashboard/static")
            or path.startswith("/tech/pwa/sw.js")
            or path.endswith((".css", ".js", ".png", ".jpg", ".svg", ".ico", ".woff2"))
            or path.startswith("/favicon")):
        return await call_next(request)

    should_record = (
        path.startswith("/tech/")
        or path.startswith("/api/")
        or False  # 4xx/5xx handled below
    )
    t0 = _time_mod.perf_counter()
    try:
        resp = await call_next(request)
    except Exception as e:
        # Always log unhandled exceptions (even on non-interesting paths).
        try:
            events_mod.log_event(
                None, "error", "http", "request.exception",
                f"{request.method} {path} crashed",
                detail={"error": str(e)},
                ref="",
            )
        except Exception:
            pass
        raise
    duration_ms = int((_time_mod.perf_counter() - t0) * 1000)
    status = resp.status_code
    if not should_record and status < 400:
        return resp

    # Resolve tech name from token (header or query) for /tech/* requests.
    tech = ""
    if path.startswith("/tech/"):
        token = request.query_params.get("t", "")
        if not token:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:]
        if token:
            db = None
            try:
                db = get_db()
                row = db.execute(
                    "SELECT t.name FROM techs t "
                    "WHERE t.pwa_token_hash=? AND t.active=1 LIMIT 1",
                    (pwa_mod.hash_token(token),),
                ).fetchone()
                tech = row["name"] if row else ""
            except Exception:
                tech = ""
            finally:
                if db is not None:
                    db.close()

    db = None
    try:
        db = get_db()
        events_mod.log_request(
            db,
            request.method, path, status, duration_ms,
            request.client.host if request.client else "",
            tech=tech,
        )
        db.commit()
    except Exception:
        # Never break the request because observability failed.
        pass
    finally:
        if db is not None:
            db.close()
    return resp


def _format_phoenix(value):
    """Format a 'YYYY-MM-DD HH:MM:SS' (or similar) timestamp in America/Phoenix time."""
    if not value:
        return "—"
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    s = str(value)
    try:
        dt = _dt.fromisoformat(s[:19].replace("T", " "))
        dt = dt.replace(tzinfo=ZoneInfo("America/Phoenix"))
    except ValueError:
        return s
    return dt.strftime("%b %-d, %Y · %-I:%M %p")


jinja_env.filters["phx"] = _format_phoenix
jinja_env.filters["dualtz"] = events_mod.fmt_dual
jinja_env.filters["dualtz_short"] = events_mod.fmt_dual_short
jinja_env.filters["dualtz_date"] = events_mod.fmt_dual_date
jinja_env.filters["dualtz_az"] = events_mod.fmt_dual_az_time
jinja_env.filters["dualtz_cmx"] = events_mod.fmt_dual_cmx_time

# ─── Owner panel (secret path + password). Mounted only when configured. ─────
if ADMIN_ENABLED:
    app.include_router(build_owner_router(ADMIN_PATH, render_template, flash))
    logger.info(f"owner panel mounted at {ADMIN_PATH}")
else:
    logger.warning(
        "owner panel DISABLED: set ADMIN_ACCESS_PASSWORD and ADMIN_PATH (e.g. "
        "ADMIN_PATH=/owner-<random-hash>) to enable the secret owner panel."
    )


def get_db_conn():
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def require_auth_html(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")


# ─── Auth ────────────────────────────────────────────────────────────────────


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error, "demo_password": DEMO_PASSWORD if DEMO_MODE else ""})


@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASSWORD:
        request.session["authenticated"] = True
        return RedirectResponse("/dashboard", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid password", "demo_password": DEMO_PASSWORD if DEMO_MODE else ""})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ─── Healthcheck ──────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


# ─── Root redirect ────────────────────────────────────────────────────────────


@app.get("/")
async def root_redirect():
    return RedirectResponse("/demo" if DEMO_MODE else "/login", status_code=302)


# ─── Public demo (DEMO_MODE only) ─────────────────────────────────────────────


def _require_demo():
    if not DEMO_MODE:
        raise HTTPException(status_code=404, detail="Not found")


@app.get("/demo", response_class=HTMLResponse)
async def demo_landing(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    _require_demo()
    stats = {
        "invoices": conn.execute("SELECT COUNT(DISTINCT invoice_id) FROM log WHERE source='email'").fetchone()[0],
        "deductions": conn.execute("SELECT COUNT(*) FROM log WHERE quantity_change < 0").fetchone()[0],
        "captures": conn.execute("SELECT COUNT(*) FROM job_captures").fetchone()[0],
        "disputed": conn.execute(
            """SELECT COALESCE(SUM(jc.amount_charged), 0) FROM disputes d
               JOIN job_captures jc ON jc.id = d.capture_id""").fetchone()[0],
        "parts": conn.execute("SELECT COUNT(*) FROM parts WHERE active=1").fetchone()[0],
    }
    return templates.TemplateResponse(request, "demo_landing.html", {
        "stats": stats, "pwa_url": f"/tech/pwa/?t={DEMO_PWA_TOKEN}", "demo_password": DEMO_PASSWORD,
    })


@app.post("/demo/enter")
async def demo_enter(request: Request):
    _require_demo()
    form = await request.form()
    request.session["authenticated"] = True
    target = str(form.get("next", "/dashboard"))
    if not target.startswith("/dashboard"):
        target = "/dashboard"
    return RedirectResponse(target, status_code=302)


@app.get("/demo/samples/invoice.pdf")
async def demo_sample_invoice():
    _require_demo()
    return Response(demo_runtime.sample_invoice_pdf(), media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="Invoice_1999_demo.pdf"'})


@app.get("/demo/samples/winston-report.jpg")
async def demo_sample_winston_report():
    _require_demo()
    return Response(demo_runtime.sample_winston_report_jpg(), media_type="image/jpeg",
                    headers={"Content-Disposition": 'attachment; filename="winston_weekly_report_demo.jpg"'})


# ─── Dashboard ────────────────────────────────────────────────────────────────


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    wh_filter = get_selected_warehouse(request, conn)
    wh_clause = ""
    wh_params = []
    if wh_filter != "all":
        wh_clause = " AND warehouse_id=?"
        wh_params.append(wh_filter)

    num_techs = conn.execute("SELECT COUNT(*) FROM techs WHERE active=1").fetchone()[0]
    num_trucks = conn.execute("SELECT COUNT(*) FROM trucks WHERE active=1").fetchone()[0]

    num_alerts = conn.execute(f"""
        SELECT COUNT(*) FROM stock s
        JOIN trucks t ON t.id = s.truck_id
        WHERE s.quantity < s.min_threshold{wh_clause}
    """, wh_params).fetchone()[0]
    num_alerts += conn.execute(f"""
        SELECT COUNT(*) FROM warehouse_stock
        WHERE quantity < min_threshold{wh_clause.replace('warehouse_id', 'warehouse_id')}
    """, wh_params).fetchone()[0]

    num_unmatched = conn.execute(
        f"SELECT COUNT(*) FROM unmatched_parts WHERE mapped_to_part_id IS NULL AND kind='part'{wh_clause}",
        wh_params,
    ).fetchone()[0]
    num_unmatched_packages = conn.execute(
        f"SELECT COUNT(*) FROM unmatched_parts WHERE mapped_to_package_id IS NULL AND kind='package'{wh_clause}",
        wh_params,
    ).fetchone()[0]
    num_stock_alerts = conn.execute(
        f"SELECT COUNT(*) FROM stock_alerts WHERE resolved=0{wh_clause}",
        wh_params,
    ).fetchone()[0]
    num_invoices = conn.execute(
        "SELECT COUNT(DISTINCT invoice_id) FROM log WHERE timestamp >= datetime('now', '-30 days') AND source='email'"
        + wh_clause,
        wh_params,
    ).fetchone()[0] or 0

    summary = {
        "techs": num_techs, "trucks": num_trucks, "alerts": num_alerts,
        "unmatched": num_unmatched, "unmatched_packages": num_unmatched_packages,
        "stock_alerts": num_stock_alerts, "invoices_30d": num_invoices,
    }

    all_warehouses = conn.execute("SELECT * FROM warehouses WHERE active=1 ORDER BY name").fetchall()
    warehouse_usage = []
    for wh in all_warehouses:
        row = conn.execute(
            """SELECT COUNT(*) AS line_items FROM log
               WHERE warehouse_id=? AND timestamp >= datetime('now', '-30 days') AND source='email'""",
            (wh["id"],),
        ).fetchone()
        line_items = row[0] if row else 0

        top = conn.execute(
            """SELECT part_id, SUM(ABS(quantity_change)) AS total_used FROM log
               WHERE warehouse_id=? AND timestamp >= datetime('now', '-30 days') AND source='email'
               GROUP BY part_id ORDER BY total_used DESC LIMIT 1""",
            (wh["id"],),
        ).fetchone()

        used_qty = conn.execute(
            """SELECT COALESCE(SUM(ABS(quantity_change)), 0) FROM log
               WHERE warehouse_id=? AND timestamp >= datetime('now', '-30 days') AND source='email'""",
            (wh["id"],),
        ).fetchone()[0]

        warehouse_usage.append({
            "name": wh["name"], "line_items": line_items,
            "top_part": top["part_id"] if top else None,
            "top_qty": top["total_used"] if top else None,
            "used_qty": used_qty,
        })

    top_movers = analytics.top_movers(conn, warehouse_id=wh_filter, days=30, limit=10)

    unmatched = conn.execute(
        f"SELECT * FROM unmatched_parts WHERE mapped_to_part_id IS NULL AND kind='part'{wh_clause} ORDER BY created_at DESC",
        wh_params,
    ).fetchall()

    unmatched_packages = conn.execute(
        f"SELECT * FROM unmatched_parts WHERE mapped_to_package_id IS NULL AND kind='package'{wh_clause} ORDER BY created_at DESC",
        wh_params,
    ).fetchall()

    stock_alerts = conn.execute(
        f"SELECT * FROM stock_alerts WHERE resolved=0{wh_clause} ORDER BY created_at DESC",
        wh_params,
    ).fetchall()

    all_parts = parts.list_parts(conn)
    all_packages = packages.list_packages(conn)

    monthly_labels = []
    monthly_series = {wh["id"]: [] for wh in all_warehouses}
    now_local = datetime.now()
    for i in range(5, -1, -1):
        month_dt = now_local
        for _ in range(i):
            month_dt = (month_dt.replace(day=1) - timedelta(days=1)).replace(day=1)
        month_label = month_dt.strftime("%b")
        monthly_labels.append(month_label)
        month_prefix = month_dt.strftime("%Y-%m")
        for wh in all_warehouses:
            count = conn.execute(
                "SELECT COALESCE(SUM(ABS(quantity_change)), 0) FROM log"
                " WHERE warehouse_id=? AND source='email'"
                " AND strftime('%Y-%m', timestamp)=?",
                (wh["id"], month_prefix),
            ).fetchone()[0]
            monthly_series[wh["id"]].append(count)

    return render_template(request, "dashboard_home.html", {
        "summary": summary, "warehouse_usage": warehouse_usage,
        "warehouse_used_qty": [u["used_qty"] for u in warehouse_usage],
        "top_movers": top_movers, "unmatched": unmatched, "all_parts": all_parts,
        "unmatched_packages": unmatched_packages,
        "pkg_has_parts": {
            p["id"]: bool(json.loads(p["parts_json"] or "[]")) for p in all_packages
        },
        "stock_alerts": stock_alerts,
        "all_packages": all_packages,
        "monthly_labels": monthly_labels,
        "monthly_series": monthly_series,
        "all_warehouses": all_warehouses,
    }, conn=conn)


# ─── Techs ────────────────────────────────────────────────────────────────────


@app.get("/dashboard/techs", response_class=HTMLResponse)
async def techs_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    return render_template(request, "dashboard_techs.html", {
        "techs": techs.list_techs(conn),
        "trucks": trucks.list_trucks(conn),
    }, conn=conn)


@app.post("/dashboard/techs/create")
async def create_tech(
    request: Request, name: str = Form(...), truck_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    truck = trucks.get_truck(conn, truck_id)
    if not truck:
        raise HTTPException(status_code=400, detail="Select a valid truck")
    techs.create_tech(conn, name, truck_id, truck["warehouse_id"])
    return RedirectResponse("/dashboard/techs", status_code=302)


@app.post("/dashboard/techs/update")
async def update_tech(
    request: Request, tech_id: int = Form(...),
    name: str = Form(None), truck_id: str = Form(None),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    tech = techs.get_tech(conn, tech_id)
    if not tech:
        raise HTTPException(status_code=404, detail="Tech not found")
    resolved_truck = truck_id if truck_id else tech["truck_id"]
    truck = trucks.get_truck(conn, resolved_truck)
    warehouse_id = truck["warehouse_id"] if truck else tech["warehouse_id"]
    techs.update_tech(conn, tech_id, resolved_truck, warehouse_id, name=name)
    return RedirectResponse("/dashboard/techs", status_code=302)


@app.post("/dashboard/techs/deactivate")
async def deactivate_tech(
    request: Request, tech_id: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    techs.deactivate_tech(conn, tech_id)
    return RedirectResponse("/dashboard/techs", status_code=302)


# ─── PWA tokens (per-tech, from the staff dashboard) ─────────────────────────


@app.post("/dashboard/techs/pwa/rotate")
async def tech_pwa_rotate(
    request: Request, tech_id: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    tech = techs.get_tech(conn, tech_id)
    if not tech:
        flash(request, "error", "Tech not found.")
        return RedirectResponse("/dashboard/techs", status_code=302)
    raw = pwa_mod.rotate_tech_token(conn, tech_id)
    conn.execute(
        "UPDATE techs SET deduction_source='pwa', active=1 WHERE id=?",
        (tech_id,),
    )
    conn.commit()
    install_url = str(request.base_url).rstrip("/") + f"/tech/pwa/?t={raw}"
    flash(request, "success",
          f"Install link for {tech['name']}: {install_url}")
    return RedirectResponse("/dashboard/techs", status_code=302)


@app.post("/dashboard/techs/pwa/clear")
async def tech_pwa_clear(
    request: Request, tech_id: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    tech = techs.get_tech(conn, tech_id)
    if not tech:
        flash(request, "error", "Tech not found.")
        return RedirectResponse("/dashboard/techs", status_code=302)
    pwa_mod.clear_tech_token(conn, tech_id)
    flash(request, "success", f"PWA token cleared — {tech['name']} can no longer sync.")
    return RedirectResponse("/dashboard/techs", status_code=302)


# ─── Companies ──────────────────────────────────────────────────────────────────


@app.get("/dashboard/warehouses", response_class=HTMLResponse)
async def warehouses_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    return render_template(request, "dashboard_warehouses.html", {
        "warehouses": warehouses.list_warehouses(conn),
    }, conn=conn)


@app.post("/dashboard/warehouses/create")
async def create_warehouse(
    request: Request, name: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result, err = warehouses.create_warehouse(conn, name)
    if err:
        flash(request, "error", err)
        return RedirectResponse("/dashboard/warehouses", status_code=302)
    flash(request, "success", f"Warehouse '{result['name']}' created — select parts below and assign to it in Batch Assign.")
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/warehouses/update")
async def update_warehouse(
    request: Request, warehouse_id: str = Form(...), name: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result, err = warehouses.rename_warehouse(conn, warehouse_id, name)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return RedirectResponse("/dashboard/warehouses", status_code=302)


@app.post("/dashboard/warehouses/delete")
async def delete_warehouse(
    request: Request, warehouse_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result, err = warehouses.deactivate_warehouse(conn, warehouse_id)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return RedirectResponse("/dashboard/warehouses", status_code=302)


# ─── Trucks ────────────────────────────────────────────────────────────────────


@app.get("/dashboard/trucks", response_class=HTMLResponse)
async def trucks_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    return render_template(request, "dashboard_trucks.html", {
        "trucks": trucks.list_trucks(conn),
    }, conn=conn)


@app.post("/dashboard/trucks/create")
async def create_truck(
    request: Request, id: str = Form(...), name: str = Form(...),
    warehouse_id: str = Form(None), conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    trucks.create_truck(conn, id, name, warehouse_id)
    return RedirectResponse("/dashboard/trucks", status_code=302)


@app.post("/dashboard/trucks/update")
async def update_truck(
    request: Request, truck_id: str = Form(...),
    name: str = Form(None), warehouse_id: str = Form(None),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    trucks.update_truck(conn, truck_id, name, warehouse_id)
    return RedirectResponse("/dashboard/trucks", status_code=302)


@app.post("/dashboard/trucks/deactivate")
async def deactivate_truck(
    request: Request, truck_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    trucks.deactivate_truck(conn, truck_id)
    return RedirectResponse("/dashboard/trucks", status_code=302)


# ─── Parts ────────────────────────────────────────────────────────────────────


@app.get("/dashboard/distributors", response_class=HTMLResponse)
async def distributors_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    return render_template(request, "dashboard_distributors.html", {
        "distributors": distributors.list_distributors(conn),
    }, conn=conn)


@app.post("/dashboard/distributors/create")
async def distributor_create(
    request: Request, distributor_id: str = Form(...), name: str = Form(...),
    email: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    pid = distributor_id.strip()
    if not pid:
        flash(request, "error", "Distributor ID is required.")
    elif distributors.get_distributor(conn, pid):
        flash(request, "error", f"Distributor '{pid}' already exists.")
    else:
        try:
            distributors.create_distributor(conn, pid, name, email=email)
            flash(request, "success", f"Distributor '{pid}' ({name}) created.")
        except sqlite3.IntegrityError as e:
            flash(request, "error", f"Could not create: {e}")
    return RedirectResponse("/dashboard/distributors", status_code=302)


@app.post("/dashboard/distributors/update")
async def distributor_update(
    request: Request, distributor_id: str = Form(...), name: str = Form(""),
    email: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    if distributors.get_distributor(conn, distributor_id):
        kwargs = {}
        if name:
            kwargs["name"] = name
        kwargs["email"] = email
        distributors.update_distributor(conn, distributor_id, **kwargs)
        flash(request, "success", f"Distributor '{distributor_id}' updated.")
    else:
        flash(request, "error", f"Distributor '{distributor_id}' not found.")
    return RedirectResponse("/dashboard/distributors", status_code=302)


@app.get("/dashboard/parts", response_class=HTMLResponse)
async def parts_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    return render_template(request, "dashboard_parts.html", {
        "parts": _part_distributor_context(conn),
        "all_warehouses": conn.execute("SELECT * FROM warehouses WHERE active=1").fetchall(),
        "all_trucks": trucks.list_trucks(conn),
        "all_distributors": distributors.list_distributors(conn),
        "import_result": None,
        "distributor_import_result": None,
        "distributor_import_for": "",
    }, conn=conn)


@app.post("/dashboard/parts/create")
async def create_part(
    request: Request, name: str = Form(...), sku: str = Form(""),
    category: str = Form(""), subcategory: str = Form(""),
    brand: str = Form(""), supplier: str = Form(""),
    unit: str = Form("piece"), cost: float = Form(0.0),
    hand: str = Form(""), length_size: str = Form(""),
    notes: str = Form(""), aliases: str = Form(""),
    amazon_sku: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    if not sku:
        sku = parts._slug(name)
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    existing = conn.execute("SELECT * FROM parts WHERE id=?", (sku,)).fetchone()
    if existing:
        parts.update_part(conn, sku, name=name, unit=unit, category=category,
                          subcategory=subcategory, hand=hand, length_size=length_size,
                          brand=brand, supplier=supplier, cost=cost, notes=notes)
        if amazon_sku:
            conn.execute("UPDATE parts SET amazon_sku=? WHERE id=?", (amazon_sku.strip(), sku))
        if not existing["active"]:
            conn.execute("UPDATE parts SET active=1 WHERE id=?", (sku,))
    else:
        parts.create_part(conn, sku, name=name, aliases=alias_list, unit=unit,
                          category=category, subcategory=subcategory, hand=hand,
                          length_size=length_size, brand=brand, supplier=supplier,
                          cost=cost, notes=notes, amazon_sku=amazon_sku.strip())
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/bulk-import")
async def bulk_import_parts(
    request: Request, file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    content = await file.read()
    filename = (file.filename or "").lower()
    if filename.endswith(".xlsx"):
        result = parts.bulk_import_xlsx(conn, content)
    else:
        result = parts.bulk_import_csv(conn, content.decode("utf-8"))
    return render_template(request, "dashboard_parts.html", {
        "parts": _part_distributor_context(conn),
        "all_warehouses": conn.execute("SELECT * FROM warehouses WHERE active=1").fetchall(),
        "all_trucks": trucks.list_trucks(conn),
        "all_distributors": distributors.list_distributors(conn),
        "import_result": result,
        "distributor_import_result": None,
        "distributor_import_for": "",
    }, conn=conn)


@app.post("/dashboard/parts/add-alias")
async def add_part_alias(
    request: Request, part_id: str = Form(...), alias: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    parts.add_alias(conn, part_id, alias)
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/bulk-add-alias")
async def bulk_add_part_aliases(
    request: Request, part_id: str = Form(...), aliases_csv: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result = parts.bulk_add_aliases(conn, part_id, aliases_csv)
    if result is None:
        flash(request, "error", f"Part '{part_id}' not found.")
    elif result:
        flash(request, "success", f"Aliases updated for {part_id}.")
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/remove-alias")
async def remove_part_alias(
    request: Request, part_id: str = Form(...), alias: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    removed = parts.remove_alias(conn, part_id, alias)
    if not removed:
        flash(request, "warning", f"Alias '{alias}' was not on '{part_id}'.")
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/import-distributor-excel")
async def import_distributor_excel(
    request: Request, distributor_id: str = Form(...),
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    filename = (file.filename or "").lower()
    if not filename.endswith(".xlsx"):
        return render_template(request, "dashboard_parts.html", {
            "parts": _part_distributor_context(conn),
            "all_warehouses": conn.execute("SELECT * FROM warehouses WHERE active=1").fetchall(),
            "all_trucks": trucks.list_trucks(conn),
            "all_distributors": distributors.list_distributors(conn),
            "import_result": None,
            "distributor_import_result": {
                "created": 0, "updated": 0, "warnings": [], "errors": [
                    {"row": 0, "reason": "Only .xlsx files are accepted."}
                ],
            },
            "distributor_import_for": distributor_id,
        }, conn=conn)
    content = await file.read()
    try:
        rows = excel_import.parse_usage_sheet(content)
    except ValueError as e:
        return render_template(request, "dashboard_parts.html", {
            "parts": _part_distributor_context(conn),
            "all_warehouses": conn.execute("SELECT * FROM warehouses WHERE active=1").fetchall(),
            "all_trucks": trucks.list_trucks(conn),
            "all_distributors": distributors.list_distributors(conn),
            "import_result": None,
            "distributor_import_result": {
                "created": 0, "updated": 0, "warnings": [], "errors": [{"row": 0, "reason": str(e)}],
            },
            "distributor_import_for": distributor_id,
        }, conn=conn)
    result = excel_import.import_distributor_catalog(conn, distributor_id, rows)
    if result["errors"]:
        flash(request, "warning",
              f"Distributor import: {result['created']} created, {result['updated']} updated, "
              f"{len(result['errors'])} errors.")
    else:
        flash(request, "success",
              f"Distributor import: {result['created']} created, {result['updated']} updated.")
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/rename")
async def rename_part(
    request: Request, part_id: str = Form(...), name: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    part = parts.get_part(conn, part_id)
    if not part:
        raise HTTPException(status_code=404, detail="Part not found")
    parts.update_part(conn, part_id, name=name)
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/deactivate")
async def deactivate_part(
    request: Request, part_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    parts.deactivate_part(conn, part_id)
    return RedirectResponse("/dashboard/parts", status_code=302)


# ─── Distributor toggles (purchase mapping, Section B) ────────────────────────


def _part_distributor_context(conn, parts_list=None):
    """parts list with per-part distributor mappings (chips + inline panel data)."""
    if parts_list is None:
        parts_list = parts.list_parts(conn)
    out = []
    for p in parts_list:
        d = dict(p)
        d["aliases"] = json.loads(d["aliases"])
        d["distributors"] = distributors.list_mappings_for_product(conn, p["id"])
        out.append(d)
    return out


@app.post("/dashboard/parts/distributor/set-active")
async def part_distributor_set_active(
    request: Request, part_id: str = Form(...), distributor_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    if distributors.get_distributor(conn, distributor_id):
        distributors.set_active(conn, part_id, distributor_id)
        flash(request, "success", f"Active distributor set for {part_id}.")
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/distributor/update")
async def part_distributor_update(
    request: Request, part_id: str = Form(...), distributor_id: str = Form(...),
    distributor_sku: str = Form(""), distributor_part_name: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    if distributors.get_distributor(conn, distributor_id):
        distributors.upsert_mapping(
            conn, part_id, distributor_id,
            sku=distributor_sku.strip(), part_name=distributor_part_name.strip(),
        )
        flash(request, "success", f"Mapping updated for {part_id}.")
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/distributor/update-email")
async def distributor_update_email(
    request: Request, distributor_id: str = Form(...), email: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    if distributors.get_distributor(conn, distributor_id):
        distributors.update_distributor(conn, distributor_id, email=email)
        flash(request, "success", f"Email updated for {distributor_id}.")
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/parts/distributor/import")
async def part_distributor_import(
    request: Request, distributor_id: str = Form(...),
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    content = await file.read()
    try:
        result = distributors.import_distributor_csv(
            conn, distributor_id, content.decode("utf-8"))
    except UnicodeDecodeError:
        result = {"created": 0, "updated": 0, "warnings": [], "errors": [
            {"row": 0, "reason": "File must be UTF-8 CSV."}]}
    return render_template(request, "dashboard_parts.html", {
        "parts": _part_distributor_context(conn),
        "all_warehouses": conn.execute("SELECT * FROM warehouses WHERE active=1").fetchall(),
        "all_trucks": trucks.list_trucks(conn),
        "all_distributors": distributors.list_distributors(conn),
        "import_result": None,
        "distributor_import_result": result,
        "distributor_import_for": distributor_id,
    }, conn=conn)


@app.post("/dashboard/parts/batch-distributor")
async def part_batch_distributor(
    request: Request, part_ids: str = Form(""), distributor_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    ids = [x for x in part_ids.split(",") if x.strip()]
    if not ids:
        flash(request, "warning", "Select at least one part.")
        return RedirectResponse("/dashboard/parts", status_code=302)
    if not distributors.get_distributor(conn, distributor_id):
        flash(request, "error", "Invalid distributor.")
        return RedirectResponse("/dashboard/parts", status_code=302)
    count = distributors.batch_set_active(conn, ids, distributor_id)
    flash(request, "success", f"Active distributor set for {count} part(s).")
    return RedirectResponse("/dashboard/parts", status_code=302)


# ─── Orders (pending reorders + Mark as ready) ─────────────────────────────────


@app.get("/dashboard/reorders", response_class=HTMLResponse)
async def reorders_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    pending = conn.execute(
        """SELECT r.*, p.name AS part_name, w.name AS warehouse_name,
                  d.name AS distributor_name
           FROM reorder_requests r
           JOIN parts p ON p.id = r.part_id
           LEFT JOIN warehouses w ON w.id = r.warehouse_id
           LEFT JOIN distributors d ON d.id = r.distributor_id
           WHERE r.status IN ('pending','sent')
           ORDER BY r.created_at"""
    ).fetchall()
    history = conn.execute(
        """SELECT r.*, p.name AS part_name, w.name AS warehouse_name,
                  d.name AS distributor_name
           FROM reorder_requests r
           JOIN parts p ON p.id = r.part_id
           LEFT JOIN warehouses w ON w.id = r.warehouse_id
           LEFT JOIN distributors d ON d.id = r.distributor_id
           WHERE r.status = 'received'
           ORDER BY r.resolved_at DESC LIMIT 50"""
    ).fetchall()
    return render_template(request, "dashboard_orders.html", {
        "pending": pending,
        "history": history,
    }, conn=conn)


@app.post("/dashboard/reorders/{reorder_id}/resolve")
async def reorder_resolve(reorder_id: int, request: Request,
                          conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    row, err = reorder.resolve_reorder(conn, reorder_id, mode="manual")
    if err:
        flash(request, "error", err)
    else:
        flash(request, "success", f"Reorder marked as received (parts received — will restock).")
    return RedirectResponse("/dashboard/reorders", status_code=302)


# ─── Packages ──────────────────────────────────────────────────────────────────


@app.get("/dashboard/packages", response_class=HTMLResponse)
async def packages_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    pkgs = []
    for p in packages.list_packages(conn):
        d = dict(p)
        d["parts_json"] = json.loads(d["parts_json"])
        pkgs.append(d)
    return render_template(request, "dashboard_packages.html", {
        "packages": pkgs,
        "all_parts": parts.list_parts(conn),
    }, conn=conn)


@app.post("/dashboard/packages/create")
async def create_package(
    request: Request, name: str = Form(...), parts_json: str = Form("[]"),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    try:
        plist = json.loads(parts_json) if parts_json else []
    except (json.JSONDecodeError, TypeError):
        plist = []
    packages.create_package(conn, name, plist)
    return RedirectResponse("/dashboard/packages", status_code=302)


@app.post("/dashboard/packages/update")
async def update_package(
    request: Request, package_id: str = Form(...),
    name: str = Form(None), parts_json: str = Form(None),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    plist = json.loads(parts_json) if parts_json else None
    packages.update_package(conn, package_id, name, plist)
    return RedirectResponse("/dashboard/packages", status_code=302)


@app.post("/dashboard/packages/delete/{package_id}")
async def delete_package(
    request: Request, package_id: str,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    packages.deactivate_package(conn, package_id)
    return RedirectResponse("/dashboard/packages", status_code=302)


# ─── Stock ────────────────────────────────────────────────────────────────────


@app.get("/dashboard/stock", response_class=HTMLResponse)
async def stock_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    wh_filter = get_selected_warehouse(request, conn)
    all_trucks = trucks.list_trucks(conn)
    all_wh = conn.execute("SELECT * FROM warehouses WHERE active=1").fetchall()

    if wh_filter == "all":
        trucks_data = [
            {"truck": dict(t), "stock": [dict(s) for s in inventory.get_truck_stock(conn, t["id"])]}
            for t in all_trucks
        ]
        whs_data = [
            {"warehouse": dict(w), "stock": [dict(s) for s in inventory.get_warehouse_stock(conn, w["id"])]}
            for w in all_wh
        ]
    else:
        trucks_data = [
            {"truck": dict(t), "stock": [dict(s) for s in inventory.get_truck_stock(conn, t["id"])]}
            for t in all_trucks if t["warehouse_id"] == wh_filter
        ]
        whs_data = [
            {"warehouse": dict(w), "stock": [dict(s) for s in inventory.get_warehouse_stock(conn, w["id"])]}
            for w in all_wh if w["id"] == wh_filter
        ]

    return render_template(request, "dashboard_stock.html", {
        "trucks": trucks_data,
        "warehouse_sections": whs_data,
        "all_warehouses": all_wh,
        "all_parts": parts.list_parts(conn),
    }, conn=conn)


@app.post("/dashboard/stock/warehouse-remove")
async def warehouse_remove_stock(
    request: Request, warehouse_id: str = Form(...), part_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result, err = inventory.remove_from_warehouse(conn, warehouse_id, part_id)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/warehouse-add")
async def warehouse_add_stock(
    request: Request, part_id: str = Form(...),
    warehouse_ids: List[str] = Form(...), quantity: int = Form(...), min_threshold: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    for wh_id in warehouse_ids:
        inventory.add_warehouse_stock_entry(conn, wh_id, part_id, quantity, min_threshold)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/adjust")
async def adjust_stock(
    request: Request, location_type: str = Form(...), location_id: str = Form(...),
    part_id: str = Form(...), new_quantity: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result, err = inventory.adjust(conn, location_type, location_id, part_id, new_quantity)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/restock")
async def restock_stock(
    request: Request, truck_id: str = Form(...), part_id: str = Form(...),
    quantity: int = Form(...), warehouse_id: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result, err = inventory.restock(conn, truck_id, part_id, quantity, warehouse_id)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/bulk-restock")
async def bulk_restock_truck_route(
    request: Request, truck_id: str = Form(...),
    warehouse_id: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result, err = inventory.bulk_restock_truck(conn, truck_id, warehouse_id or None)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.get("/dashboard/audit/truck/{truck_id}", response_class=HTMLResponse)
async def truck_restock_list(request: Request, truck_id: str, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    from datetime import date
    truck = trucks.get_truck(conn, truck_id)
    if not truck:
        raise HTTPException(status_code=404, detail="Truck not found")
    stock = conn.execute("""
        SELECT s.*, p.name AS part_name, p.unit
        FROM stock s JOIN parts p ON p.id = s.part_id
        WHERE s.truck_id=? AND p.active=1 AND s.quantity < s.min_threshold
        ORDER BY p.name
    """, (truck_id,)).fetchall()
    items = []
    for s in stock:
        needed = max(0, s["min_threshold"] - s["quantity"])
        if needed > 0:
            items.append({**dict(s), "qty_needed": needed})
    return render_template(request, "dashboard_restock_list.html", {
        "truck": dict(truck),
        "items": items,
        "date": date.today().isoformat(),
    }, conn=conn)


@app.post("/dashboard/stock/quick-set")
async def quick_set_stock(
    request: Request, location_type: str = Form(...), location_id: str = Form(...),
    part_id: str = Form(...), new_quantity: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    result, err = inventory.adjust(conn, location_type, location_id, part_id, new_quantity)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/batch-set")
async def batch_set_stock(
    request: Request, location_type: str = Form(...), location_id: str = Form(...),
    part_ids: str = Form(""), new_quantity: int = Form(None), min_threshold: int = Form(None),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    ids = [p.strip() for p in part_ids.split(",") if p.strip()]
    if not ids:
        flash(request, "warning", "Select at least one part.")
        return RedirectResponse("/dashboard/stock", status_code=302)
    if new_quantity is None and min_threshold is None:
        flash(request, "warning", "Provide a quantity and/or a minimum.")
        return RedirectResponse("/dashboard/stock", status_code=302)
    if location_type not in ("warehouse", "truck"):
        flash(request, "error", "Invalid location type.")
        return RedirectResponse("/dashboard/stock", status_code=302)

    table = "warehouse_stock" if location_type == "warehouse" else "stock"
    id_col = "warehouse_id" if location_type == "warehouse" else "truck_id"

    updated = 0
    skipped = []
    for pid in ids:
        exists = conn.execute(
            f"SELECT 1 FROM {table} WHERE {id_col}=? AND part_id=?", (location_id, pid)
        ).fetchone()
        if not exists:
            skipped.append(pid)
            continue
        if new_quantity is not None:
            result, err = inventory.adjust(conn, location_type, location_id, pid, new_quantity)
            if err:
                flash(request, "error", f"{pid}: {err}")
                return RedirectResponse("/dashboard/stock", status_code=302)
        if min_threshold is not None:
            conn.execute(
                f"UPDATE {table} SET min_threshold=?, updated_at=datetime('now', 'localtime') WHERE {id_col}=? AND part_id=?",
                (min_threshold, location_id, pid),
            )
            conn.commit()
        updated += 1
    msg = f"Audit adjust complete — {updated} part(s) set to qty {new_quantity if new_quantity is not None else '—'}, min {min_threshold if min_threshold is not None else '—'}."
    if skipped:
        msg += f"\nSkipped (not assigned here): {', '.join(skipped[:8])}"
    flash(request, "success", msg)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/apply-rows")
async def apply_stock_rows(
    request: Request, location_type: str = Form(...), location_id: str = Form(...),
    method: str = Form("audit"), rows_json: str = Form("[]"),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    """Per-row stock edit from the Stock page. Each checked row submits its own
    qty/min. method='audit' sets directly (does NOT touch the related warehouse);
    method='warehouse' moves stock between truck and its warehouse (blocks if
    the warehouse lacks stock). Warehouse rows always set directly."""
    require_auth_html(request)
    if location_type not in ("warehouse", "truck"):
        flash(request, "error", "Invalid location type.")
        return RedirectResponse("/dashboard/stock", status_code=302)
    try:
        rows = json.loads(rows_json or "[]")
    except (json.JSONDecodeError, TypeError):
        rows = []
    if not rows:
        flash(request, "warning", "Select at least one row to apply.")
        return RedirectResponse("/dashboard/stock", status_code=302)

    changed = 0
    skipped = []
    for r in rows:
        pid = (r.get("part_id") or "").strip()
        qty = r.get("quantity")
        mn = r.get("min_threshold")
        if not pid or (qty is None and mn is None):
            continue
        if location_type == "truck" and method == "warehouse" and qty is not None:
            result, err = inventory.set_truck_stock_entry(conn, location_id, pid, qty, mn if mn is not None else 0)
        else:
            result, err = inventory.set_audit_entry(conn, location_type, location_id, pid, qty if qty is not None else 0, mn if mn is not None else 0, reason="manual_adjustment")
        if err:
            skipped.append(f"{pid}: {err}")
            continue
        changed += 1
    msg = f"Applied — {changed} row(s) updated."
    if skipped:
        msg += f"\nNot applied: {', '.join(skipped[:8])}"
    flash(request, "success" if not skipped else "warning", msg)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/truck-set")
async def truck_set_stock(
    request: Request, truck_id: str = Form(...), part_id: str = Form(...),
    warehouse_id: str = Form(""), new_quantity: int = Form(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    from modules.logger import log_transaction

    current = conn.execute(
        "SELECT quantity FROM stock WHERE truck_id=? AND part_id=?", (truck_id, part_id)
    ).fetchone()
    if not current:
        raise HTTPException(status_code=404, detail="Stock entry not found")

    delta = new_quantity - current["quantity"]
    wh = conn.execute("SELECT * FROM warehouses WHERE id=?", (warehouse_id,)).fetchone() if warehouse_id else None

    if warehouse_id and wh and delta != 0:
        if delta > 0:
            result, err = inventory.restock(conn, truck_id, part_id, delta, warehouse_id, "manual_adjustment")
        else:
            move_qty = -delta
            conn.execute(
                "UPDATE stock SET quantity=quantity-?, updated_at=datetime('now', 'localtime') WHERE truck_id=? AND part_id=?",
                (move_qty, truck_id, part_id),
            )
            conn.execute(
                "UPDATE warehouse_stock SET quantity=quantity+?, updated_at=datetime('now', 'localtime') WHERE warehouse_id=? AND part_id=?",
                (move_qty, warehouse_id, part_id),
            )
            log_transaction(
                conn, tech_name="system", truck_id=truck_id, warehouse_id=warehouse_id,
                part_id=part_id, quantity_change=move_qty,
                stock_before=current["quantity"],
                stock_after=new_quantity, source="manual_adjustment", format="",
            )
            conn.commit()
            result, err = {}, None
    else:
        conn.execute(
            "UPDATE stock SET quantity=?, updated_at=datetime('now', 'localtime') WHERE truck_id=? AND part_id=?",
            (new_quantity, truck_id, part_id),
        )
        log_transaction(
            conn, tech_name="system", truck_id=truck_id, warehouse_id=warehouse_id or "",
            part_id=part_id, quantity_change=delta,
            stock_before=current["quantity"],
            stock_after=new_quantity, source="manual_adjustment", format="",
        )
        conn.commit()
        result, err = {}, None
    if err:
        raise HTTPException(status_code=400, detail=err)
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/add-product")
async def add_product_to_stock(
    request: Request,
    name: str = Form(...), sku: str = Form(""), category: str = Form(""),
    subcategory: str = Form(""), brand: str = Form(""), unit: str = Form("piece"),
    cost: float = Form(0.0), supplier: str = Form(""), notes: str = Form(""),
    hand: str = Form(""), length_size: str = Form(""),
    warehouse_ids: List[str] = Form([]), wh_qty: int = Form(0), wh_min: int = Form(0),
    truck_id: str = Form(""), truck_qty: int = Form(0), truck_min: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    if not sku:
        sku = parts._slug(name)
    existing = conn.execute("SELECT * FROM parts WHERE id=?", (sku,)).fetchone()
    if existing:
        parts.update_part(conn, sku, name=name, unit=unit, category=category,
                          subcategory=subcategory, hand=hand, length_size=length_size,
                          brand=brand, supplier=supplier, cost=cost, notes=notes)
        if not existing["active"]:
            conn.execute("UPDATE parts SET active=1 WHERE id=?", (sku,))
    else:
        parts.create_part(conn, sku, name, unit=unit, category=category,
                          subcategory=subcategory, hand=hand, length_size=length_size,
                          brand=brand, supplier=supplier, cost=cost, notes=notes)
    if wh_qty > 0:
        for wh_id in warehouse_ids:
            inventory.add_warehouse_stock_entry(conn, wh_id, sku, wh_qty, wh_min)
    if truck_id and truck_qty > 0:
        existing_stock = conn.execute(
            "SELECT * FROM stock WHERE truck_id=? AND part_id=?", (truck_id, sku)
        ).fetchone()
        if existing_stock:
            conn.execute(
                "UPDATE stock SET quantity=quantity+?, min_threshold=?, updated_at=datetime('now', 'localtime') WHERE truck_id=? AND part_id=?",
                (truck_qty, truck_min, truck_id, sku),
            )
        else:
            conn.execute(
                "INSERT INTO stock (truck_id, part_id, quantity, min_threshold) VALUES (?, ?, ?, ?)",
                (truck_id, sku, truck_qty, truck_min),
            )
        conn.commit()
    return RedirectResponse("/dashboard/stock", status_code=302)


@app.post("/dashboard/stock/batch-assign")
async def batch_assign_stock(
    request: Request, warehouse_ids: List[str] = Form([]),
    truck_ids: List[str] = Form([]), quantity: int = Form(...),
    min_threshold: int = Form(0), part_ids: str = Form(""),
    method: str = Form("assign"),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    """Batch assign from the Parts Catalog. method='assign' sets warehouse stock
    directly and moves truck stock from its warehouse (blocks if insufficient).
    method='audit' sets every selected location directly WITHOUT touching the
    related warehouse — use it for corrections."""
    require_auth_html(request)
    ids = [p.strip() for p in part_ids.split(",") if p.strip()]
    if not ids:
        flash(request, "warning", "Select at least one part to assign.")
        return RedirectResponse("/dashboard/parts", status_code=302)
    if not warehouse_ids and not truck_ids:
        flash(request, "warning", "Select at least one warehouse or truck to assign to.")
        return RedirectResponse("/dashboard/parts", status_code=302)
    errors = []
    for wh_id in warehouse_ids:
        for pid in ids:
            if method == "audit":
                result, err = inventory.set_audit_entry(conn, "warehouse", wh_id, pid, quantity, min_threshold, reason="manual_adjustment")
            else:
                result, err = inventory.add_warehouse_stock_entry(conn, wh_id, pid, quantity, min_threshold)
            if err:
                errors.append(f"warehouse {wh_id} / {pid}: {err}")
    for t_id in truck_ids:
        for pid in ids:
            if method == "audit":
                result, err = inventory.set_audit_entry(conn, "truck", t_id, pid, quantity, min_threshold, reason="manual_adjustment")
            else:
                result, err = inventory.set_truck_stock_entry(conn, t_id, pid, quantity, min_threshold)
            if err:
                errors.append(f"truck {t_id} / {pid}: {err}")
    if errors:
        msg = "Some assignments were BLOCKED:\n" + "\n".join(errors[:10])
        flash(request, "error", msg)
        return RedirectResponse("/dashboard/parts", status_code=302)
    flash(request, "success", "Batch assign complete.")
    return RedirectResponse("/dashboard/parts", status_code=302)


@app.post("/dashboard/stock/set-min")
async def set_min_threshold(
    request: Request, location_type: str = Form(...), location_id: str = Form(...),
    part_id: str = Form(...), min_threshold: int = Form(0),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    if location_type == "truck":
        conn.execute(
            "UPDATE stock SET min_threshold=?, updated_at=datetime('now', 'localtime') WHERE truck_id=? AND part_id=?",
            (min_threshold, location_id, part_id),
        )
    elif location_type == "warehouse":
        conn.execute(
            "UPDATE warehouse_stock SET min_threshold=?, updated_at=datetime('now', 'localtime') WHERE warehouse_id=? AND part_id=?",
            (min_threshold, location_id, part_id),
        )
    conn.commit()
    return RedirectResponse("/dashboard/stock", status_code=302)


# ─── Unmatched Parts ──────────────────────────────────────────────────────────


@app.get("/dashboard/unmatched", response_class=HTMLResponse)
async def unmatched_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    wh_filter = request.query_params.get("wh", "all")
    if wh_filter != "all":
        exists = conn.execute("SELECT 1 FROM warehouses WHERE id=?", (wh_filter,)).fetchone()
        if not exists:
            wh_filter = "all"
    if wh_filter == "all":
        request.session.pop("selected_warehouse", None)
    wh_clause = ""
    wh_params = []
    if wh_filter != "all":
        wh_clause = " AND warehouse_id=?"
        wh_params.append(wh_filter)
    rows = conn.execute(
        f"SELECT * FROM unmatched_parts WHERE mapped_to_part_id IS NULL AND kind='part'{wh_clause} ORDER BY created_at DESC",
        wh_params,
    ).fetchall()
    pkg_rows = conn.execute(
        f"SELECT * FROM unmatched_parts WHERE mapped_to_package_id IS NULL AND kind='package'{wh_clause} ORDER BY created_at DESC",
        wh_params,
    ).fetchall()
    tech_rows = conn.execute(
        "SELECT * FROM unmatched_parts WHERE kind='tech' AND tech_name='' ORDER BY created_at DESC"
    ).fetchall()
    pwa_source_rows = conn.execute(
        "SELECT * FROM unmatched_parts WHERE kind='pwa_source' ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    all_parts_list = parts.list_parts(conn)
    all_packages = packages.list_packages(conn)
    active_techs = techs.list_techs(conn)
    tech_suggestions = {
        r["id"]: techs.score_techs_against(active_techs, r["raw_name"]) for r in tech_rows
    }
    return render_template(request, "dashboard_unmatched.html", {
        "unmatched": rows, "all_parts": all_parts_list,
        "unmatched_packages": pkg_rows, "all_packages": all_packages,
        "tech_confirmations": tech_rows,
        "pwa_source_flags": pwa_source_rows,
        "tech_suggestions": tech_suggestions,
        "all_techs": active_techs,
        "pkg_has_parts": {
            p["id"]: bool(json.loads(p["parts_json"] or "[]")) for p in all_packages
        },
    }, conn=conn)


@app.post("/dashboard/unmatched/bulk-map")
async def bulk_map_unmatched(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    blocked = []
    for key, value in form.items():
        if key.startswith("map_") and value:
            unmatched_id, part_id = value.split(":")
            unmatched_id = int(unmatched_id)
            row = conn.execute(
                "SELECT * FROM unmatched_parts WHERE id=?", (unmatched_id,)
            ).fetchone()
            if not row:
                continue

            part = parts.get_part(conn, part_id)
            if not part:
                continue

            aliases_list = json.loads(part["aliases"])
            if row["raw_name"] not in aliases_list:
                aliases_list.append(row["raw_name"])
                parts.update_part(conn, part_id, aliases=aliases_list)

            conn.execute(
                "UPDATE unmatched_parts SET mapped_to_part_id=? WHERE id=?",
                (part_id, unmatched_id),
            )

            qty = row["quantity"] or 1
            truck_id = row["truck_id"]
            warehouse_id = row["warehouse_id"]
            tech_name = row["tech_name"] or "system"
            invoice_id = row["invoice_id"] or f"manual-map-{unmatched_id}"

            if truck_id and warehouse_id:
                result, err = inventory.deduct(
                    conn, truck_id, warehouse_id, part_id, qty,
                    tech_name, invoice_id, 1.0, "itemized",
                    raw_text=row["raw_name"], invoice_name=row["invoice_name"] or "",
                )
                if err:
                    blocked.append({"raw_name": row["raw_name"], "qty": qty, "error": err})
                    conn.execute(
                        "UPDATE unmatched_parts SET mapped_to_part_id=NULL WHERE id=?",
                        (unmatched_id,),
                    )
    conn.commit()
    if blocked:
        detail = "\n".join(f"• {b['raw_name']} x{b['qty']}: {b['error']}" for b in blocked[:5])
        msg = f"Some mappings could NOT be deducted (insufficient stock):\n{detail}"
        flash(request, "error", msg)
        return RedirectResponse("/dashboard/unmatched", status_code=302)
    return RedirectResponse("/dashboard/unmatched", status_code=302)


def _deduct_package_into(conn, row, pkg):
    """Deduct all parts of a package for the tech/truck/warehouse of an unmatched row."""
    results = []
    for entry in json.loads(pkg["parts_json"]):
        part_id = entry.get("part_id")
        qty = int(entry.get("qty") or 0)
        if not part_id or qty <= 0:
            continue
        result, err = inventory.deduct(
            conn, row["truck_id"], row["warehouse_id"], part_id, qty,
            row["tech_name"] or "system",
            row["invoice_id"] or f"manual-map-{row['id']}",
            1.0, "package_pricing", raw_text=pkg["name"],
            invoice_name=row["invoice_name"] or "",
        )
        results.append({"part_id": part_id, "qty": qty, "error": err, "result": result})
    return results


@app.post("/dashboard/unmatched/package-map")
async def map_unmatched_package(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    blocked = []
    for key, value in form.items():
        if key.startswith("pkg_map_") and value:
            try:
                unmatched_id, package_id = value.split(":")
            except ValueError:
                continue
            unmatched_id = int(unmatched_id)
            row = conn.execute(
                "SELECT * FROM unmatched_parts WHERE id=?", (unmatched_id,)
            ).fetchone()
            if not row or row["kind"] != "package":
                continue
            pkg = packages.get_package(conn, package_id)
            if not pkg:
                continue
            if not json.loads(pkg["parts_json"] or "[]"):
                blocked.append({"pkg": pkg["name"]})
                continue
            results = _deduct_package_into(conn, row, pkg)
            failed = [r for r in results if r["error"]]
            if failed:
                blocked.append({"pkg": pkg["name"], "details": "; ".join(
                    f"{r['part_id']} x{r['qty']}: {r['error']}" for r in failed[:4])})
                continue
            conn.execute(
                "UPDATE unmatched_parts SET mapped_to_package_id=? WHERE id=?",
                (package_id, unmatched_id),
            )
    conn.commit()
    if blocked:
        detail = "\n".join(
            f"• '{b['pkg']}'" + (f": {b['details']}" if b.get("details") else " has no parts yet — add parts first")
            for b in blocked[:3]
        )
        msg = f"Not deducted:\n{detail}"
        flash(request, "error", msg)
        return RedirectResponse("/dashboard/unmatched", status_code=302)
    return RedirectResponse("/dashboard/unmatched", status_code=302)


@app.post("/dashboard/unmatched/package-create")
async def create_unmatched_package(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    try:
        unmatched_id = int(form.get("unmatched_id", ""))
    except ValueError:
        return RedirectResponse("/dashboard/unmatched", status_code=302)

    row = conn.execute("SELECT * FROM unmatched_parts WHERE id=?", (unmatched_id,)).fetchone()
    if not row or row["kind"] != "package":
        return RedirectResponse("/dashboard/unmatched", status_code=302)

    name = row["raw_name"].strip()
    if not name:
        return RedirectResponse("/dashboard/unmatched", status_code=302)

    from modules.packages import _slug
    pkg = packages.get_package(conn, _slug(name))
    if not pkg:
        pkg = packages.create_package(conn, name, [])
        message = f"Package '{pkg['name']}' created — now add its parts, then assign the invoice in Unmatched"
    elif not pkg["active"]:
        conn.execute("UPDATE packages SET active=1 WHERE id=?", (pkg["id"],))
        conn.commit()
        pkg = packages.get_package(conn, pkg["id"])
        message = f"Package '{pkg['name']}' was inactive — reactivated. Add its parts, then assign the invoice in Unmatched"
    else:
        message = f"Package '{pkg['name']}' already exists — add its parts, then assign the invoice in Unmatched"
    conn.commit()
    flash(request, "success", message)
    return RedirectResponse("/dashboard/packages", status_code=302)


@app.post("/dashboard/unmatched/tech-confirm")
async def confirm_unmatched_tech(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    try:
        unmatched_id = int(form.get("unmatched_id", ""))
    except ValueError:
        return RedirectResponse("/dashboard/unmatched", status_code=302)

    row = conn.execute("SELECT * FROM unmatched_parts WHERE id=?", (unmatched_id,)).fetchone()
    if not row or row["kind"] != "tech":
        return RedirectResponse("/dashboard/unmatched", status_code=302)
    if row["tech_name"]:
        flash(request, "warning",
             f"'{row['raw_name']}' was already confirmed as '{row['tech_name']}'. No changes made.")
        return RedirectResponse("/dashboard/unmatched", status_code=302)

    tech_id = form.get("tech_id", "").strip()
    if tech_id:
        tech = techs.get_tech(conn, tech_id)
    else:
        tech = None

    if not tech or tech["active"] != 1:
        flash(request, "error", f"Select a valid tech to confirm '{row['raw_name']}'.")
        return RedirectResponse("/dashboard/unmatched", status_code=302)

    result = reprocess_with_confirmed_tech(conn, row, tech)
    if result.get("status") == "error":
        flash(request, "error", f"Could not reprocess '{row['raw_name']}': {result.get('reason')}")
        return RedirectResponse("/dashboard/unmatched", status_code=302)

    deducted = [d for d in (result.get("deductions") or []) if d.get("result") and not d.get("error")]
    already = [d for d in (result.get("deductions") or []) if d.get("already_deducted")]
    blocked = [d for d in (result.get("deductions") or []) if d.get("error")]
    unmatched_items = result.get("unmatched") or []

    if blocked:
        blocked_ids = {d.get("part_id") for d in blocked if d.get("part_id")}
        parts = []
        if blocked_ids:
            parts = conn.execute(
                "SELECT id, name FROM parts WHERE id IN ({})".format(
                    ",".join("?" * len(blocked_ids))
                ),
                tuple(blocked_ids),
            ).fetchall()
        part_names = {p["id"]: p["name"] for p in parts}
        block_lines = []
        for d in blocked:
            pname = part_names.get(d.get("part_id")) or d.get("raw_name") or d.get("part_id") or "?"
            block_lines.append(f"• {pname} x{d['qty']}: {d['error']}")
        detail = "\n".join(block_lines[:6])
        msg = (
            f"Confirmed tech '{tech['name']}' for '{row['raw_name']}' — "
            f"{len(deducted)} deducted, {len(blocked)} BLOCKED.\n"
            f"Fix stock for these, then click Confirm again:\n{detail}"
        )
        flash(request, "error", msg)
        return RedirectResponse("/dashboard/unmatched", status_code=302)

    conn.execute(
        "UPDATE unmatched_parts SET tech_name=?, truck_id=?, warehouse_id=? WHERE id=?",
        (tech["name"], tech["truck_id"], tech["warehouse_id"], unmatched_id),
    )
    conn.commit()

    parts_msg = ""
    if deducted:
        parts_msg += f" {len(deducted)} item(s) deducted."
    if already:
        parts_msg += f" {len(already)} item(s) already deducted (skipped)."
    if unmatched_items:
        parts_msg += f" {len(unmatched_items)} item(s) queued under Unmatched Parts."
    if result.get("status") == "unmatched_package":
        parts_msg += " Package queued under Unmatched Packages."
    flash(request, "success",
         f"Confirmed tech '{tech['name']}' for '{row['raw_name']}'." + (parts_msg or " No items to process."))
    return RedirectResponse("/dashboard/unmatched", status_code=302)


# ─── Log ──────────────────────────────────────────────────────────────────────


@app.get("/dashboard/log", response_class=HTMLResponse)
async def log_page(
    request: Request,
    tech_name: str = None, truck_id: str = None, part_id: str = None,
    source: str = None, date_from: str = None, date_to: str = None,
    limit: int = 200, offset: int = 0,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    wh_filter = get_selected_warehouse(request, conn)
    log_entries = get_logs(conn, tech_name, truck_id, part_id, None, source, date_from, date_to, limit, offset, warehouse_id=wh_filter)
    wh_names = {w["id"]: w["name"] for w in conn.execute("SELECT id, name FROM warehouses").fetchall()}
    return render_template(request, "dashboard_log.html", {
        "log": log_entries, "wh_names": wh_names,
    }, conn=conn)


# ─── Alerts ────────────────────────────────────────────────────────────────────


@app.get("/dashboard/alerts", response_class=HTMLResponse)
async def alerts_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    wh_filter = get_selected_warehouse(request, conn)
    low = inventory.check_thresholds(conn, warehouse_id=wh_filter)
    wh_clause = ""
    wh_params = []
    if wh_filter != "all":
        wh_clause = " AND sa.warehouse_id=?"
        wh_params.append(wh_filter)
    stock_alerts = conn.execute(
        f"""SELECT sa.*, t.name AS truck_name, w.name AS warehouse_name,
                   COALESCE(s.quantity, 0) AS live_truck_qty,
                   COALESCE(ws.quantity, 0) AS live_wh_qty
            FROM stock_alerts sa
            LEFT JOIN trucks t ON t.id = sa.truck_id
            LEFT JOIN warehouses w ON w.id = sa.warehouse_id
            LEFT JOIN stock s ON s.truck_id = sa.truck_id AND s.part_id = sa.part_id
            LEFT JOIN warehouse_stock ws ON ws.warehouse_id = sa.warehouse_id AND ws.part_id = sa.part_id
            WHERE sa.resolved=0{wh_clause} ORDER BY sa.created_at DESC""",
        wh_params,
    ).fetchall()
    return render_template(request, "dashboard_alerts.html", {
        "alerts": {
            "truck_items": [dict(r) for r in low["truck_items"]],
            "warehouse_items": [dict(r) for r in low["warehouse_items"]],
        },
        "stock_alerts": stock_alerts,
    }, conn=conn)


@app.post("/dashboard/alerts/stock/resolve")
async def resolve_stock_alert(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    alert_id = form.get("alert_id")
    if not alert_id:
        return RedirectResponse("/dashboard/alerts", status_code=302)
    alert = conn.execute("SELECT * FROM stock_alerts WHERE id=?", (alert_id,)).fetchone()
    if not alert:
        flash(request, "error", "Alert not found.")
        return RedirectResponse("/dashboard/alerts", status_code=302)

    truck = conn.execute("SELECT * FROM trucks WHERE id=?", (alert["truck_id"],)).fetchone() if alert["truck_id"] else None
    warehouse = conn.execute("SELECT * FROM warehouses WHERE id=?", (alert["warehouse_id"],)).fetchone() if alert["warehouse_id"] else None
    truck_label = f"{truck['id']} — {truck['name']}" if truck else alert["truck_id"]
    wh_label = f"{warehouse['id']} — {warehouse['name']}" if warehouse else alert["warehouse_id"]

    live_truck = conn.execute(
        "SELECT quantity FROM stock WHERE truck_id=? AND part_id=?",
        (alert["truck_id"], alert["part_id"]),
    ).fetchone()
    live_truck_qty = live_truck["quantity"] if live_truck else 0
    live_wh = conn.execute(
        "SELECT quantity FROM warehouse_stock WHERE warehouse_id=? AND part_id=?",
        (alert["warehouse_id"], alert["part_id"]),
    ).fetchone()
    live_wh_qty = live_wh["quantity"] if live_wh else 0

    already = conn.execute(
        """SELECT COUNT(*) c FROM log
           WHERE part_id=? AND truck_id=? AND invoice_id=? AND quantity_change=-?""",
        (alert["part_id"], alert["truck_id"], alert["invoice_id"] or "", alert["requested_qty"]),
    ).fetchone()["c"]
    if already:
        conn.execute("UPDATE stock_alerts SET resolved=1 WHERE id=?", (alert_id,))
        conn.commit()
        flash(request, "success",
             f"'{alert['part_name']}' x{alert['requested_qty']} was already deducted "
             f"for this invoice on {truck_label} — alert marked resolved (no double deduction).")
        return RedirectResponse("/dashboard/alerts", status_code=302)

    result, err = inventory.deduct(
        conn, alert["truck_id"], alert["warehouse_id"], alert["part_id"],
        alert["requested_qty"], "system", alert["invoice_id"], 1.0, "itemized",
        raw_text=f"shortfall resolve: {alert['part_name']}",
        record_alert=False,
    )
    if err:
        msg = (
            f"Cannot deduct {alert['part_name']} x{alert['requested_qty']} "
            f"for truck {truck_label} (warehouse {wh_label}).\n"
            f"Current: truck has {live_truck_qty}, warehouse has {live_wh_qty}.\n{err}\n\n"
            f"Assign stock to the truck via Parts → Batch Assign (deducts from the "
            f"warehouse) or use Audit Adjust to correct values, then click Resolve again."
        )
        flash(request, "error", msg)
        return RedirectResponse("/dashboard/alerts", status_code=302)

    conn.execute("UPDATE stock_alerts SET resolved=1 WHERE id=?", (alert_id,))
    conn.commit()
    msg = (
        f"Resolved correctly — deducted {alert['part_name']} x{alert['requested_qty']} "
        f"from truck {truck_label} (warehouse {wh_label}). "
        f"Truck now has {live_truck_qty - alert['requested_qty']}. Logged to the Log."
    )
    flash(request, "success", msg)
    return RedirectResponse("/dashboard/alerts", status_code=302)


# ─── Top Movers ────────────────────────────────────────────────────────────────


@app.get("/dashboard/top-movers", response_class=HTMLResponse)
async def top_movers_page(
    request: Request, days: int = 30, limit: int = 10,
    order: str = "desc",
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    wh_filter = get_selected_warehouse(request, conn)
    rows = analytics.top_movers(conn, warehouse_id=wh_filter, days=days, limit=limit, order=order)

    return render_template(request, "dashboard_top_movers.html", {
        "movers": rows, "order": order,
    }, conn=conn)


# ─── Audit Report ────────────────────────────────────────────────────────────


@app.get("/dashboard/audit", response_class=HTMLResponse)
async def audit_report(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    from datetime import date, datetime
    wh_filter = get_selected_warehouse(request, conn)
    all_trucks = []
    truck_query = "SELECT * FROM trucks WHERE active=1"
    truck_params = ()
    if wh_filter != "all":
        truck_query += " AND warehouse_id=?"
        truck_params = (wh_filter,)
    for t in conn.execute(truck_query, truck_params).fetchall():
        wh = conn.execute("SELECT * FROM warehouses WHERE id=?", (t["warehouse_id"],)).fetchone()
        stock = conn.execute("""
            SELECT s.*, p.name AS part_name, p.unit
            FROM stock s JOIN parts p ON p.id = s.part_id
            WHERE s.truck_id=? ORDER BY p.name
        """, (t["id"],)).fetchall()
        all_trucks.append({
            "id": t["id"], "name": t["name"], "warehouse_name": wh["name"] if wh else "",
            "stock": [dict(s) for s in stock],
        })
    all_wh = []
    wh_query = "SELECT * FROM warehouses WHERE active=1"
    wh_params = ()
    if wh_filter != "all":
        wh_query += " AND id=?"
        wh_params = (wh_filter,)
    for w in conn.execute(wh_query, wh_params).fetchall():
        stock = conn.execute("""
            SELECT ws.*, p.name AS part_name, p.unit
            FROM warehouse_stock ws JOIN parts p ON p.id = ws.part_id
            WHERE ws.warehouse_id=? ORDER BY p.name
        """, (w["id"],)).fetchall()
        all_wh.append({
            "id": w["id"], "name": w["name"],
            "stock": [dict(s) for s in stock],
        })
    return render_template(request, "dashboard_audit.html", {
        "trucks": all_trucks, "warehouses": all_wh,
        "date": date.today().isoformat(),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }, conn=conn)


# ─── Manual Invoice Upload (for testing) ──────────────────────────────────────


@app.get("/dashboard/upload-invoice", response_class=HTMLResponse)
async def upload_invoice_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    return render_template(request, "dashboard_upload.html", {"result": None}, conn=conn)


@app.post("/dashboard/upload-invoice")
async def upload_invoice(
    request: Request, file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    import email
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders
    from modules.email_poller import process_single_email

    pdf_bytes = await file.read()
    msg = MIMEMultipart()
    msg["Subject"] = f"Manual upload: {file.filename}"
    msg["Message-ID"] = f"<manual-{file.filename}-{os.urandom(4).hex()}@manual>"
    part = MIMEBase("application", "pdf")
    part.set_payload(pdf_bytes)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=file.filename)
    msg.attach(part)
    result = process_single_email(conn, msg, b"manual")
    if "results" not in result:
        result = {"results": [result]}

    shortfalls = []
    for r in result.get("results", []):
        for d in r.get("deductions", []):
            if d.get("error"):
                shortfalls.append({
                    "raw_name": d.get("raw_name", d.get("part_id", "item")),
                    "qty": d.get("qty", ""),
                    "error": d["error"],
                })

    return render_template(request, "dashboard_upload.html", {
        "result": result,
        "shortfalls": shortfalls,
        "shortfall_count": len(shortfalls),
    }, conn=conn)


# ─── Usage Tracking ────────────────────────────────────────────────────────────


@app.get("/dashboard/usage", response_class=HTMLResponse)
async def usage_page(
    request: Request,
    warehouse_id: str = None, period: str = "month", truck_id: str = None,
    tech_name: str = None,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    all_wh = conn.execute("SELECT * FROM warehouses WHERE active=1 ORDER BY name").fetchall()
    if not warehouse_id:
        selected_wh = get_selected_warehouse(request, conn)
        warehouse_id = selected_wh if selected_wh != "all" else (all_wh[0]["id"] if all_wh else None)

    # Update session warehouse for convenience
    if warehouse_id and any(w["id"] == warehouse_id for w in all_wh):
        request.session["selected_warehouse"] = warehouse_id

    valid_periods = {"day": 1, "week": 7, "month": 30, "quarter": 90}
    days = valid_periods.get(period, 30)

    truck_usage = []
    selected_truck = None
    part_usage = []
    tech_usage = []
    selected_tech = None
    tech_part_usage = []

    if warehouse_id:
        truck_usage = analytics.usage_by_truck(conn, warehouse_id, days=days)
        tech_usage = analytics.usage_by_tech(conn, warehouse_id, days=days)
        if truck_id:
            selected_truck = trucks.get_truck(conn, truck_id)
            part_usage = analytics.usage_by_part(conn, truck_id, days=days)
        if tech_name:
            selected_tech = tech_name
            tech_part_usage = analytics.usage_by_part_for_tech(conn, tech_name, days=days)

    return render_template(request, "dashboard_usage.html", {
        "warehouses": all_wh,
        "warehouse_id": warehouse_id,
        "period": period,
        "truck_usage": truck_usage,
        "selected_truck": dict(selected_truck) if selected_truck else None,
        "part_usage": part_usage,
        "tech_usage": tech_usage,
        "selected_tech": selected_tech,
        "tech_part_usage": tech_part_usage,
    }, conn=conn)


# ─── Email Ingestion Webhook (Cloudflare Email Worker) ─────────────────────────


@app.post("/api/ingest/email")
async def ingest_email(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    secret = request.headers.get("X-Webhook-Secret", "")
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        events_mod.log_event(
            conn, "warn", "ingest", "ingest.unauthorized",
            "Webhook rejected (invalid X-Webhook-Secret)",
            detail={"from": request.headers.get("from", "")},
        )
        conn.commit()
        return JSONResponse({"status": "error", "error": "invalid_webhook_secret"}, status_code=401)

    raw = await request.body()
    if not raw:
        events_mod.log_event(
            conn, "warn", "ingest", "ingest.empty_body",
            "Webhook received with empty body",
        )
        conn.commit()
        return JSONResponse({"status": "error", "error": "empty_body"}, status_code=400)

    try:
        msg = email.message_from_bytes(raw)
    except Exception as e:
        logger.error(f"ingest: failed to parse MIME: {e}")
        events_mod.log_event(
            conn, "error", "ingest", "ingest.invalid_mime",
            f"Failed to parse MIME from webhook: {e}",
        )
        conn.commit()
        return JSONResponse({"status": "error", "error": "invalid_mime"}, status_code=400)

    subject = ""
    try:
        subject = msg.get("Subject", "") or ""
    except Exception:
        pass
    events_mod.log_event(
        conn, "info", "ingest", "ingest.received",
        f"Email received from webhook ({len(raw)} bytes)",
        detail={"subject": subject[:200]},
        ref=msg.get("Message-ID", "") or "",
    )
    conn.commit()

    try:
        result = process_single_email(conn, msg, "webhook")
    except Exception as e:
        logger.error(f"ingest: process_single_email crashed: {e}")
        try:
            events_mod.log_event(
                conn, "error", "ingest", "ingest.exception",
                f"Unhandled error while processing invoice email: {e}",
                detail={"subject": subject[:200]},
                ref=msg.get("Message-ID", "") or "",
            )
            conn.commit()
        except Exception:
            pass
        raise
    status_code = 200 if result.get("status") in ("processed", "skipped") else 202
    events_mod.log_event(
        conn, "info", "ingest", f"ingest.{result.get('status','unknown')}",
        f"Ingest status={result.get('status')}",
        detail={"subject": subject[:200], "reason": result.get("reason", "")},
        ref=result.get("invoice_id", ""),
    )
    conn.commit()
    return JSONResponse(result, status_code=status_code)


@app.get("/dashboard/door-orders", response_class=HTMLResponse)
async def door_orders_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    all_orders = door_orders_mod.list_all(conn)
    return render_template(request, "dashboard_door_orders.html", {
        "pending": [o for o in all_orders if o["status"] == "captured"],
        "ordered": [o for o in all_orders if o["status"] == "ordered"],
    }, conn=conn)


@app.post("/dashboard/door-orders/status")
async def door_orders_set_status(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    order_id = form.get("order_id")
    new_status = form.get("new_status")
    order, err = door_orders_mod.set_status(conn, order_id, new_status, "staff")
    if err == "not_found":
        flash(request, "error", "Door order not found.")
    elif err == "invalid_transition":
        flash(request, "error", "That status change isn't allowed from the order's current state.")
    else:
        flash(request, "success", f"Door order {order['job_wo']} marked {new_status}.")
    return RedirectResponse("/dashboard/door-orders", status_code=302)


# ─── PWA API (Winston tech) ────────────────────────────────────────────────────


@app.get("/tech/catalog")
async def tech_catalog(
    token: str = "",
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)
    return JSONResponse({
        "status": "ok",
        "tech": tech["name"],
        "truck_id": tech["truck_id"],
        "warehouse_id": tech["warehouse_id"],
        "parts": pwa_mod.get_truck_catalog(conn, tech),
    })


@app.post("/tech/deductions")
async def tech_deductions(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    token = body.get("token", "")
    batch_id = str(body.get("batch_id", "")).strip()
    items = body.get("items", [])

    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)
    if not batch_id:
        return JSONResponse({"status": "error", "error": "missing_batch_id"}, status_code=400)
    if not isinstance(items, list):
        return JSONResponse({"status": "error", "error": "items_must_be_list"}, status_code=400)

    result = pwa_mod.process_batch(conn, tech, batch_id, items)
    return JSONResponse(result)


# ─── Door order capture (PWA) ───────────────────────────────────────────────────
# NOT an inventory deduction — separate table, separate workflow. Shares only
# the PWA token auth with the deduction endpoints above.


@app.get("/tech/lookups/brands")
async def tech_lookup_brands():
    return JSONResponse({"brands": door_orders_mod.BRANDS})


@app.get("/tech/lookups/models")
async def tech_lookup_models(brand: str = ""):
    return JSONResponse({"models": door_orders_mod.list_models(brand)})


@app.get("/tech/lookups/designs")
async def tech_lookup_designs(model: str = ""):
    return JSONResponse({"designs": door_orders_mod.list_designs(model)})


@app.get("/tech/lookups/insulations")
async def tech_lookup_insulations(model: str = "", design: str = ""):
    return JSONResponse({"insulations": door_orders_mod.list_insulations(model, design)})


@app.get("/tech/lookups/sizes")
async def tech_lookup_sizes(model: str = "", design: str = "", insulation: str = ""):
    return JSONResponse(door_orders_mod.list_sizes(model, design, insulation or None))


@app.get("/tech/lookups/windows")
async def tech_lookup_windows(model: str = "", design: str = "", insulation: str = ""):
    return JSONResponse(door_orders_mod.window_info(model, design, insulation or None))


@app.get("/tech/lookups/colors")
async def tech_lookup_colors(model: str = ""):
    return JSONResponse({"colors": door_orders_mod.list_colors(model)})


@app.get("/tech/lookups/generic-options")
async def tech_lookup_generic_options():
    return JSONResponse(door_orders_mod.generic_form_options())


def _send_door_order_email(conn, order):
    """Single-attempt send (no multi-minute retry backoff — this runs inline
    in the tech's sync request, and the 60s notification target means we
    can't block on Resend retries). Failure is logged but never fails the
    door-order capture itself: the DB row is the source of truth."""
    to_list = [e for e in [NICK_EMAIL, MARIA_EMAIL] if e]
    if not to_list or not RESEND_API_KEY:
        return
    # Fixed production URL, not derived from the incoming request — the
    # request's Host varies by client (reverse proxy, curl, a test client)
    # and previously produced broken links like "http://testserver/...".
    # Points at /login (not directly at /dashboard/door-orders) since the
    # recipient isn't authenticated yet when they open the email.
    dashboard_url = PUBLIC_URL + "/login"
    html = door_order_html(order, dashboard_url=dashboard_url)
    subject = f"[DoorFlex] Door sold — {order['job_wo']} — order required"
    try:
        resp = send_html(RESEND_API_KEY, get_report_from(), to_list, subject, html)
        log_email(conn, "door_order", subject, ",".join(to_list), "sent", response=str(resp)[:500])
        events_mod.log_event(
            conn, "info", "email", "email.door_order.sent",
            f"Door order email sent for WO {order['job_wo']}",
            detail={"to": to_list, "job_wo": order["job_wo"]},
            ref=order["job_wo"],
        )
    except Exception as e:
        log_email(conn, "door_order", subject, ",".join(to_list), "failed", response=str(e)[:500])
        events_mod.log_event(
            conn, "error", "email", "email.door_order.failed",
            f"Door order email failed for WO {order['job_wo']}: {e}",
            detail={"to": to_list, "job_wo": order["job_wo"], "error": str(e)},
            ref=order["job_wo"],
        )
    conn.commit()


@app.post("/tech/door-orders")
async def tech_door_orders(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    token = body.get("token", "")
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)

    order, status = door_orders_mod.create_door_order(conn, tech, body)
    if status == "missing_job_wo":
        return JSONResponse({"status": "error", "error": "missing_job_wo"}, status_code=400)
    if status == "wo_already_closed":
        return JSONResponse({"status": "error", "error": "wo_already_closed"}, status_code=409)
    if status == "already_processed":
        return JSONResponse({"status": "already_processed", "order": order})

    _send_door_order_email(conn, order)
    return JSONResponse({"status": "processed", "order": order})


@app.post("/tech/door-orders/cancel")
async def tech_door_orders_cancel(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    token = body.get("token", "")
    job_wo = str(body.get("job_wo", "")).strip()
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)

    order, err = door_orders_mod.cancel_by_tech(conn, tech, job_wo)
    if err == "not_found":
        return JSONResponse({"status": "error", "error": "not_found"}, status_code=404)
    if err == "not_owner":
        return JSONResponse({"status": "error", "error": "forbidden"}, status_code=403)
    if err == "invalid_transition":
        return JSONResponse({"status": "error", "error": "already_actioned_by_office"}, status_code=409)
    return JSONResponse({"status": "ok", "order": order})


# ─── Job capture (PWA) ──────────────────────────────────────────────────────────
# Independent evidence record (PRD: "the system of record") — separate from
# both deductions and door orders. Shares only the PWA token auth.

JOB_CAPTURE_PHOTOS_DIR = Path(DATABASE_PATH).parent / "job_captures"
WINSTON_REPORTS_DIR = Path(DATABASE_PATH).parent / "winston_reports"


@app.get("/tech/jobs/today")
async def tech_jobs_today(
    token: str = "", date: str = "",
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)
    date_str = date or datetime.now().strftime("%Y-%m-%d")
    jobs = job_captures_mod.list_jobs_for_tech_today(conn, tech["id"], date_str)
    return JSONResponse({"jobs": jobs, "date": date_str})


@app.post("/tech/job-captures")
async def tech_job_captures(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    token = body.get("token", "")
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)

    capture, status = job_captures_mod.create_job_capture(conn, tech, body)
    if status in ("missing_batch_id", "missing_wo_number", "invalid_payment_method", "missing_closing_notes"):
        return JSONResponse({"status": "error", "error": status}, status_code=400)
    if status == "wo_already_closed":
        return JSONResponse({"status": "error", "error": "wo_already_closed"}, status_code=409)
    if status == "already_processed":
        return JSONResponse({"status": "already_processed", "capture": capture})
    return JSONResponse({"status": "processed", "capture": capture})


@app.post("/tech/job-captures/photo")
async def tech_job_captures_photo(
    token: str = "",
    batch_id: str = Form(...),
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)

    batch_id = str(batch_id).strip()
    if not batch_id:
        return JSONResponse({"status": "error", "error": "missing_batch_id"}, status_code=400)

    JOB_CAPTURE_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
        ext = ".jpg"
    dest = JOB_CAPTURE_PHOTOS_DIR / f"{batch_id}{ext}"
    dest.write_bytes(await file.read())

    job_captures_mod.set_photo_path(conn, batch_id, dest.name)
    return JSONResponse({"status": "ok", "receipt_photo_path": dest.name})


@app.get("/dashboard/receipts/{filename}")
async def dashboard_receipt_photo(request: Request, filename: str):
    require_auth_html(request)
    safe_name = Path(filename).name
    path = JOB_CAPTURE_PHOTOS_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)


@app.get("/dashboard/winston-reports/{filename}")
async def dashboard_winston_report_photo(request: Request, filename: str):
    require_auth_html(request)
    safe_name = Path(filename).name
    path = WINSTON_REPORTS_DIR / safe_name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)


@app.post("/tech/push/subscribe")
async def tech_push_subscribe(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    token = body.get("token", "")
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)

    endpoint = str(body.get("endpoint", "")).strip()
    keys = body.get("keys", {}) or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return JSONResponse({"status": "error", "error": "invalid_subscription"}, status_code=400)

    push_mod.upsert_subscription(
        conn, tech["id"], endpoint, keys["p256dh"], keys["auth"],
        user_agent=request.headers.get("user-agent", "")[:255],
    )
    return JSONResponse({"status": "ok"})


@app.get("/tech/push/vapid-public-key")
async def tech_push_vapid_public_key():
    return JSONResponse({"key": VAPID_PUBLIC_KEY})


# ─── Jobs Today / reconciliation dashboard (Maria-facing) ──────────────────────
# Manual entry replaces the Dispatch.me scraper for now (PRD §4.1.4 — deferred,
# no dedicated laptop/n8n in this pass). Same shared dashboard login as Nick.


@app.get("/dashboard/jobs-today", response_class=HTMLResponse)
async def jobs_today_page(request: Request, date: str = "", conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    date_str = date or datetime.now().strftime("%Y-%m-%d")
    jobs = job_captures_mod.list_jobs_for_date(conn, date_str)
    for j in jobs:
        j["window_start_display"] = job_captures_mod.format_window_time(j["window_start"])
        j["window_end_display"] = job_captures_mod.format_window_time(j["window_end"])
    all_techs = techs.list_techs(conn)
    wo_numbers = [j["wo_number"] for j in jobs]
    # Invoice-parity view: charge (job_captures), parts used (log, via the
    # tech's WO# tag on a PWA deduction), and door orders side by side,
    # matching how Dispatch invoices work for out-of-pocket jobs.
    captures_by_wo = job_captures_mod.get_captures_by_wo(conn, wo_numbers)
    parts_by_wo = get_parts_used_by_wo(conn, wo_numbers)
    doors_by_wo = door_orders_mod.get_orders_by_wo(conn, wo_numbers)
    try:
        date_display = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    except ValueError:
        date_display = date_str
    return render_template(request, "dashboard_jobs_today.html", {
        "jobs": jobs,
        "date": date_str,
        "date_display": date_display,
        "all_techs": all_techs,
        "captures_by_wo": captures_by_wo,
        "parts_by_wo": parts_by_wo,
        "doors_by_wo": doors_by_wo,
    }, conn=conn)


@app.post("/dashboard/jobs-today/create")
async def jobs_today_create(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    job, err = job_captures_mod.create_expected_job(conn, dict(form))
    if err == "missing_wo_number":
        flash(request, "error", "WO# is required.")
    elif err == "duplicate_wo_number":
        flash(request, "error", "That WO# is already on today's list.")
    else:
        flash(request, "success", f"Job {job['wo_number']} added.")
    return RedirectResponse("/dashboard/jobs-today", status_code=302)


@app.post("/dashboard/jobs-today/mark-removed")
async def jobs_today_mark_removed(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    job_id = form.get("job_id")
    wo_number, err = job_captures_mod.mark_removed_by_dispatch(conn, job_id)
    if err == "not_found":
        flash(request, "error", "Job not found.")
    else:
        flash(request, "warning", f"WO# {wo_number} marked removed by Dispatch — dispute opened.")
    return RedirectResponse("/dashboard/jobs-today", status_code=302)


@app.get("/dashboard/overview-today", response_class=HTMLResponse)
async def overview_today_page(request: Request, date: str = "", conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    date_str = date or datetime.now().strftime("%Y-%m-%d")
    result = job_captures_mod.compute_reconciliation(conn, date_str)
    return render_template(request, "dashboard_overview_today.html", {
        "result": result,
        "date": date_str,
    }, conn=conn)


@app.get("/dashboard/disputes", response_class=HTMLResponse)
async def disputes_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    rows = conn.execute(
        """SELECT d.*, jc.closing_notes, jc.amount_charged, jc.receipt_photo_path,
                  jc.tech_id, t.name AS tech_name
           FROM disputes d
           LEFT JOIN job_captures jc ON jc.id = d.capture_id
           LEFT JOIN techs t ON t.id = jc.tech_id
           ORDER BY d.created_at DESC"""
    ).fetchall()
    return render_template(request, "dashboard_disputes.html", {
        "disputes": [dict(r) for r in rows],
    }, conn=conn)


@app.post("/dashboard/disputes/status")
async def disputes_set_status(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    dispute_id = form.get("dispute_id")
    new_status = form.get("new_status")
    notes = form.get("notes", "")
    conn.execute(
        "UPDATE disputes SET status=?, notes=?, updated_at=datetime('now','localtime') WHERE id=?",
        (new_status, notes, dispute_id),
    )
    conn.commit()
    flash(request, "success", "Dispute updated.")
    return RedirectResponse("/dashboard/disputes", status_code=302)


# ─── Weekly Commission (Winston report parsing + reconciliation) ───────────────
# Every upload goes through parse -> review/edit -> confirm. Nothing reaches
# winston_report_jobs straight from the raw AI parse — see modules/winston_reports.py.


@app.get("/dashboard/commission", response_class=HTMLResponse)
async def commission_page(request: Request, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    drafts = winston_reports_mod.list_reports(conn, status="draft")
    confirmed = winston_reports_mod.list_reports(conn, status="confirmed")
    summaries = []
    for r in confirmed:
        summaries.append({
            "report": r,
            "commission": winston_reports_mod.commission_summary(conn, r["id"]),
        })
    return render_template(request, "dashboard_commission.html", {
        "drafts": drafts,
        "confirmed": summaries,
    }, conn=conn)


@app.post("/dashboard/commission/upload")
async def commission_upload(
    request: Request, file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    require_auth_html(request)
    image_bytes = await file.read()
    content_type = file.content_type or "image/jpeg"

    try:
        parsed = winston_reports_mod.parse_report_image(image_bytes, content_type)
    except Exception as e:
        logger.error(f"winston report parse failed: {e}")
        events_mod.log_event(
            conn, "error", "winston_reports", "winston_report.parse_failed",
            f"Vision parse failed: {e}", detail={"error": str(e)},
        )
        conn.commit()
        flash(request, "error", f"Couldn't read that image: {e}")
        return RedirectResponse("/dashboard/commission", status_code=302)

    WINSTON_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "").suffix.lower() or ".jpg"
    report_id_placeholder = str(int(time_mod.time() * 1000))
    dest = WINSTON_REPORTS_DIR / f"{report_id_placeholder}{ext}"
    dest.write_bytes(image_bytes)

    report_id = winston_reports_mod.create_draft(conn, parsed, dest.name)
    events_mod.log_event(
        conn, "info", "winston_reports", "winston_report.draft_created",
        f"Winston report draft #{report_id} created ({len(parsed.get('jobs', []))} jobs parsed)",
        detail={"report_id": report_id}, ref=str(report_id),
    )
    conn.commit()
    return RedirectResponse(f"/dashboard/commission/review/{report_id}", status_code=302)


@app.get("/dashboard/commission/review/{report_id}", response_class=HTMLResponse)
async def commission_review(request: Request, report_id: int, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    report = winston_reports_mod.get_report(conn, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    if report["status"] == "confirmed":
        # Read-only detail view — sourced from the real winston_report_jobs
        # rows (what was actually saved), never from the original AI parse.
        jobs = winston_reports_mod.report_jobs(conn, report_id)
        result = winston_reports_mod.reconcile_against_report(conn, report_id)
        return render_template(request, "dashboard_commission_review.html", {
            "report": report,
            "confirmed_jobs": jobs,
            "commission": winston_reports_mod.commission_summary(conn, report_id),
            "result": result,
        }, conn=conn)

    parsed = json.loads(report["parsed_json"]) if report["parsed_json"] else {}
    warnings = winston_reports_mod.validate_draft(parsed)
    dup = winston_reports_mod.find_overlapping_confirmed_report(
        conn, parsed.get("period_start", ""), parsed.get("period_end", ""))
    warnings_by_row = {}
    report_level_warnings = []
    for w in warnings:
        if w["row_index"] is None:
            report_level_warnings.append(w)
        else:
            warnings_by_row.setdefault(w["row_index"], []).append(w)
    return render_template(request, "dashboard_commission_review.html", {
        "report": report,
        "parsed": parsed,
        "jobs": parsed.get("jobs", []) or [],
        "warnings_by_row": warnings_by_row,
        "report_level_warnings": report_level_warnings,
        "duplicate_report": dup,
    }, conn=conn)


@app.post("/dashboard/commission/confirm/{report_id}")
async def commission_confirm(request: Request, report_id: int, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    form = await request.form()
    row_count = int(form.get("row_count", "0") or "0")
    edited_rows = []
    for i in range(row_count):
        edited_rows.append({
            "date": form.get(f"row-{i}-date", ""),
            "wo_number": form.get(f"row-{i}-wo_number", ""),
            "total_sales": _to_float(form.get(f"row-{i}-total_sales", "")),
            "payout": _to_float(form.get(f"row-{i}-payout", "")),
            "commission": _to_float(form.get(f"row-{i}-commission", "")),
            "notes": form.get(f"row-{i}-notes", ""),
        })
    edited_totals = {
        "tech_name": form.get("tech_name", ""),
        "period_start": form.get("period_start", ""),
        "period_end": form.get("period_end", ""),
        "subtotal_sales": _to_float(form.get("subtotal_sales", "")),
        "subtotal_payout": _to_float(form.get("subtotal_payout", "")),
        "parts_deduction": _to_float(form.get("parts_deduction", "")),
        "total_commission": _to_float(form.get("total_commission", "")),
    }
    report, err = winston_reports_mod.confirm_draft(conn, report_id, edited_rows, edited_totals)
    if err == "not_found":
        flash(request, "error", "Report not found.")
        return RedirectResponse("/dashboard/commission", status_code=302)
    flash(request, "success", f"Report confirmed — {len(edited_rows)} job(s) saved.")
    return RedirectResponse("/dashboard/commission", status_code=302)


def _to_float(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@app.get("/dashboard/commission/export.csv")
async def commission_export_csv(request: Request, report_id: int, conn: sqlite3.Connection = Depends(get_db_conn)):
    require_auth_html(request)
    report = winston_reports_mod.get_report(conn, report_id)
    if not report or report["status"] != "confirmed":
        raise HTTPException(status_code=404, detail="Confirmed report not found")
    jobs = winston_reports_mod.report_jobs(conn, report_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["WO#", "Date", "Total Sales", "Payout", "Commission", "Notes"])
    for j in jobs:
        writer.writerow([j["wo_number"], j["report_date"], j["total_sales"], j["payout"], j["commission"], j["notes"]])

    filename = f"commission-{report['period_start']}-{report['period_end']}.csv"
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─── Job list capture (tech-uploaded Dispatch screenshot) ──────────────────────
# Replaces the deferred Dispatch.me scraper. Same parse -> review -> confirm
# shape: the tech reviews/corrects the parsed list in the PWA before anything
# reaches expected_jobs.


@app.post("/tech/jobs/parse-screenshot")
async def tech_jobs_parse_screenshot(
    token: str = "",
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)

    image_bytes = await file.read()
    content_type = file.content_type or "image/jpeg"
    try:
        jobs = job_captures_mod.parse_dispatch_screenshot(image_bytes, content_type)
    except Exception as e:
        logger.error(f"dispatch screenshot parse failed: {e}")
        events_mod.log_event(
            conn, "error", "job_captures", "jobs.screenshot_parse_failed",
            f"Vision parse failed: {e}", detail={"error": str(e), "tech": tech["name"]},
        )
        conn.commit()
        return JSONResponse({"status": "error", "error": "parse_failed", "detail": str(e)}, status_code=502)

    warnings = job_captures_mod.validate_screenshot_jobs(jobs)
    return JSONResponse({"status": "ok", "jobs": jobs, "warnings": warnings})


@app.post("/tech/jobs/confirm-screenshot")
async def tech_jobs_confirm_screenshot(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    token = body.get("token", "")
    tech = pwa_mod.get_tech_by_token(conn, token)
    if not tech:
        return JSONResponse({"status": "error", "error": "invalid_token"}, status_code=401)

    jobs = body.get("jobs", [])
    if not isinstance(jobs, list):
        return JSONResponse({"status": "error", "error": "jobs_must_be_list"}, status_code=400)

    result = job_captures_mod.upsert_from_screenshot(conn, tech["id"], jobs)
    return JSONResponse({"status": "ok", **result})


# ─── QuickBooks Online webhook (DoorFlex invoice → automatic stock deduction) ─
# Intuit posts a small "notification" payload here whenever an Invoice is
# created/updated/deleted in the connected QBO company (Nick's DoorFlex).
# The full invoice is fetched via the REST API inside qbo_ingest, so all this
# route does is: verify the signature, dedupe, iterate entities, hand off.
#
# The endpoint must return 2xx fast — Intuit retries aggressively on non-2xx.
# We treat non-invoice entities and non-Invoice change types as no-ops (200).


@app.post("/webhooks/qbo/invoice")
async def qbo_invoice_webhook(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db_conn),
):
    import hmac, hashlib, base64
    from config import QBO_ENABLED, QBO_WEBHOOK_VERIFIER_TOKEN

    if not QBO_ENABLED:
        return JSONResponse({"status": "error", "error": "qbo_not_configured"}, status_code=501)

    raw = await request.body()
    signature_header = request.headers.get("intuit-signature", "")
    if not signature_header:
        return JSONResponse({"status": "error", "error": "missing_signature"}, status_code=401)
    expected = base64.b64encode(
        hmac.new(QBO_WEBHOOK_VERIFIER_TOKEN.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected, signature_header):
        return JSONResponse({"status": "error", "error": "bad_signature"}, status_code=401)

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return JSONResponse({"status": "error", "error": "invalid_json"}, status_code=400)

    processed = 0
    skipped = 0
    errors = []
    for notif in body.get("eventNotifications", []) or []:
        realm_id = str(notif.get("realmId", "")).strip()
        entities = (notif.get("dataChangeEvent", {}) or {}).get("entities", []) or []
        for ent in entities:
            if ent.get("name") != "Invoice":
                skipped += 1
                continue
            operation = ent.get("operation", "")
            if operation not in ("Create", "Update"):
                skipped += 1
                continue
            invoice_id = str(ent.get("id", "")).strip()
            if not realm_id or not invoice_id:
                skipped += 1
                continue
            try:
                qbo_ingest_mod.process_invoice_event(conn, realm_id, invoice_id)
                processed += 1
            except qbo_ingest_mod.QboIngestError as e:
                errors.append({"invoice_id": invoice_id, "error": str(e)})
            except qbo_oauth_mod.QboOAuthError as e:
                errors.append({"invoice_id": invoice_id, "error": f"oauth: {e}"})

    # Intuit needs 2xx even on partial failures, else it retries the whole
    # batch and we'd re-do the successes. Errors are logged inside the ingest
    # module (event log + owner-panel visibility), not swallowed.
    return JSONResponse({
        "status": "ok", "processed": processed, "skipped": skipped,
        "errors": errors,
    })


# ─── Error handler ──────────────────────────────────────────────────────────────


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc: HTTPException):
    return RedirectResponse("/login", status_code=302)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
