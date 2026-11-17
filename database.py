import shutil
import sqlite3
import os
import time
import zipfile
from datetime import datetime
from pathlib import Path

os.environ.setdefault("TZ", "America/Phoenix")
try:
    time.tzset()
except AttributeError:
    pass

DEFAULT_DB_PATH = Path(__file__).parent / "data" / "inventory.db"
BACKUP_RETENTION = int(os.environ.get("BACKUP_RETENTION", "13"))
BACKUP_GLOB_ZIP = "doorflex-backup-*.zip"
BACKUP_GLOB_LEGACY_DB = "inventory.backup-*.db"


def get_backup_dir():
    """Backups live next to the DB (inside the persistent volume) unless
    BACKUP_PATH is set. Default resolves to <db parent>/backups."""
    override = os.environ.get("BACKUP_PATH", "").strip()
    if override:
        return Path(override)
    return Path(get_db_path()).parent / "backups"

SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouses (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS trucks (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    warehouse_id TEXT REFERENCES warehouses(id),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS techs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    truck_id TEXT NOT NULL REFERENCES trucks(id),
    warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
    active INTEGER NOT NULL DEFAULT 1,
    deduction_source TEXT NOT NULL DEFAULT 'email',
    pwa_token_hash TEXT NOT NULL DEFAULT '',
    pwa_token TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS parts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    unit TEXT NOT NULL DEFAULT 'piece',
    category TEXT NOT NULL DEFAULT '',
    subcategory TEXT NOT NULL DEFAULT '',
    hand TEXT NOT NULL DEFAULT '',
    length_size TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    supplier TEXT NOT NULL DEFAULT '',
    cost REAL NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    truck_id TEXT NOT NULL REFERENCES trucks(id),
    part_id TEXT NOT NULL REFERENCES parts(id),
    quantity INTEGER NOT NULL DEFAULT 0,
    min_threshold INTEGER NOT NULL DEFAULT 5,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(truck_id, part_id)
);

CREATE TABLE IF NOT EXISTS warehouse_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
    part_id TEXT NOT NULL REFERENCES parts(id),
    quantity INTEGER NOT NULL DEFAULT 0,
    min_threshold INTEGER NOT NULL DEFAULT 10,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(warehouse_id, part_id)
);

CREATE TABLE IF NOT EXISTS packages (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parts_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS distributors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS product_distributors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL REFERENCES parts(id),
    distributor_id TEXT NOT NULL REFERENCES distributors(id),
    distributor_sku TEXT NOT NULL DEFAULT '',
    distributor_part_name TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE(product_id, distributor_id)
);

CREATE TABLE IF NOT EXISTS email_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_type TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    to_address TEXT NOT NULL DEFAULT '',
    part_id TEXT NOT NULL DEFAULT '',
    distributor_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    response TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS reorder_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id TEXT NOT NULL REFERENCES parts(id),
    distributor_id TEXT NOT NULL DEFAULT '',
    warehouse_id TEXT NOT NULL DEFAULT '',
    qty INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    email_log_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    tech_name TEXT NOT NULL,
    truck_id TEXT NOT NULL,
    warehouse_id TEXT NOT NULL,
    part_id TEXT NOT NULL,
    quantity_change INTEGER NOT NULL,
    stock_before INTEGER NOT NULL,
    stock_after INTEGER NOT NULL,
    invoice_id TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    raw_text TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'email',
    format TEXT NOT NULL DEFAULT '',
    invoice_name TEXT NOT NULL DEFAULT '',
    wo_number TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS unmatched_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL DEFAULT '',
    tech_name TEXT NOT NULL DEFAULT '',
    raw_name TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    mapped_to_part_id TEXT REFERENCES parts(id),
    truck_id TEXT NOT NULL DEFAULT '',
    warehouse_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    kind TEXT NOT NULL DEFAULT 'part',
    mapped_to_package_id TEXT REFERENCES packages(id),
    invoice_name TEXT NOT NULL DEFAULT '',
    pending_data TEXT
);

