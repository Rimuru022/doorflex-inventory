"""Owner panel — secret-path, password-protected tools.

Everything in this module is independent from the staff dashboard: DevTools
(reset DB), Backups, and Reports. The router is only mounted when the owner
configured both ADMIN_PATH and ADMIN_ACCESS_PASSWORD; otherwise the panel is
disabled (all routes 404).

Add future owner-only features here — just append routes to the router returned
by `build_owner_router`.
"""
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from config import (
    ADMIN_ACCESS_PASSWORD, DATABASE_PATH, QBO_ENABLED, REPORT_RECIPIENTS,
    RESEND_API_KEY,
)
from database import (
    backup_db, delete_backup, get_backup_dir, get_db, init_db, list_backups,
    reset_database, restore_everything_from_zip,
)
from modules.email_reporter import build_report_data, render_report_pdf, send_report_pdf
from modules import events as events_mod
from modules import qbo_oauth


class LoginRateLimiter:
    """Block brute force: max MAX_ATTEMPTS failures per IP per WINDOW_SECONDS."""

    MAX_ATTEMPTS = 5
    WINDOW_SECONDS = 300

    def __init__(self):
        self._fails = {}
        self._lock = threading.Lock()

    def _prune(self, ip, now):
        ts = [t for t in self._fails.get(ip, []) if now - t < self.WINDOW_SECONDS]
        self._fails[ip] = ts
        return ts

    def allowed(self, ip):
        with self._lock:
            return len(self._prune(ip, time.time())) < self.MAX_ATTEMPTS

    def record_failure(self, ip):
        with self._lock:
            self._fails.setdefault(ip, []).append(time.time())

    def clear(self, ip):
        with self._lock:
            self._fails.pop(ip, None)

    def retry_after_seconds(self, ip):
        now = time.time()
        with self._lock:
            ts = self._prune(ip, now)
            if not ts:
                return 0
            return int(self.WINDOW_SECONDS - (now - ts[0]))


_limiter = LoginRateLimiter()


def _get_db():
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def _sqlite_quick_check(path: Path):
    try:
        c = sqlite3.connect(str(path))
        row = c.execute("PRAGMA quick_check").fetchone()
        c.close()
        return bool(row and row[0] == "ok")
    except Exception:
        return False


