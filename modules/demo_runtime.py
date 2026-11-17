"""DEMO_MODE runtime: (re)building the demo database and guarding paid calls.

The demo DB is built in a scratch file and then copied into the live DB with
SQLite's online backup API. Unlike swapping files, that is safe while the app
is serving requests (it takes the DB lock page by page, and WAL readers see a
consistent snapshot), so a nightly reset never needs a restart.
"""
import logging
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from config import DEMO_PWA_TOKEN, DEMO_RESET_HOUR, DEMO_VISION_DAILY_LIMIT
from database import SCHEMA, _migrate, _seed_distributors, get_db_path

logger = logging.getLogger("doorflex.demo")

# Folders under the data dir that hold files the DB points to by name.
ASSET_DIRS = ("job_captures", "winston_reports")

_lock = threading.Lock()
_vision = {"day": None, "count": 0}


def rebuild(today=None):
    """Regenerate the demo dataset and install it as the live DB + assets."""
    from modules import demo_seed

    with _lock:
        started = time.time()
        live_db = Path(get_db_path())
        data_dir = live_db.parent
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="demo-build-", dir=data_dir) as tmp:
            tmp = Path(tmp)
            build = sqlite3.connect(tmp / "demo.db")
            build.row_factory = sqlite3.Row
            build.execute("PRAGMA foreign_keys=ON")
            build.execute("PRAGMA synchronous=OFF")
            build.executescript(SCHEMA)
            _migrate(build)
            _seed_distributors(build)
            build.commit()
            summary = demo_seed.build_demo(build, today=today or date.today(), assets_dir=tmp,
                                           pwa_token=DEMO_PWA_TOKEN)

            live = sqlite3.connect(live_db, timeout=30)
            try:
                build.backup(live)
            finally:
                live.close()
                build.close()

            for name in ASSET_DIRS:
                target = data_dir / name
                shutil.rmtree(target, ignore_errors=True)
                if (tmp / name).is_dir():
                    shutil.copytree(tmp / name, target)
        logger.info(f"demo dataset rebuilt in {time.time() - started:.1f}s: {summary}")
        return summary


def nightly_reset_loop():
    while True:
        now = datetime.now()
        target = now.replace(hour=DEMO_RESET_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep(max(60.0, (target - now).total_seconds()))
        try:
            rebuild()
        except Exception:
            logger.exception("nightly demo reset failed")


def consume_vision_call():
    """Raise before a paid vision call when the demo's daily budget is used
    up (or there is no key). Message is shown to the visitor as-is."""
    from config import OPENAI_API_KEY

    if not OPENAI_API_KEY:
        raise RuntimeError("AI vision is turned off in this demo (no API key configured).")
    with _lock:
        today = date.today()
        if _vision["day"] != today:
            _vision.update(day=today, count=0)
        if _vision["count"] >= DEMO_VISION_DAILY_LIMIT:
            raise RuntimeError("The demo's daily AI vision budget is used up. It resets tomorrow.")
        _vision["count"] += 1


def sample_invoice_pdf():
    """Itemized invoice for one of the demo techs, built so the real parser
    deducts it cleanly - plus one line that lands in Unmatched on purpose."""
    from modules import demo_assets

    items = [("Torsion Spring .218 x 2 x 32", 2, 95.00), ("Rollers", 10, 19.50), ("Spring retention", 1, 45.00),
             ("Lube", 1, 15.00), ("Hinge #4 heavy duty", 2, 24.00)]
    return demo_assets.sample_invoice_pdf("Kyle Bennett", 1999, items)


def sample_winston_report_jpg():
    """A weekly payment report photo for the CURRENT week of Andre's captured
    jobs, so uploading it reconciles against real demo captures."""
    from modules import demo_assets
    from database import get_db

    conn = get_db()
    try:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        rows = conn.execute(
            """SELECT jc.wo_number, jc.amount_charged, substr(jc.captured_at, 1, 10) AS d
               FROM job_captures jc JOIN techs t ON t.id = jc.tech_id
               WHERE t.deduction_source='pwa' AND jc.payment_method != 'not_collected'
                 AND substr(jc.captured_at, 1, 10) >= ? ORDER BY jc.captured_at""",
            (monday.isoformat(),),
        ).fetchall()
        if len(rows) < 3:  # early in the week: use last week instead
            monday -= timedelta(days=7)
            rows = conn.execute(
                """SELECT jc.wo_number, jc.amount_charged, substr(jc.captured_at, 1, 10) AS d
                   FROM job_captures jc JOIN techs t ON t.id = jc.tech_id
                   WHERE t.deduction_source='pwa' AND jc.payment_method != 'not_collected'
                     AND substr(jc.captured_at, 1, 10) BETWEEN ? AND ? ORDER BY jc.captured_at""",
                (monday.isoformat(), (monday + timedelta(days=5)).isoformat()),
            ).fetchall()
    finally:
        conn.close()
    jobs = []
    for r in rows:
        payout = round(r["amount_charged"] * 0.6, 2)
        jobs.append({"date": r["d"], "wo_number": r["wo_number"], "total_sales": r["amount_charged"],
                     "payout": payout, "commission": round(payout * 0.5, 2)})
    parts = 42.0 if jobs else 0.0
    parsed = {
        "tech_name": "Andre Wallace", "period_start": monday.isoformat(),
        "period_end": (monday + timedelta(days=5)).isoformat(), "jobs": jobs,
        "subtotal_sales": round(sum(j["total_sales"] for j in jobs), 2),
        "subtotal_payout": round(sum(j["payout"] for j in jobs), 2),
        "parts_deduction": parts,
        "total_commission": round(sum(j["commission"] for j in jobs) - parts, 2),
    }
    return demo_assets.winston_report_bytes(parsed)