CREATE TABLE IF NOT EXISTS stock_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL DEFAULT '',
    part_id TEXT NOT NULL DEFAULT '',
    part_name TEXT NOT NULL DEFAULT '',
    truck_id TEXT NOT NULL DEFAULT '',
    warehouse_id TEXT NOT NULL DEFAULT '',
    requested_qty INTEGER NOT NULL DEFAULT 0,
    available_qty INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_log_timestamp ON log(timestamp);
CREATE INDEX IF NOT EXISTS idx_log_invoice_id ON log(invoice_id);
CREATE INDEX IF NOT EXISTS idx_log_truck_id ON log(truck_id);
CREATE INDEX IF NOT EXISTS idx_log_part_id ON log(part_id);
CREATE INDEX IF NOT EXISTS idx_log_source ON log(source);
-- idx_log_wo_number is created in _migrate() (after the additive ALTER that
-- adds the column to pre-existing databases) — putting it here would fail on
-- an old prod DB where CREATE TABLE IF NOT EXISTS log is a no-op and the
-- column doesn't exist yet, aborting the whole executescript before _migrate
-- gets a chance to run. (Reproduced against a prod deploy 2026-08-21.)
CREATE INDEX IF NOT EXISTS idx_unmatched_mapped ON unmatched_parts(mapped_to_part_id);
CREATE INDEX IF NOT EXISTS idx_unmatched_kind_techname ON unmatched_parts(kind, tech_name);
CREATE INDEX IF NOT EXISTS idx_stock_alerts_resolved ON stock_alerts(resolved, id);
CREATE TABLE IF NOT EXISTS processed_invoices (
    invoice_id TEXT PRIMARY KEY,
    email_id TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS deduction_batches (
    batch_id TEXT PRIMARY KEY,
    tech_id INTEGER NOT NULL,
    items_hash TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS door_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_wo TEXT NOT NULL UNIQUE,
    tech_id INTEGER NOT NULL REFERENCES techs(id),
    tech_name TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    model_label TEXT NOT NULL DEFAULT '',
    series_design TEXT NOT NULL DEFAULT '',
    insulation TEXT NOT NULL DEFAULT '',
    item_number TEXT NOT NULL DEFAULT '',
    width TEXT NOT NULL DEFAULT '',
    height TEXT NOT NULL DEFAULT '',
    windows INTEGER NOT NULL DEFAULT 0,
    window_style TEXT NOT NULL DEFAULT '',
    glass_type TEXT NOT NULL DEFAULT '',
    color_base TEXT NOT NULL DEFAULT '',
    color_overlay TEXT NOT NULL DEFAULT '',
    construction TEXT NOT NULL DEFAULT '',
    insulated INTEGER NOT NULL DEFAULT 0,
    customer_address TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    needs_distributor_quote INTEGER NOT NULL DEFAULT 0,
    generic_path INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'captured',
    captured_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_door_orders_status ON door_orders(status, captured_at);

CREATE TABLE IF NOT EXISTS app_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    level TEXT NOT NULL DEFAULT 'info',
    scope TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ref TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_app_events_ts ON app_events(ts);
CREATE INDEX IF NOT EXISTS idx_app_events_type ON app_events(event_type);
CREATE INDEX IF NOT EXISTS idx_app_events_scope ON app_events(scope, ts);
CREATE INDEX IF NOT EXISTS idx_app_events_level ON app_events(level, ts);

CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    method TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    status INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    ip TEXT NOT NULL DEFAULT '',
    tech TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts);
CREATE INDEX IF NOT EXISTS idx_request_log_path ON request_log(path, ts);
CREATE INDEX IF NOT EXISTS idx_request_log_status ON request_log(status, ts);

CREATE INDEX IF NOT EXISTS idx_stock_truck_part ON stock(truck_id, part_id);
CREATE INDEX IF NOT EXISTS idx_whstock_warehouse_part ON warehouse_stock(warehouse_id, part_id);
CREATE INDEX IF NOT EXISTS idx_product_distributors_product ON product_distributors(product_id);
CREATE INDEX IF NOT EXISTS idx_product_distributors_active ON product_distributors(distributor_id, is_active);
CREATE INDEX IF NOT EXISTS idx_reorder_status ON reorder_requests(status, warehouse_id);
CREATE INDEX IF NOT EXISTS idx_email_log_type ON email_log(email_type, status);

-- Jobs expected today, either entered manually or (later) ingested from a
-- Dispatch.me scraper. Independent system-of-record source for a job list,
-- separate from door_orders (which is a parts-ordering workflow).
CREATE TABLE IF NOT EXISTS expected_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_number TEXT NOT NULL UNIQUE,
    tech_id INTEGER REFERENCES techs(id),
    address TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    job_type TEXT NOT NULL DEFAULT '',
    warranty_company TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    removed_at TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_expected_jobs_wo ON expected_jobs(wo_number);
CREATE INDEX IF NOT EXISTS idx_expected_jobs_tech_status ON expected_jobs(tech_id, status);

-- What a tech actually captured in the PWA for a job — the independent
-- evidence record (closing notes + payment info + receipt photo).
CREATE TABLE IF NOT EXISTS job_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    wo_number TEXT NOT NULL,
    tech_id INTEGER NOT NULL REFERENCES techs(id),
    amount_charged REAL,
    payment_method TEXT NOT NULL DEFAULT '',
    check_number TEXT NOT NULL DEFAULT '',
    not_collected_reason TEXT NOT NULL DEFAULT '',
    closing_notes TEXT NOT NULL DEFAULT '',
    receipt_photo_path TEXT NOT NULL DEFAULT '',
    captured_at TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_job_captures_wo ON job_captures(wo_number);
CREATE INDEX IF NOT EXISTS idx_job_captures_tech ON job_captures(tech_id);

-- Jobs Winston removed from Dispatch.me (or otherwise disputed) with capture
-- evidence attached, for Maria to track through resolution.
CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_number TEXT NOT NULL,
    capture_id INTEGER REFERENCES job_captures(id),
    status TEXT NOT NULL DEFAULT 'new',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status);

