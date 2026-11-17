# PRD: DoorFlex Inventory System v3

> **Versión:** 3.0
> **Status:** Borrador para revisión de Fer
> **Prepárate para:** Implementación (post-firma del ICA)

---

## 1. Overview

Inventory tracking system for DoorFlex LLC that covers both business streams (Warranty and Subcontractor). Technicians forward invoice emails to a dedicated Gmail inbox. The system polls the inbox, extracts the PDF, reads the technician's name from the signature section, identifies the parts used, deducts from the correct truck and warehouse stock, and logs everything. No manual input required from the tech.

### Core design principle

**Deduction-only.** The system only records what was PAID and SIGNED. It never adds parts to inventory except via:
- `POST /dashboard/stock/restock` — moving parts from warehouse to truck (triggered by Nick)
- `POST /dashboard/stock/adjust` — correcting discrepancies found during physical audit (triggered by Nick)

No returns, no giveaways, no freebies. The invoice is the single source of truth. If a part isn't on a signed paid invoice, it wasn't consumed.

### Why email + PDF?

The tech already receives invoice PDFs from Dispatch. Forwarding the email (1 tap, 3 seconds) is zero friction. The PDF format is consistent and never changes, so extraction is deterministic and reliable.

---

## 2. The Two Business Streams

DoorFlex operates two separate inventory streams under the same roof:

| Attribute | WARRANTY | SUBCONTRACTOR |
|-----------|----------|---------------|
| Entity | DoorFlex LLC (direct) | DoorFlex as sub for another garage door company |
| Revenue source | Home warranty companies (90% of jobs) | Weekly commission from the other company |
| Parts cost to Nick | 50% (partner pays other 50%) | 100% (Nick pays everything) |
| Dispatch access | Office + Field (admin) | Field only (technician app, no web) |
| Catalog control | 1 person controls part names | Techs can add parts freely (inconsistent names) |
| Staff | 4 techs shared across streams | 1 tech dedicated |
| Trucks | Both trucks (shared) | 1 dedicated truck |
| Invoicing | Per job from Dispatch Office | Weekly deposit, not per-job |
| Invoice format | Dispatch standard PDF (always same format) | Dispatch standard PDF (always same format) |

The 4 techs work both streams and use the same 2 trucks. Parts are physically stored in the same warehouse but belong to different logical inventories (one at 50% cost, one at 100%). When a tech grabs from the wrong bin, Nick loses money.

**Real example that triggered this system:** A tech pulled $600 in parts from the subcontractor storage for a warranty job. Nick paid 100% for parts that should have been 50%. He lost $300.

The system must track which stream each part came from so every deduction is costed correctly.

---

## 3. Architecture

```
Coolify (auto SSL + reverse proxy)
│
└── doorflex-inventory service (single FastAPI process)
    │
    ├── Background task: polls az.mystock@gmail.com via IMAP
    │   every 60 seconds for new invoice PDFs
    │
    ├── /dashboard/*             → Admin dashboard (password protected)
    │                              Shared account for Nick + Maria
    │
    ├── SQLite database (WAL mode for concurrent writes)
    │   ├── techs, trucks, warehouses, parts, stock, log tables
    │   ├── Two separate warehouses (warranty + subcontractor)
    │   └── Append-only transaction log
```

### IMAP vs Gmail API

**Decision:** Use IMAP with an app password.

| Approach | Setup time | Complexity |
|----------|-----------|------------|
| Gmail API | ~1 hour | OAuth, service account, domain-wide delegation |
| IMAP + app password | ~5 minutes | Generate app password in Google account settings |

Both are equally reliable for polling a single inbox. IMAP is simpler and avoids Google Cloud Project setup entirely.

---

## 4. Email Processor

### Inbound flow

```
Tech receives invoice PDF from Dispatch (always standard format)
        │
        ▼
Tech forwards the email to az.mystock@gmail.com (1 tap)
        │
        ▼
System polls the Gmail inbox via IMAP every 60 seconds
        │
        ▼
New email found → download PDF → process → mark as read
```

### Processing pipeline

