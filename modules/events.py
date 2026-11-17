"""Application event log + dual-timezone formatting.

Replaces the invisible stdout-only logging with a structured, queryable table
that the owner panel renders with both Phoenix and Mexico City timestamps.

The module is deliberately small. `log_event` is safe to call from background
threads (it opens its own short-lived connection when no one is passed in) and
imports don't cause circular dependency cycles because it doesn't import any
other project module.
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from database import get_db

logger = logging.getLogger("doorflex.events")

_VALID_LEVELS = {"info", "warn", "error"}


def _now_phoenix():
    """Server-local 'now' (matches SQLite datetime('now','localtime'))."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log_event(conn, level, scope, event_type, message, detail=None, ref=""):
    """Insert one event. `conn` may be None from background threads — a short-
    lived connection is opened and committed in that case. Returns the row id."""
    if level not in _VALID_LEVELS:
        level = "info"
    detail_json = ""
    if detail is not None and detail != "":
        try:
            detail_json = json.dumps(detail, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            detail_json = json.dumps({"repr": repr(detail)})

    owned = conn is None
    if owned:
        conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO app_events (ts, level, scope, event_type, message, detail, ref)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (_now_phoenix(), level, scope or "", event_type or "", message or "",
             detail_json, ref or ""),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        logger.error("log_event failed (type=%s): %s", event_type, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass


def list_events(conn, scope=None, level=None, event_type=None,
                date_from=None, date_to=None, search=None,
                limit=100, offset=0):
    """Filtered event listing (newest first)."""
    conditions = []
    params = []
    if scope:
        conditions.append("scope=?")
        params.append(scope)
    if level:
        conditions.append("level=?")
        params.append(level)
    if event_type:
        conditions.append("event_type=?")
        params.append(event_type)
    if date_from:
        conditions.append("ts >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("ts <= ?")
        params.append(date_to)
    if search:
        conditions.append("(message LIKE ? OR ref LIKE ? OR event_type LIKE ?)")
        s = f"%{search}%"
        params.extend([s, s, s])
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM app_events {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    counts = conn.execute(
        f"SELECT COUNT(*) FROM app_events {where}", params,
    ).fetchone()[0]
    return rows, counts


def list_requests(conn, path_prefix=None, status=None, method=None,
                  date_from=None, date_to=None, limit=100, offset=0):
    """Filtered request log listing (newest first)."""
    conditions = []
    params = []
    if path_prefix:
        conditions.append("path LIKE ?")
        params.append(f"{path_prefix}%")
    if status is not None:
        conditions.append("status=?")
        params.append(status)
    if method:
        conditions.append("method=?")
        params.append(method)
    if date_from:
        conditions.append("ts >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("ts <= ?")
        params.append(date_to)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM request_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    counts = conn.execute(
        f"SELECT COUNT(*) FROM request_log {where}", params,
    ).fetchone()[0]
    return rows, counts


def log_request(conn, method, path, status, duration_ms, ip, tech="", detail=""):
    """Persist one request row. Always safe (catches and logs internally).
    Returns the new row id, or None on failure."""
    try:
        cur = conn.execute(
            """INSERT INTO request_log
               (ts, method, path, status, duration_ms, ip, tech, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (_now_phoenix(), method or "", path or "", int(status or 0),
             int(duration_ms or 0), ip or "", tech or "", detail or ""),
        )
        return cur.lastrowid
    except Exception as e:
        logger.error("log_request failed (%s %s): %s", method, path, e)
        return None


def counts_24h(conn):
    """Compact counts for the observe dashboard."""
    rows = conn.execute("""
        SELECT
          (SELECT COUNT(*) FROM app_events WHERE level='error' AND ts >= datetime('now','-1 day')) AS errors_24h,
          (SELECT COUNT(*) FROM app_events WHERE level='warn'  AND ts >= datetime('now','-1 day')) AS warns_24h,
          (SELECT COUNT(*) FROM app_events WHERE ts >= datetime('now','-1 day')) AS events_24h,
          (SELECT COUNT(*) FROM request_log WHERE ts >= datetime('now','-1 day')) AS requests_24h,
          (SELECT COUNT(*) FROM request_log WHERE status >= 400 AND ts >= datetime('now','-1 day')) AS request_errors_24h,
          (SELECT COUNT(*) FROM email_log WHERE status='sent' AND created_at >= datetime('now','-7 day')) AS emails_sent_7d,
          (SELECT COUNT(*) FROM email_log WHERE status='failed' AND created_at >= datetime('now','-7 day')) AS emails_failed_7d,
          (SELECT COUNT(*) FROM app_events WHERE scope='ingest' AND ts >= datetime('now','-1 day')) AS ingest_24h,
          (SELECT COUNT(*) FROM app_events WHERE scope='pwa' AND ts >= datetime('now','-1 day')) AS pwa_24h,
          (SELECT COUNT(*) FROM stock_alerts WHERE resolved=0) AS stock_alerts_open
    """).fetchone()
    return dict(rows)


def requests_by_day(conn, days=7):
    """Per-day request counts (timezone-local)."""
    return conn.execute("""
        SELECT substr(ts,1,10) AS day, COUNT(*) AS n
        FROM request_log
        WHERE ts >= datetime('now', ?)
        GROUP BY day ORDER BY day
    """, (f"-{days} days",)).fetchall()


_UNIFIED_SOURCES = ("event", "http", "email", "stock")
"""Source keys used by `unified_errors`. 'stock' rows are state (unresolved
alerts) so they are never time-filtered — they stay visible until resolved."""


def unified_errors(conn, sources=None, levels=None, hours=24, search=None,
                   limit=100, offset=0):
    """Consolidated error feed across every source, newest first.

    Merges app_events (error/warn), request_log (4xx/5xx), email_log (failed)
    and unresolved stock_alerts into one normalized list. Each row:
    ts, source, level, tag, kind, title, detail (dict), ref, sid.
    Returns (rows, total). `hours` only applies to event/http/email; stock
    alerts are always included (they persist until resolved)."""
    source_set = set(sources or _UNIFIED_SOURCES)
    for s in source_set:
        if s not in _UNIFIED_SOURCES:
            source_set.discard(s)
    level_set = set(levels or ("error", "warn"))
    if not level_set:
        level_set = {"error", "warn"}
    since = f"-{max(int(hours or 1), 1)} hours"
    rows = []

    if "event" in source_set:
        placeholders = ",".join("?" for _ in level_set)
        ev = conn.execute(
            f"""SELECT id, ts, level, scope, event_type, message, detail, ref
                FROM app_events
                WHERE level IN ({placeholders}) AND ts >= datetime('now', ?)
                ORDER BY id DESC LIMIT ?""",
            tuple(sorted(level_set)) + (since, 5000),
        ).fetchall()
        for r in ev:
            try:
                detail = json.loads(r["detail"]) if r["detail"] else {}
            except ValueError:
                detail = {"raw": r["detail"]}
            rows.append({
                "ts": r["ts"], "source": "event", "level": r["level"],
                "tag": r["scope"] or "app", "kind": r["event_type"],
                "title": r["message"], "detail": detail,
                "ref": r["ref"], "sid": r["id"],
            })

    if "http" in source_set:
        http_levels = [l for l in ("error", "warn") if l in level_set]
        if http_levels:
            if "error" in http_levels and "warn" in http_levels:
                status_clause = "status >= 400"
            elif "error" in http_levels:
                status_clause = "status >= 500"
            else:
                status_clause = "status >= 400 AND status < 500"
            req = conn.execute(
                f"""SELECT id, ts, method, path, status, duration_ms, ip, tech, detail
                    FROM request_log
                    WHERE {status_clause} AND ts >= datetime('now', ?)
                    ORDER BY id DESC LIMIT ?""",
                (since, 5000),
            ).fetchall()
            for r in req:
                rows.append({
                    "ts": r["ts"], "source": "http",
                    "level": "error" if r["status"] >= 500 else "warn",
                    "tag": f"HTTP {r['status']}",
                    "kind": f"{r['method']} {r['path']}",
                    "title": f"{r['method']} {r['path']} → {r['status']}",
                    "detail": {
                        "status": r["status"], "method": r["method"], "path": r["path"],
                        "duration_ms": r["duration_ms"], "ip": r["ip"], "tech": r["tech"],
                    },
                    "ref": f"req#{r['id']}", "sid": r["id"],
                })

    if "email" in source_set and "error" in level_set:
        em = conn.execute(
            """SELECT id, email_type, subject, to_address, part_id, attempts,
                      response, created_at
               FROM email_log
               WHERE status='failed' AND created_at >= datetime('now', ?)
               ORDER BY id DESC LIMIT ?""",
            (since, 5000),
        ).fetchall()
        for r in em:
            rows.append({
                "ts": r["created_at"], "source": "email", "level": "error",
                "tag": r["email_type"] or "email",
                "kind": f"email.{r['email_type'] or 'unknown'}",
                "title": (f"Email {r['email_type'] or '(unknown)'} failed → "
                          f"{r['to_address'] or '?'}"),
                "detail": {
                    "email_type": r["email_type"], "subject": r["subject"],
                    "to": r["to_address"], "part_id": r["part_id"],
                    "attempts": r["attempts"], "response": r["response"],
                },
                "ref": "", "sid": r["id"],
            })

    if "stock" in source_set and "warn" in level_set:
        st = conn.execute(
            """SELECT id, part_id, part_name, truck_id, warehouse_id,
                      requested_qty, available_qty, invoice_id, created_at
               FROM stock_alerts
               WHERE resolved=0
               ORDER BY id DESC LIMIT ?""",
            (5000,),
        ).fetchall()
        for r in st:
            rows.append({
                "ts": r["created_at"], "source": "stock", "level": "warn",
                "tag": "stock", "kind": "stock.shortfall",
                "title": (f"Shortfall: {r['part_name'] or r['part_id']} "
                          f"(need {r['requested_qty']}, have {r['available_qty']})"),
                "detail": {
                    "part_id": r["part_id"], "part_name": r["part_name"],
                    "truck_id": r["truck_id"], "warehouse_id": r["warehouse_id"],
                    "requested_qty": r["requested_qty"],
                    "available_qty": r["available_qty"],
                    "invoice_id": r["invoice_id"],
                },
                "ref": r["invoice_id"], "sid": r["id"],
            })

    if search:
        needle = search.lower()
        rows = [
            r for r in rows
            if needle in json.dumps(r, ensure_ascii=False, default=str).lower()
        ]

    rows.sort(key=lambda r: (r["ts"] or ""), reverse=True)
    total = len(rows)
    return rows[offset:offset + max(limit, 1)], total


def errors_by_source(conn, hours=24):
    """Per-source error counts for the dashboard cards (last `hours`, except
    stock which counts all unresolved alerts)."""
    since = f"-{max(int(hours or 1), 1)} hours"
    ev_err = conn.execute(
        "SELECT COUNT(*) FROM app_events WHERE level='error' AND ts >= datetime('now', ?)",
        (since,)).fetchone()[0]
    ev_warn = conn.execute(
        "SELECT COUNT(*) FROM app_events WHERE level='warn' AND ts >= datetime('now', ?)",
        (since,)).fetchone()[0]
    http = conn.execute(
        "SELECT COUNT(*) FROM request_log WHERE status >= 400 AND ts >= datetime('now', ?)",
        (since,)).fetchone()[0]
    email = conn.execute(
        "SELECT COUNT(*) FROM email_log WHERE status='failed' AND created_at >= datetime('now', ?)",
        (since,)).fetchone()[0]
    stock = conn.execute(
        "SELECT COUNT(*) FROM stock_alerts WHERE resolved=0").fetchone()[0]
    return {
        "event_errors": ev_err, "event_warns": ev_warn,
        "http": http, "email": email, "stock": stock,
    }


def prune(conn, events_days=30, requests_days=14):
    """Drop old events / requests. Idempotent."""
    e = conn.execute(
        "DELETE FROM app_events WHERE ts < datetime('now', ?)",
        (f"-{events_days} days",),
    ).rowcount
    r = conn.execute(
        "DELETE FROM request_log WHERE ts < datetime('now', ?)",
        (f"-{requests_days} days",),
    ).rowcount
    conn.commit()
    return {"events_pruned": e, "requests_pruned": r}


# ─── Dual timezone formatting ─────────────────────────────────────────────────
# Server DB timestamps are stored in America/Phoenix (the container TZ is forced
# to AZ via database.py at startup). CDMX is always AZ + 1h (no DST for either),
# but we still resolve through zoneinfo to be safe + render both columns.

_PHX = ZoneInfo("America/Phoenix")
_CMX = ZoneInfo("America/Mexico_City")


def _parse_naive(ts):
    """Parse 'YYYY-MM-DD HH:MM:SS' (or 'YYYY-MM-DD HH:MM:SS.ffffff') as Phoenix."""
    if not ts:
        return None
    s = str(ts)
    if len(s) >= 19 and s[4] == "-" and s[10] == " ":
        s = s[:19]
    else:
        s = s[:19]
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_PHX)
    except ValueError:
        return None


def fmt_dual(ts):
    """Full: 'Aug 15, 2026 · 08:00:00 AZ / 09:00:00 CDMX'."""
    dt = _parse_naive(ts)
    if not dt:
        return "—"
    az = dt.strftime("%b %-d, %Y · %H:%M:%S")
    cmx = dt.astimezone(_CMX).strftime("%H:%M:%S")
    return f"{az} AZ / {cmx} CDMX"


def fmt_dual_short(ts):
    """Compact: '08:00:00 AZ / 09:00:00 CDMX' (date in separate column)."""
    dt = _parse_naive(ts)
    if not dt:
        return "—"
    az = dt.strftime("%H:%M:%S")
    cmx = dt.astimezone(_CMX).strftime("%H:%M:%S")
    return f"{az} AZ / {cmx} CDMX"


def fmt_dual_date(ts):
    """Date only, AZ: 'Aug 15, 2026'."""
    dt = _parse_naive(ts)
    if not dt:
        return "—"
    return dt.strftime("%b %-d, %Y")


def fmt_dual_az_time(ts):
    """Time only, AZ: '08:00:00'."""
    dt = _parse_naive(ts)
    if not dt:
        return "—"
    return dt.strftime("%H:%M:%S")


def fmt_dual_cmx_time(ts):
    """Time only, Mexico City: '09:00:00'."""
    dt = _parse_naive(ts)
    if not dt:
        return "—"
    return dt.astimezone(_CMX).strftime("%H:%M:%S")