-- One row per daily reconciliation run (captured vs not-collected vs
-- uncaptured vs removed-by-dispatch), for the 5:30pm email + audit trail.
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    matched_count INTEGER NOT NULL DEFAULT 0,
    not_collected_count INTEGER NOT NULL DEFAULT 0,
    uncaptured_count INTEGER NOT NULL DEFAULT 0,
    removed_count INTEGER NOT NULL DEFAULT 0,
    report_sent_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Web Push subscriptions for the iOS/Android end-of-day sync reminder.
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tech_id INTEGER NOT NULL REFERENCES techs(id),
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL DEFAULT '',
    auth TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_push_subscriptions_tech ON push_subscriptions(tech_id);

-- Winston's weekly payment report (parsed from a photo via vision AI). A row
-- starts as 'draft' right after upload/parse and only becomes 'confirmed'
-- (and gets its winston_report_jobs rows) once a human has reviewed and
-- possibly corrected the parse — see modules/winston_reports.py.
CREATE TABLE IF NOT EXISTS winston_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tech_name TEXT NOT NULL DEFAULT '',
    period_start TEXT NOT NULL DEFAULT '',
    period_end TEXT NOT NULL DEFAULT '',
    raw_image_path TEXT NOT NULL DEFAULT '',
    parsed_json TEXT NOT NULL DEFAULT '',
    subtotal_sales REAL,
    subtotal_payout REAL,
    parts_deduction REAL,
    total_commission REAL,
    status TEXT NOT NULL DEFAULT 'draft',
    received_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    processed_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_winston_reports_status ON winston_reports(status);