def build_owner_router(prefix: str, render_template, flash):
    """Build the owner router. `render_template`/`flash` are injected from main
    to avoid a circular import; everything else lives in this module."""

    router = APIRouter(prefix=prefix)

    def require_owner(request: Request):
        if not request.session.get("owner_authenticated"):
            # 404 (not 401/redirect) so the panel's existence stays hidden.
            raise HTTPException(status_code=404, detail="Not found")

    def _client_ip(request: Request):
        return request.client.host if request.client else "unknown"

    # ─── Entry / login / logout ────────────────────────────────────────────

    @router.get("")
    @router.get("/")
    async def owner_index(request: Request):
        if request.session.get("owner_authenticated"):
            return RedirectResponse(f"{prefix}/dev", status_code=302)
        return RedirectResponse(f"{prefix}/login", status_code=302)

    @router.get("/login")
    async def owner_login_page(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        return render_template(request, "owner_login.html", {"admin_path": prefix}, conn=conn)

    @router.post("/login")
    async def owner_login(request: Request, password: str = Form(""), conn: sqlite3.Connection = Depends(_get_db)):
        ip = _client_ip(request)
        if not _limiter.allowed(ip):
            retry = max(_limiter.retry_after_seconds(ip), 1)
            return render_template(request, "owner_login.html", {
                "admin_path": prefix,
                "error": f"Too many attempts. Try again in {retry} seconds.",
            }, conn=conn)
        if password == ADMIN_ACCESS_PASSWORD:
            _limiter.clear(ip)
            request.session["owner_authenticated"] = True
            return RedirectResponse(f"{prefix}/dev", status_code=302)
        _limiter.record_failure(ip)
        return render_template(request, "owner_login.html", {
            "admin_path": prefix,
            "error": "Invalid password",
        }, conn=conn)

    @router.get("/logout")
    async def owner_logout(request: Request):
        request.session.pop("owner_authenticated", None)
        return RedirectResponse(f"{prefix}/login", status_code=302)

    # ─── Backups ───────────────────────────────────────────────────────────

    @router.get("/backups")
    async def owner_backups(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        return render_template(request, "owner_backups.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "backups": list_backups(),
        }, conn=conn)

    @router.post("/backups/create")
    async def owner_backup_create(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        path = backup_db()
        if path:
            flash(request, "success", f"Backup created: {Path(path).name}")
        else:
            flash(request, "warning", "No database file found — nothing to back up yet.")
        return RedirectResponse(f"{prefix}/backups", status_code=302)

    @router.get("/backups/download")
    async def owner_backup_download(request: Request, file: str = "", conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        if not file or ".." in file or "/" in file or "\\" in file:
            raise HTTPException(status_code=404, detail="Not found")
        path = get_backup_dir() / file
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        from fastapi.responses import FileResponse
        return FileResponse(path, filename=file, media_type="application/octet-stream")

    @router.post("/backups/delete")
    async def owner_backup_delete(request: Request, file: str = Form(...), conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        if not file or ".." in file or "/" in file or "\\" in file:
            raise HTTPException(status_code=404, detail="Not found")
        if delete_backup(file):
            flash(request, "success", f"Deleted backup: {file}")
        else:
            flash(request, "warning", "That backup no longer exists.")
        return RedirectResponse(f"{prefix}/backups", status_code=302)

    @router.post("/backups/restore")
    async def owner_backup_restore(request: Request, file: UploadFile = File(...), conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        data = await file.read()
        is_sqlite = data[:16] == b"SQLite format 3\x00"
        is_zip = data[:4] == b"PK\x03\x04"
        if not is_sqlite and not is_zip:
            flash(request, "error", "That file isn't a recognized backup (.db or .zip).")
            return RedirectResponse(f"{prefix}/backups", status_code=302)

        suffix = ".db" if is_sqlite else ".zip"
        tmp = Path(DATABASE_PATH).with_name(f"restore-tmp{suffix}")
        tmp.write_bytes(data)
        try:
            if is_zip:
                try:
                    with zipfile.ZipFile(tmp, "r") as zf:
                        bad = zf.testzip()
                    if bad:
                        flash(request, "error", f"The uploaded archive is corrupt (bad entry: {bad}).")
                        return RedirectResponse(f"{prefix}/backups", status_code=302)
                except zipfile.BadZipFile:
                    flash(request, "error", "The uploaded file isn't a valid zip archive.")
                    return RedirectResponse(f"{prefix}/backups", status_code=302)
                backup_db()  # safety snapshot of current state before replacing it
                restore_everything_from_zip(tmp)
                init_db(seed=True)
                flash(request, "success", f"Restored everything (database + photos) from '{file.filename}'. Schema re-verified.")
            else:
                if not _sqlite_quick_check(tmp):
                    flash(request, "error", "The uploaded database failed validation (PRAGMA quick_check).")
                    return RedirectResponse(f"{prefix}/backups", status_code=302)
                backup_db()
                live = Path(DATABASE_PATH)
                live.parent.mkdir(parents=True, exist_ok=True)
                live.unlink(missing_ok=True)
                shutil.copy2(tmp, live)
                init_db(seed=True)
                flash(request, "success", f"Database restored from '{file.filename}' (database only — this file predates full backups, or was a database-only export). Schema re-verified.")
        finally:
            tmp.unlink(missing_ok=True)
        return RedirectResponse(f"{prefix}/backups", status_code=302)

    # ─── Dev Tools ─────────────────────────────────────────────────────────

    @router.get("/dev")
    async def owner_dev(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        return render_template(request, "owner_dev.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
        }, conn=conn)

    @router.post("/dev/reset")
    async def owner_dev_reset(request: Request, confirm: str = Form(""), conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        if confirm.strip().upper() != "RESET":
            flash(request, "error", "Type RESET to confirm. Nothing was changed.")
            return RedirectResponse(f"{prefix}/dev", status_code=302)
        reset_database()
        flash(request, "success", "Database reset complete — all data wiped and re-seeded with defaults.")
        return RedirectResponse(f"{prefix}/dev", status_code=302)

    # ─── PWA tech tokens ────────────────────────────────────────────────────

    @router.get("/pwa")
    async def owner_pwa(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        from modules import pwa as pwa_mod
        techs = conn.execute(
            """SELECT t.*, tr.name AS truck_name
               FROM techs t LEFT JOIN trucks tr ON tr.id = t.truck_id
               ORDER BY t.name"""
        ).fetchall()
        return render_template(request, "owner_pwa.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "techs": techs,
        }, conn=conn)

    @router.post("/pwa/rotate")
    async def owner_pwa_rotate(tech_id: int = Form(...), request: Request = None,
                               conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        from modules import pwa as pwa_mod
        tech = conn.execute("SELECT * FROM techs WHERE id=?", (tech_id,)).fetchone()
        if not tech:
            flash(request, "error", "Tech not found.")
        else:
            raw = pwa_mod.rotate_tech_token(conn, tech_id)
            flash(request, "success",
                  f"New install link for {tech['name']}: /tech/pwa/?t={raw}")
        return RedirectResponse(f"{prefix}/pwa", status_code=302)

    @router.post("/pwa/clear")
    async def owner_pwa_clear(tech_id: int = Form(...), request: Request = None,
                              conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        from modules import pwa as pwa_mod
        pwa_mod.clear_tech_token(conn, tech_id)
        flash(request, "success", "PWA token cleared — that tech can no longer sync.")
        return RedirectResponse(f"{prefix}/pwa", status_code=302)

    # ─── Observability (events / requests / emails / diagrams) ─────────────

    @router.get("/observe")
    async def owner_observe(
        request: Request,
        source: str = "", level: str = "", hours: int = 24,
        search: str = "", limit: int = 100, offset: int = 0,
        conn: sqlite3.Connection = Depends(_get_db),
    ):
        require_owner(request)
        counts = events_mod.counts_24h(conn)
        source_options = list(events_mod._UNIFIED_SOURCES)
        levels = None
        if level:
            levels = [l.strip() for l in level.split(",") if l.strip() in ("error", "warn")]
        rows, total = events_mod.unified_errors(
            conn,
            sources=[s.strip() for s in source.split(",") if s.strip()] or None,
            levels=levels or None,
            hours=hours, search=search or None,
            limit=min(max(limit, 10), 500), offset=max(offset, 0),
        )
        recent, _ = events_mod.list_events(conn, limit=50)
        days_rows = events_mod.requests_by_day(conn, days=7)
        max_n = max((r["n"] for r in days_rows), default=1) or 1
        days_chart = [
            {"day": r["day"], "n": r["n"], "pct": int(r["n"] * 100 / max_n)}
            for r in days_rows
        ]
        source_counts = events_mod.errors_by_source(conn, hours=hours)
        return render_template(request, "owner_observe.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "active_page": "observe",
            "counts": counts,
            "source_counts": source_counts,
            "rows": [dict(r) for r in rows],
            "total": total,
            "filters": {"source": source, "level": level, "hours": hours, "search": search},
            "source_options": source_options,
            "limit": limit, "offset": offset,
            "recent": [dict(r) for r in recent],
            "days_chart": days_chart,
        }, conn=conn)

    @router.get("/observe/events")
    async def owner_observe_events(
        request: Request,
        scope: str = "", level: str = "", event_type: str = "",
        date_from: str = "", date_to: str = "", search: str = "",
        limit: int = 200, offset: int = 0,
        conn: sqlite3.Connection = Depends(_get_db),
    ):
        require_owner(request)
        events, total = events_mod.list_events(
            conn,
            scope=scope or None, level=level or None, event_type=event_type or None,
            date_from=date_from or None, date_to=date_to or None, search=search or None,
            limit=min(max(limit, 10), 500), offset=max(offset, 0),
        )
        # Distinct scopes + event_types for filter dropdowns.
        scope_rows = [r["scope"] for r in conn.execute(
            "SELECT DISTINCT scope FROM app_events WHERE scope<>'' ORDER BY scope").fetchall()]
        type_rows = [r["event_type"] for r in conn.execute(
            "SELECT DISTINCT event_type FROM app_events WHERE event_type<>'' ORDER BY event_type"
        ).fetchall()]
        return render_template(request, "owner_observe_events.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "active_page": "observe",
            "events": [dict(r) for r in events],
            "total": total,
            "filters": {
                "scope": scope, "level": level, "event_type": event_type,
                "date_from": date_from, "date_to": date_to, "search": search,
            },
            "scope_options": scope_rows,
            "type_options": type_rows,
            "limit": limit, "offset": offset,
        }, conn=conn)

    @router.get("/observe/requests")
    async def owner_observe_requests(
        request: Request,
        path_prefix: str = "", status: str = "", method: str = "",
        date_from: str = "", date_to: str = "",
        limit: int = 200, offset: int = 0,
        conn: sqlite3.Connection = Depends(_get_db),
    ):
        require_owner(request)
        status_int = None
        if status:
            try:
                status_int = int(status)
            except ValueError:
                status_int = None
        rows, total = events_mod.list_requests(
            conn,
            path_prefix=path_prefix or None, status=status_int, method=method or None,
            date_from=date_from or None, date_to=date_to or None,
            limit=min(max(limit, 10), 500), offset=max(offset, 0),
        )
        return render_template(request, "owner_observe_requests.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "active_page": "observe",
            "rows": [dict(r) for r in rows],
            "total": total,
            "filters": {
                "path_prefix": path_prefix, "status": status, "method": method,
                "date_from": date_from, "date_to": date_to,
            },
            "limit": limit, "offset": offset,
        }, conn=conn)

    @router.get("/observe/emails")
    async def owner_observe_emails(
        request: Request,
        status: str = "", email_type: str = "",
        limit: int = 200, offset: int = 0,
        conn: sqlite3.Connection = Depends(_get_db),
    ):
        require_owner(request)
        conditions = []
        params = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if email_type:
            conditions.append("email_type=?")
            params.append(email_type)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM email_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [min(max(limit, 10), 500), max(offset, 0)],
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM email_log {where}", params).fetchone()[0]
        type_rows = [r["email_type"] for r in conn.execute(
            "SELECT DISTINCT email_type FROM email_log WHERE email_type<>'' ORDER BY email_type"
        ).fetchall()]
        return render_template(request, "owner_observe_emails.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "active_page": "observe",
            "rows": [dict(r) for r in rows],
            "total": total,
            "filters": {"status": status, "email_type": email_type},
            "type_options": type_rows,
            "limit": limit, "offset": offset,
        }, conn=conn)

    @router.get("/observe/diagrams")
    async def owner_observe_diagrams(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        return render_template(request, "owner_observe_diagrams.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "active_page": "observe",
        }, conn=conn)

    @router.get("/observe/api/module-deps")
    async def owner_observe_module_deps(request: Request):
        """Generate the module-dependency graph on demand from real imports."""
        require_owner(request)
        from pathlib import Path as _P
        from scripts.deps_mermaid import build_mermaid
        text = build_mermaid(_P(__file__).resolve().parent.parent)
        return JSONResponse({"mermaid": text})

    # ─── Monthly Report ────────────────────────────────────────────────────

    @router.get("/reports")
    async def owner_reports(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        return render_template(request, "owner_reports.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "configured_recipients": REPORT_RECIPIENTS,
            "resend_configured": bool(RESEND_API_KEY) and not RESEND_API_KEY.startswith("your-"),
            "preview": build_report_data(conn),
        }, conn=conn)

    @router.post("/reports/send")
    async def owner_reports_send(request: Request, recipients: str = Form(""), conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        if not RESEND_API_KEY or RESEND_API_KEY.startswith("your-"):
            flash(request, "error", "Resend API key not configured — set RESEND_API_KEY in .env and restart.")
            return RedirectResponse(f"{prefix}/reports", status_code=302)

        to_list = [e.strip() for e in recipients.split(",") if e.strip()]
        if not to_list:
            to_list = REPORT_RECIPIENTS
        if not to_list:
            flash(request, "error", "No recipients. Enter at least one email or set REPORT_RECIPIENTS in .env.")
            return RedirectResponse(f"{prefix}/reports", status_code=302)

        try:
            data = build_report_data(conn)
            pdf = render_report_pdf(data)
            send_report_pdf(pdf, to_list, data["month_label"], RESEND_API_KEY)
            flash(request, "success",
                  f"Report for {data['month_label']} sent to {', '.join(to_list)} ({len(pdf):,} bytes).")
        except Exception as e:
            flash(request, "error", f"Failed to send report: {e}")
        return RedirectResponse(f"{prefix}/reports", status_code=302)

    # ─── QuickBooks Online (DoorFlex invoice → stock automation) ───────────

    @router.get("/qbo")
    async def owner_qbo(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        realm_id, info = qbo_oauth.get_connected_realm(conn)
        return render_template(request, "owner_qbo.html", {
            "admin_path": prefix,
            "owner_authenticated": True,
            "qbo_enabled": QBO_ENABLED,
            "realm_id": realm_id,
            "info": info,
        }, conn=conn)

    @router.get("/qbo/connect")
    async def owner_qbo_connect(request: Request):
        require_owner(request)
        if not QBO_ENABLED:
            flash(request, "error", "QBO env vars not set (QBO_CLIENT_ID, QBO_CLIENT_SECRET, QBO_WEBHOOK_VERIFIER_TOKEN).")
            return RedirectResponse(f"{prefix}/qbo", status_code=302)
        state = qbo_oauth.new_state()
        # State survives the redirect round-trip through the owner-session
        # cookie; the callback compares against it to defeat CSRF.
        request.session["qbo_oauth_state"] = state
        return RedirectResponse(qbo_oauth.build_authorize_url(state), status_code=302)

    @router.get("/qbo/callback")
    async def owner_qbo_callback(
        request: Request,
        code: str = "",
        state: str = "",
        realmId: str = "",
        error: str = "",
        conn: sqlite3.Connection = Depends(_get_db),
    ):
        require_owner(request)
        expected = request.session.pop("qbo_oauth_state", "")
        if error:
            flash(request, "error", f"Intuit returned an error: {error}")
            return RedirectResponse(f"{prefix}/qbo", status_code=302)
        if not code or not realmId or not state or state != expected:
            flash(request, "error", "OAuth callback rejected (bad or missing state/code).")
            return RedirectResponse(f"{prefix}/qbo", status_code=302)
        try:
            tokens = qbo_oauth.exchange_code_for_tokens(code)
            qbo_oauth.store_tokens(conn, realmId, tokens)
        except qbo_oauth.QboOAuthError as e:
            flash(request, "error", f"Failed to complete QBO connect: {e}")
            return RedirectResponse(f"{prefix}/qbo", status_code=302)
        flash(request, "success", f"Connected to QuickBooks Online (realm {realmId}).")
        return RedirectResponse(f"{prefix}/qbo", status_code=302)

    @router.post("/qbo/disconnect")
    async def owner_qbo_disconnect(request: Request, conn: sqlite3.Connection = Depends(_get_db)):
        require_owner(request)
        qbo_oauth.clear_tokens(conn)
        flash(request, "success", "Disconnected from QuickBooks Online. No more invoice webhooks will be processed until you reconnect.")
        return RedirectResponse(f"{prefix}/qbo", status_code=302)

    return router