```
IMAP returns new unread message
        │
        ├── 1. Download PDF attachment(s)
        │
        ├── 2. Parse PDF:
        │       ├── Extract technician name from signature section
        │       │   (looks for "Tech Signature" or "Technician" field)
        │       │
        │       ├── Detect invoice format:
        │       │   ├── Format A (package pricing — single price per job)
        │       │   ├── Format B (itemized list — per-part prices)
        │       │   └── Neither → flag for manual review
        │       │
        │       └── Extract parts and quantities:
        │           ├── Format A: match job description against
        │           │   packages table → expand to list of parts
        │           ├── Format B: extract each line item
        │           │   (part name/description + quantity)
        │
        ├── 3. Look up technician by extracted name in techs table
        │
        ├── 4. Look up tech's assigned truck and warehouse
        │       (determines which stream: warranty or subcontractor)
        │
        ├── 5. Match part names against canonical parts catalog:
        │       ├── Format A: exact package match (must exist in packages table)
        │       ├── Format B: fuzzy match raw name + aliases against
        │       │   canonical name (difflib + alias lookup)
        │       └── Confidence < 70% → flag in unmatched list
        │
        ├── 6. Deduct from truck stock
        │
        ├── 7. Deduct from warehouse stock
        │
        ├── 8. Log: tech, truck, warehouse, stream, parts,
        │       stock before/after, invoice ID, confidence
        │
        ├── 9. If some parts unmatched (confidence < 70%):
        │       → Flag in dashboard for manual mapping
        │       → Deduct matched parts, skip unmatched
        │       → No notification to tech
        │
        └── 10. Check stock thresholds → queue alert if below minimum
```

### Format A: Package pricing → parts expansion

Format A invoices contain a job description (e.g., "Full spring replacement") and a single price. There is no itemized parts list. To deduct parts from inventory, the system uses a **packages** table:

```sql
packages
├── id       TEXT PK | e.g. "pkg-spring-full"
├── name     TEXT    | "Full spring replacement"
├── parts_json TEXT  | [{"part_id": "SPR-207-30", "qty": 2}, {"part_id": "CAB-18", "qty": 1}]
├── active   BOOLEAN |
└── created_at TIMESTAMP
```

**Flow:**
1. Extract job description from Format A PDF (e.g., "Full spring replacement + cable")
2. Fuzzy-match against `packages.name`
3. If match found → expand `parts_json` into deduction transactions
4. If no match → flag invoice in "Unmatched" section with the raw description
5. Nick creates a new `package` mapping the description to the correct parts
6. Future invoices with the same description auto-match

Nick already knows which parts go into each package. He can create and manage packages directly from the dashboard.

### Reliability guarantees

- The two Dispatch invoice formats are STABLE. They never change. The PDF template is generated by the Dispatch platform and is identical every time.
- The parser uses deterministic extractors (one per format), not AI.
- If a PDF does not match either format, it is flagged for manual review without attempting deduction.
- Every email is logged regardless of success or failure, including the raw email body and extracted text for debugging.

### Handling concurrent emails

- FastAPI async handles multiple IMAP polling cycles without blocking.
- SQLite uses WAL (Write-Ahead Logging) mode, which allows concurrent reads and non-conflicting writes.
- Each email is processed atomically in its own database transaction.
- If two emails for the same truck arrive in the same polling cycle, transactions queue and apply sequentially.

---

## 5. Technician Identification

### How the system knows which tech sent it

The system identifies the technician by parsing their signature from the invoice PDF:

```
PDF signature section contains:
┌────────────────────────────────┐
│ Customer Signature: ___X___    │
│ Technician Signature: Juan P.  │   ← This is what the system reads
│ Date: 07/14/2026               │
└────────────────────────────────┘
```

The system extracts "Juan P." and looks it up in the techs table by name (flexible match: supports initials, partial names).

### No per-tech email aliases needed

One single inbox: `az.mystock@gmail.com`. All techs forward to the same address. The system figures out who sent it from the PDF content.

---

## 6. Data Model

### techs

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| name | TEXT | Technician full name (as it appears in PDF signature) |
| truck_id | TEXT FK | Currently assigned truck (modifiable in dashboard) |
| warehouse_id | TEXT FK | Currently assigned warehouse (modifiable in dashboard) |
| active | BOOLEAN | Always true while employed |
| created_at | TIMESTAMP | |