-- Individual job lines from a confirmed Winston report. Only ever written by
-- confirm_draft() — never populated directly from the raw AI parse.
CREATE TABLE IF NOT EXISTS winston_report_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES winston_reports(id),
    report_date TEXT NOT NULL DEFAULT '',
    wo_number TEXT NOT NULL,
    total_sales REAL,
    payout REAL,
    commission REAL,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_winston_report_jobs_wo ON winston_report_jobs(wo_number);
CREATE INDEX IF NOT EXISTS idx_winston_report_jobs_report ON winston_report_jobs(report_id);

-- QuickBooks Online OAuth tokens. One row per connected QBO company
-- (Nick's DoorFlex account = one row). Written on OAuth callback, read on
-- every webhook (with refresh-on-expiry). realm_id is Intuit's company id.
CREATE TABLE IF NOT EXISTS qbo_tokens (
    realm_id TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    last_webhook_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def get_db_path():
    return os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH))


def get_db():
    db_path = get_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _backup_data_subdirs():
    """The on-disk user data that lives outside the DB but that the DB only
    stores *paths* to (job_captures.receipt_photo_path,
    winston_reports.raw_image_path, and anything similar added later) —
    never the file bytes themselves. A DB-only backup silently loses these;
    a full backup must capture them too.

    Auto-discovers every subdirectory of the data folder (excluding the
    backups folder itself) rather than a hardcoded list, so a future feature
    that saves files under a new data/<name>/ folder is automatically
    included in full backups with no code change here."""
    base = Path(get_db_path()).parent
    backup_dir = get_backup_dir().resolve()
    if not base.is_dir():
        return {}
    return {
        d.name: d for d in base.iterdir()
        if d.is_dir() and d.resolve() != backup_dir
    }


def backup_db():
    """Snapshot the DB *and* the on-disk photo directories into one archive
    at <backup_dir>/doorflex-backup-<ts>.zip — a full backup, not just the
    database. Prunes old backups (this format and any legacy DB-only .db
    backups still on disk from before this existed) keeping the newest
    BACKUP_RETENTION combined. Returns the backup path, or None if there is
    no live DB yet."""
    db_path = Path(get_db_path())
    if not db_path.exists():
        return None
    backup_dir = get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    conn = None
    try:
        conn = get_db()
        conn.execute("PRAGMA wal_checkpoint(FULL)")
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    # Microsecond precision: two backups within the same second (the daily
    # cron landing right as someone clicks "Backup Now", a double-click on
    # that button) previously got the *identical* second-precision filename
    # and the second one silently overwrote the first.
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_dir / f"doorflex-backup-{ts}.zip"
    tmp = dest.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, "inventory.db")
            for arc_root, folder in _backup_data_subdirs().items():
                if not folder.is_dir():
                    continue
                for f in folder.rglob("*"):
                    if f.is_file():
                        zf.write(f, f"{arc_root}/{f.relative_to(folder)}")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    _prune_backups(backup_dir)
    return str(dest)


def _list_backup_files(backup_dir):
    return sorted(
        list(backup_dir.glob(BACKUP_GLOB_ZIP)) + list(backup_dir.glob(BACKUP_GLOB_LEGACY_DB)),
        key=lambda f: f.stat().st_mtime, reverse=True,
    )


def _prune_backups(backup_dir):
    """Keep only the newest BACKUP_RETENTION backups overall."""
    if BACKUP_RETENTION <= 0:
        return
    for old in _list_backup_files(backup_dir)[BACKUP_RETENTION:]:
        try:
            old.unlink()
        except OSError:
            pass


def list_backups():
    backup_dir = get_backup_dir()
    if not backup_dir.exists():
        return []
    return [
        {
            "name": f.name,
            "size": f.stat().st_size,
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "kind": "full" if f.suffix == ".zip" else "db_only",
        }
        for f in _list_backup_files(backup_dir)
    ]