### trucks

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | e.g. "T-01" |
| name | TEXT | Human-readable |
| warehouse_id | TEXT FK | Which warehouse this truck restocks from |
| active | BOOLEAN | |
| created_at | TIMESTAMP | |

### warehouses

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | "wh-warranty" or "wh-sub" |
| name | TEXT | "Warranty (50%)" or "Subcontractor (100%)" |
| cost_share | REAL | Nick's cost percentage: 0.50 or 1.00 |
| active | BOOLEAN | |
| created_at | TIMESTAMP | |

### parts (canonical catalog)

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT PK | SKU e.g. "SPR-207-30" |
| name | TEXT | Canonical display name |
| aliases | TEXT | JSON array of alternative names ("red spring", "207 spring") |
| unit | TEXT | "piece", "pair", etc. |
| active | BOOLEAN | |
| created_at | TIMESTAMP | |

### stock (per truck per part)

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| truck_id | TEXT FK | |
| part_id | TEXT FK | |
| quantity | INTEGER | Current stock on that truck |
| min_threshold | INTEGER | Per-part, per-truck reorder threshold |
| updated_at | TIMESTAMP | |

### warehouse_stock (per warehouse per part)

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| warehouse_id | TEXT FK | |
| part_id | TEXT FK | |
| quantity | INTEGER | Current stock in warehouse |
| min_threshold | INTEGER | Reorder threshold for warehouse |
| updated_at | TIMESTAMP | |

### log (append-only, never modified)

| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | |
| timestamp | TIMESTAMP | |
| tech_name | TEXT | Extracted from PDF signature |
| truck_id | TEXT | |
| warehouse_id | TEXT | |
| stream | TEXT | "warranty" or "subcontractor" |
| part_id | TEXT FK | |
| quantity_change | INTEGER | Negative for deduction, positive for restock/adjust |
| cost_share | REAL | 0.50 or 1.00 at time of transaction |
| stock_before | INTEGER | Truck stock before transaction |
| stock_after | INTEGER | Truck stock after transaction |
| invoice_id | TEXT | From the email or PDF |
| confidence | REAL | Name matching confidence (0.0 to 1.0) |
| raw_text | TEXT | Original extracted text for audit |
| source | TEXT | "email", "manual_adjustment", "restock" |
| format | TEXT | "package_pricing" or "itemized" |

---

## 7. API Endpoints

### Dashboard (password protected — single account for Nick + Maria)

#### GET /dashboard
Main dashboard with Parts at a Glance summary + Top Movers + Alerts.

#### GET /dashboard/techs
List all techs with their assigned truck, warehouse, and status.

#### POST /dashboard/techs/create
Add a tech with name and assigned truck + warehouse. Returns their info link.

#### POST /dashboard/techs/update
Change a tech's truck or warehouse assignment.

#### GET /dashboard/trucks
List all trucks with current stock levels and assigned warehouse.

#### POST /dashboard/trucks/create
Add a new truck.

#### GET /dashboard/parts
Canonical parts list with aliases.

#### POST /dashboard/parts/create
Add a new part to the catalog.

#### POST /dashboard/parts/bulk-import
**CSV import.** Accepts CSV with columns: `sku, name, unit, min_threshold_warranty, min_threshold_subcontractor, aliases`.
- If `sku` exists → update
- If `sku` is new → create
- Returns summary: `{created: N, updated: N, errors: [{row, reason}]}`

#### POST /dashboard/parts/add-alias
Map an unrecognized part name to an existing canonical part.

#### POST /dashboard/parts/update-name
Change a part's display name without affecting historical logs.

#### GET /dashboard/packages
List all packages (Format A job descriptions with their parts mapping).

#### POST /dashboard/packages/create
Create a new package: name, description, parts list.

#### POST /dashboard/packages/update
Edit a package's name or parts list.

#### DELETE /dashboard/packages/{id}
Soft-delete a package (does not break historical logs).

#### GET /dashboard/stock
Full stock view per truck and per warehouse. Shows expected vs actual for audits.

#### POST /dashboard/stock/adjust
Manual stock correction after physical audit.

#### POST /dashboard/stock/restock
Move parts from warehouse to a truck. Auto-deducts from warehouse and adds to truck.

#### GET /dashboard/unmatched-parts
List all incoming part names that could not be matched. Each shows the raw name, quantity, and a dropdown to map to a canonical part.

#### GET /dashboard/log
Paginated, filterable transaction log (by tech, truck, part, date range, stream).

#### GET /dashboard/log/export
Export log as printable PDF for physical audits (expected vs actual).

#### GET /dashboard/alerts
Current low-stock alerts across all trucks and warehouses.

#### GET /dashboard/top-movers
Top 10 parts by usage volume (last 30 days), broken down by stream.

---

## 8. Admin Dashboard (Detailed)

### Layout

```
┌────────────────────────────────────────────────────────────┐
│  🚚 DoorFlex Inventory                                [Logout]
├────────────────────────────────────────────────────────────┤
│                                                            │
│  Summary Cards                                             │
│  ┌────────┬──────────┬───────────┬──────────┐             │
│  │ Techs  │ Trucks   │ Alerts    │ Unmatched│             │
│  │   4    │    2     │    3 🔴   │   2 ⚠️   │             │
│  └────────┴──────────┴───────────┴──────────┘             │
│                                                            │
│  ================ Parts at a Glance ================       │
│                                                            │
│  📦 Parts Used (last 30 days)     Invoices: 47 (95% auto) │
│                                                            │
│  WARRANTY stream (Nick's share: 50%)                       │
│    Total line items deducted:      844                      │
│    Most used: Nylon Roller (240), Spring 207x30 (62)       │
│                                                            │
│  SUBCONTRACTOR stream (Nick's share: 100%)                 │
│    Total line items deducted:      312                      │
│    Most used: Spring 207x30 (25), Steel Cable 18ft (18)    │
│                                                            │
│  Total parts tracked this month:  1,156                    │
│                                                            │
│  ================ Top Movers ================              │
│                                                            │
│  Top 10 Parts (last 30 days)                               │
│    1. Nylon Roller           240  ▼                        │
│    2. Spring 207x30           87                           │
│    3. Steel Cable 18ft        45                           │
│    4. End Bearing Bracket     31                           │
│    5. Center Bearing Bracket  22                           │
│    6. Bottom Bracket          18                           │
│    7. Hinge 1                 15                           │
│    8. Hinge 2                 12                           │
│    9. Coupler                 8                            │
│   10. Keypad                  5                            │
│                                                            │
│  Navigation: Techs | Trucks | Parts | Packages | Stock     │
│              Log | Alerts | Top Movers                      │
│                                                            │
│  ⚠️ Unmatched Parts (needs mapping)                       │
│  ┌──────┬──────────────────┬──────┬────────────────────┐   │
│  │ Tech │ Raw Name         │ Qty  │ Map to Part        │   │
│  ├──────┼──────────────────┼──────┼────────────────────┤   │
│  │ Juan │ "red coil spring"│ 2    │ [▼ SPR-207-30]     │   │
│  │ Juan │ "big cable"      │ 1    │ [▼ CAB-18]         │   │
│  └──────┴──────────────────┴──────┴────────────────────┘   │
│  [Save Mapping]  [Ignore]                                   │
│                                                            │
│  ================ Inventory View ================          │
│                                                            │
│  ┌─────────┬────────────────────┬────────────────────┐     │
│  │ Part    │ T-01 (Sub 100%)    │ T-02 (Warr 50%)    │     │
│  ├─────────┼────────────────────┼────────────────────┤     │
│  │SPR-207  │ 8 (min 5) ✅      │ 12 (min 5) ✅      │     │
│  │BRG-END  │ 3 (min 5) 🔴      │ 15 (min 5) ✅      │     │
│  │CAB-18   │ 7 (min 5) ✅      │ 4 (min 5) 🔴       │     │
│  └─────────┴────────────────────┴────────────────────┘     │
│  [Print Audit Report]  [Adjust Stock]  [Restock Truck]     │
│                                                            │
│  Warehouse Stock                                            │
│  ┌─────────┬────────────────────┬────────────────────┐     │
│  │ Part    │ wh-warranty (50%)  │ wh-sub (100%)      │     │
│  ├─────────┼────────────────────┼────────────────────┤     │
│  │SPR-207  │ 25 (min 10) ✅    │ 30 (min 10) ✅     │     │
│  │BRG-END  │ 30 (min 10) ✅    │ 25 (min 10) ✅     │     │
│  └─────────┴────────────────────┴────────────────────┘     │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

### Key dashboard actions for Nick (and Maria)

| Action | How |
|--------|-----|
| Assign tech to truck/warehouse | Edit tech, pick from dropdown |
| Move tech to different truck | One click, changes future deductions |
| Restock truck from warehouse | Enter part + qty, system deducts warehouse + adds to truck |
| Map unknown part to canonical | Select raw name, pick canonical part, save |
| Create/edit a package (Format A) | Name + add parts from catalog |
| Bulk import parts from CSV | Upload file, system ingests |
| Adjust stock after physical count | Enter new qty, system logs delta |
| Print audit report | One click, expected vs actual per truck |
| View all unmatched parts | Separate section in dashboard |
| View alerts | At-a-glance card, drill into details |

---

## 9. Alerts

### Trigger

Every time a part is deducted (via email), the system checks:

```python
if stock_after < min_threshold:
    send_alert(nick_email, truck, part, stock_after, min_threshold)