def delete_backup(filename):
    """Delete one backup file by name. Returns True if it existed and was
    removed. Re-validates the filename itself (no path separators/
    traversal) even though callers should already be checking this."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return False
    path = get_backup_dir() / filename
    if not path.exists() or not path.is_file():
        return False
    path.unlink()
    return True


def restore_everything_from_zip(zip_path):
    """Extract a full backup zip into place: replaces the live DB and,
    only for each photo subdirectory the archive actually contains entries
    for, replaces it wholesale (not merged) — matching the existing DB
    restore's replace semantics, just extended to cover the files the DB
    only stores paths to. An archive missing a subdirectory (an older
    backup, or one made before this feature) leaves that live folder
    untouched rather than wiping it based on the archive's absence."""
    db_path = Path(get_db_path())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if "inventory.db" not in names:
            raise ValueError("Archive has no inventory.db entry")
        with zf.open("inventory.db") as src, open(db_path, "wb") as dst:
            shutil.copyfileobj(src, dst)

        for arc_root, folder in _backup_data_subdirs().items():
            entries = [n for n in names if n.startswith(f"{arc_root}/") and not n.endswith("/")]
            if not entries:
                continue  # archive doesn't cover this folder — leave live data alone
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir(parents=True, exist_ok=True)
            folder_resolved = folder.resolve()
            for name in entries:
                rel = name[len(arc_root) + 1:]
                target = (folder / rel).resolve()
                if not str(target).startswith(str(folder_resolved) + os.sep):
                    continue  # zip-slip guard: entry would escape the target folder
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)


def restore_latest_backup():
    """If the live DB is missing, restore from the newest backup — full
    zip (DB + photos) or, for anything created before this feature
    existed, a legacy DB-only .db file. Returns the restored path, or
    None if nothing to restore."""
    db_path = Path(get_db_path())
    if db_path.exists():
        return None
    backups = list_backups()
    if not backups:
        return None
    src = get_backup_dir() / backups[0]["name"]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix == ".zip":
        restore_everything_from_zip(src)
    else:
        shutil.copy2(src, db_path)
    return str(src)


def reset_database():
    """Wipe all user data and re-seed defaults. Creates a backup first so the
    reset is never irreversible."""
    backup_db()
    conn = get_db()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in ("log", "stock", "warehouse_stock", "stock_alerts",
                      "unmatched_parts", "processed_invoices", "packages",
                      "techs", "parts", "trucks", "warehouses",
                      "reorder_requests", "email_log", "product_distributors",
                      "distributors", "deduction_batches", "door_orders",
                      "expected_jobs", "job_captures", "disputes",
                      "reconciliation_runs", "push_subscriptions",
                      "winston_reports", "winston_report_jobs"):
            conn.execute(f"DELETE FROM [{table}]")
        conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
    _seed_data(conn)
    conn.close()


def ensure_db(seed=True):
    """Startup routine: restore from backup if the DB is missing, snapshot the
    current DB before any migration, then create/migrate/seed."""
    if not Path(get_db_path()).exists():
        restore_latest_backup()
    else:
        backup_db()
    init_db(seed=seed)


def init_db(seed=True):
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    if seed:
        _seed_data(conn)
    conn.close()