```

### Alert format

```
Subject: ⚠️ DoorFlex Inventory - Stock Low

Truck: T-01
Stream: Subcontractor (100%)
Part: Spring 207x30
Current stock: 3
Minimum: 5
Suggested restock: 10

Registered by: Juan (from invoice INV-1234)

Dashboard: https://inventory.doorflex.app/dashboard
```

### Weekly digest

Every Friday at 5pm, Nick receives a summary of ALL low-stock items across all trucks and warehouses, grouped by stream, with restock recommendations.

---

## 10. Unmatched Parts Workflow

When an email contains a part name that does not match any canonical part (confidence < 70%):

```
1. System flags the entry in dashboard (does NOT notify tech)
2. Dashboard "Unmatched Parts" counter increments
3. In the dashboard:
   - Raw name shown (e.g., "red coil spring")
   - Quantity shown
   - Tech who sent it shown
   - Dropdown to map to any canonical part
4. Nick (or Maria) selects the correct part from dropdown, saves
5. System adds the raw name as an alias to that canonical part
6. Future invoices with the same raw name auto-match at high confidence
```

Multiple unmatched entries can be mapped in bulk (select all, assign part, save).

---

## 11. Audits (Expected vs Actual)

```
1. Dashboard > Stock > Print Audit Report
2. System generates a printable PDF with:
   - Per truck: expected stock per part (from system)
   - Empty column for actual count
   - Variance column
3. Nick counts physically, writes in actuals
4. Nick enters corrections in dashboard:
   - If actual < expected → system logs deduction (loss/theft)
   - If actual > expected → system logs addition (found/miscount)
5. Log shows source = "manual_adjustment" for audit trail
```

---

## 12. Part Name Changes (Historical Integrity)

Part names can change without breaking logs:

```
- Log stores part_id (SPR-207-30), not the display name
- Display name is a label that can be changed freely
- Aliases persist regardless of display name changes
- Historical logs always show what was recorded at time of transaction
```

---

## 13. Dashboard Views (the 3 core screens)

### Parts at a Glance (homepage)

Computed from the `log` table, no new schema needed:

**Query 1: Line items by stream (last 30 days)**
```sql
SELECT stream, COUNT(*) AS line_items
FROM log
WHERE timestamp >= datetime('now', '-30 days')
  AND source = 'email'
GROUP BY stream;
```

**Query 2: Top part per stream (last 30 days)**
```sql
SELECT stream, part_id, SUM(ABS(quantity_change)) AS total_used
FROM log
WHERE timestamp >= datetime('now', '-30 days')
  AND source = 'email'
GROUP BY stream, part_id
ORDER BY stream, total_used DESC
LIMIT 1;
```

**Query 3: Total invoices processed**
```sql
SELECT COUNT(DISTINCT invoice_id) AS total_invoices
FROM log
WHERE timestamp >= datetime('now', '-30 days');
```

**Query 4: Auto-match rate**
```sql
SELECT
  CAST(SUM(CASE WHEN confidence >= 0.7 THEN 1 ELSE 0 END) AS REAL)
  / COUNT(*) AS rate
FROM log
WHERE timestamp >= datetime('now', '-30 days')
  AND source = 'email';
```

### Top Movers (standalone page)

```sql
SELECT part_id, SUM(ABS(quantity_change)) AS total_used
FROM log
WHERE timestamp >= datetime('now', '-30 days')
  AND source = 'email'
GROUP BY part_id
ORDER BY total_used DESC
LIMIT 10;
```

Optionally filtered by stream:
```sql
SELECT part_id, SUM(ABS(quantity_change)) AS total_used
FROM log
WHERE timestamp >= datetime('now', '-30 days')
  AND source = 'email'
  AND stream = 'warranty'
GROUP BY part_id
ORDER BY total_used DESC
LIMIT 10;
```

### Audit Discrepancies (section inside Stock page)

```sql
SELECT
  truck_id,
  part_id,
  SUM(ABS(quantity_change)) AS discrepancy_quantity,
  COUNT(*) AS adjustment_count
FROM log
WHERE source = 'manual_adjustment'
  AND quantity_change < 0   -- parts missing = loss
GROUP BY truck_id, part_id
ORDER BY discrepancy_quantity DESC;
```

Shows only when there have been manual adjustments. Starts empty.

---

## 14. Tech Stack

| Component | Technology | Reason |
|-----------|-----------|--------|
| Language | Python 3.11+ | Fer's preference, easy maintain |
| Framework | FastAPI | Async, fast, built-in validation |
| Server | Uvicorn (via Coolify) | SSL + proxy handled |
| Database | SQLite (WAL mode) | Handles concurrent writes |
| PDF parsing | pypdf or pdfplumber | Extract text deterministically |
| IMAP client | built-in `imaplib` | Simple, no external dependency |
| Name matching | difflib + alias lookup | No AI API, zero cost |
| Templates | Jinja2 | Server-side rendering |
| CSS | Pico CSS | Under 10KB, mobile-first |
| CSV parsing | Python csv module | Built-in, deterministic |
| Alerts | Resend outbound email | Already used by Fer |
| Hosting | Coolify on VPS | Included in retainer |
| Backup | Cron + cp | Simple, 3 lines in crontab |

---

## 15. Directory Structure

```
doorflex-inventory/
├── main.py                 # App entry, router registration
├── config.py               # Environment config
├── database.py             # SQLite connection + schema + WAL mode
│
├── modules/
│   ├── __init__.py
│   ├── email_poller.py     # IMAP poller, PDF download
│   ├── pdf_extractor.py    # Parse Dispatch PDF (both Format A + B)
│   ├── name_matcher.py     # Fuzzy match part names → canonical
│   ├── inventory.py        # Stock deduction + restock + threshold check
│   ├── packages.py         # Package CRUD + format A expansion
│   ├── techs.py            # Technician CRUD
│   ├── trucks.py           # Truck CRUD + warehouse assignment
│   ├── parts.py            # Parts catalog + alias management + CSV import
│   ├── alerts.py           # Low-stock alerts + weekly digest
│   ├── logger.py           # Append-only transaction log
│   ├── analytics.py        # Parts at a Glance + Top Movers queries
│   └── audit.py            # Expected vs actual + printable reports
│
├── templates/
│   ├── dashboard_base.html
│   ├── dashboard_home.html       # Parts at a Glance
│   ├── dashboard_techs.html
│   ├── dashboard_trucks.html
│   ├── dashboard_parts.html
│   ├── dashboard_packages.html   # CRUD for Format A packages
│   ├── dashboard_stock.html      # + Audit Discrepancies section
│   ├── dashboard_log.html
│   ├── dashboard_alerts.html
│   ├── dashboard_top_movers.html # Top 10 with stream filter
│   └── dashboard_unmatched.html
│
├── data/
│   └── inventory.db
│
├── requirements.txt
├── .env.example
└── README.md
```

---

## 16. Modules

| Module | Responsibility | Depends on |
|--------|---------------|------------|
| email_poller | IMAP poll every 60s, download PDF attachments | pdf_extractor, name_matcher, inventory, logger |
| pdf_extractor | Parse Dispatch PDF, detect format, extract tech name + parts/description | None (pure parsing) |
| name_matcher | Fuzzy match raw names to canonical part + alias expansion | parts |
| inventory | Deduct, restock, threshold check | logger |
| packages | CRUD + expand package → parts list (Format A) | database |
| techs | CRUD techs | database |
| trucks | CRUD trucks | database |
| parts | CRUD parts + aliases + CSV bulk import | database |
| alerts | Low-stock emails + weekly digest | inventory |
| logger | Append-only log | database |
| analytics | Parts at a Glance + Top Movers queries | logger |
| audit | Expected vs actual, printable reports | inventory |

All modules are independent files. Adding a new feature = new file in `modules/` + route in `main.py`. No existing code touched.

---

## 17. Setup

### Prerequisites

- Python 3.11+
- Coolify instance (already running on VPS)
- IMAP-ready email: `az.mystock@gmail.com` with app password enabled
- Resend account (for alert emails)
- Nick's two PDF invoice examples (already provided)
- Nick's initial parts list (CSV format for bulk import)

### Deployment via Coolify

1. Push code to a Git repository
2. Coolify: Create service → select repo
3. Build: `pip install -r requirements.txt`
4. Start: `uvicorn main:app --host 0.0.0.0 --port 8000`
5. Set environment variables in Coolify
6. Coolify auto-generates SSL + public URL

### Environment Variables

```
ADMIN_PASSWORD=your-secret-password
DATABASE_PATH=data/inventory.db
IMAP_EMAIL=az.mystock@gmail.com
IMAP_PASSWORD=your-app-password
POLL_INTERVAL_SECONDS=60
RESEND_API_KEY=your-resend-key
NICK_EMAIL=nick@doorflexaz.com
```

### Daily Backup (3-line cron)

```bash
#!/bin/bash
cp /app/doorflex-inventory/data/inventory.db /app/backups/inventory-$(date +%F).db
find /app/backups/ -mtime +30 -delete
```

---

## 18. Testing Plan

1. Create a tech via dashboard with name + truck + warehouse
2. Create a warehouse (wh-warranty at 50% or wh-sub at 100%)
3. Bulk import parts via CSV
4. Create a package mapping a job description to multiple parts
5. Forward a test Format B invoice (itemized list) → verify extraction
6. Forward a test Format A invoice (package pricing) → verify package match + expansion
7. Verify stock deducted from both truck and warehouse
8. Verify log entry with correct tech, truck, warehouse, stream
9. Forward an invoice with an unknown part → verify flagged as unmatched
10. Map the unknown part via dashboard → send again → verify auto-match
11. Trigger low stock → verify alert email to Nick
12. Restock truck from warehouse → verify both stocks adjust
13. Print audit report → verify layout
14. Adjust stock manually → verify log shows "manual_adjustment"
15. Change tech's truck assignment → verify next invoice deducts from new truck

---

## 19. Future Considerations (explicitly out of scope for MVP)

These features were discussed during the discovery call but are intentionally excluded from the MVP:

| Feature | Reason for deferral |
|---------|-------------------|
| Multi-user roles (Maria separate account) | Nick trusts Maria with full access. Single account is fine for now. |
| Supplier/distributor management | Nick prefers to handle negotiation manually. Alerts go to him. |
| Returns / giveaways tracking | Deduction-only architecture. What's on the signed invoice is what was used. |
| Tech confirmation view | Techs forward email and trust the system. Not needed for MVP. |
| Missing invoice detection | Maria can cross-check against Dispatch manually. |
| Part unit prices / financial $ metrics | Nick hasn't provided pricing. MVP tracks quantities. Prices can be added later. |
| Supplier-specific reorder emails | Alert goes to Nick, he decides where to order from. |
| Google Sheets read-only sync | Only if Nick explicitly requests it after using the dashboard. |
| Mobile app | Pico CSS is mobile-first. Browser access from phone is sufficient. |

---

> **Próximo paso:** Revisar y aprobar este PRD. Una vez firmado el ICA, se procede a implementación.