def _migrate(conn):
    """Additive migrations for databases created before newer columns existed."""
    part_cols = {row["name"] for row in conn.execute("PRAGMA table_info(parts)").fetchall()}
    if "amazon_sku" not in part_cols:
        conn.execute("ALTER TABLE parts ADD COLUMN amazon_sku TEXT NOT NULL DEFAULT ''")

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(unmatched_parts)").fetchall()}
    if "kind" not in cols:
        conn.execute("ALTER TABLE unmatched_parts ADD COLUMN kind TEXT NOT NULL DEFAULT 'part'")
    if "mapped_to_package_id" not in cols:
        conn.execute("ALTER TABLE unmatched_parts ADD COLUMN mapped_to_package_id TEXT REFERENCES packages(id)")
    if "invoice_name" not in cols:
        conn.execute("ALTER TABLE unmatched_parts ADD COLUMN invoice_name TEXT NOT NULL DEFAULT ''")
    if "pending_data" not in cols:
        conn.execute("ALTER TABLE unmatched_parts ADD COLUMN pending_data TEXT")
    log_cols = {row["name"] for row in conn.execute("PRAGMA table_info(log)").fetchall()}
    if "invoice_name" not in log_cols:
        conn.execute("ALTER TABLE log ADD COLUMN invoice_name TEXT NOT NULL DEFAULT ''")
    if "wo_number" not in log_cols:
        # Optional link from a PWA deduction to the job it was for — lets the
        # office see "what parts were used" alongside a job capture's charge,
        # matching how Dispatch invoices work (Nick's explanation, 2026-08-20).
        conn.execute("ALTER TABLE log ADD COLUMN wo_number TEXT NOT NULL DEFAULT ''")
    # Unconditional (idempotent) — SCHEMA can't create this index safely on
    # a pre-migration DB, so ownership lives here for BOTH fresh installs
    # and legacy ones. Runs after any ALTER above.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_log_wo_number ON log(wo_number)")

    # V3 PWA: techs get a deduction source + optional PWA token hash
    tech_cols = {row["name"] for row in conn.execute("PRAGMA table_info(techs)").fetchall()}
    if "deduction_source" not in tech_cols:
        conn.execute("ALTER TABLE techs ADD COLUMN deduction_source TEXT NOT NULL DEFAULT 'email'")
    if "pwa_token_hash" not in tech_cols:
        conn.execute("ALTER TABLE techs ADD COLUMN pwa_token_hash TEXT NOT NULL DEFAULT ''")
    if "pwa_token" not in tech_cols:
        conn.execute("ALTER TABLE techs ADD COLUMN pwa_token TEXT NOT NULL DEFAULT ''")

    # V1 cleanup: drop legacy stream/share/companies artifacts (pre-production data)
    conn.execute("DROP TABLE IF EXISTS companies")
    wh_cols = {row["name"] for row in conn.execute("PRAGMA table_info(warehouses)").fetchall()}
    if "cost_share" in wh_cols:
        conn.execute("ALTER TABLE warehouses DROP COLUMN cost_share")
    if "stream" in log_cols:
        conn.execute("ALTER TABLE log DROP COLUMN stream")


def _seed_distributors(conn):
    """Seed the placeholder distributors that are part of the canonical flow.
    Real vendors (Service Spring, etc.) are created from the dashboard. Safe
    to run on any DB — inserts only the ones that are missing."""
    existing = {r["id"] for r in conn.execute("SELECT id FROM distributors").fetchall()}
    placeholders = [
        ("amazon", "Amazon", ""),
    ]
    for did, name, email in placeholders:
        if did not in existing:
            conn.execute(
                "INSERT INTO distributors (id, name, email) VALUES (?, ?, ?)",
                (did, name, email),
            )
    conn.commit()


def _seed_data(conn):
    cursor = conn.cursor()
    _seed_distributors(conn)

    existing = cursor.execute("SELECT COUNT(*) FROM warehouses").fetchone()[0]
    if existing > 0:
        return

    cursor.executemany(
        "INSERT INTO warehouses (id, name) VALUES (?, ?)",
        [
            ("wh-warranty", "DoorFlex"),
            ("wh-sub", "Total Garage Solutions"),
        ],
    )

    cursor.executemany(
        "INSERT INTO trucks (id, name, warehouse_id) VALUES (?, ?, ?)",
        [
            ("T-01", "Truck 1", "wh-sub"),
            ("T-02", "Truck 2", "wh-warranty"),
        ],
    )

    conn.commit()


if __name__ == "__main__":
    init_db(seed=True)
    print("Database initialized with seed data.")
    conn = get_db()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    print("\nTables created:")
    for t in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM [{t[0]}]").fetchone()[0]
        print(f"  {t[0]}: {count} rows")
    conn.close()
