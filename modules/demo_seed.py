"""Full synthetic demo dataset (DEMO_MODE).

Builds ~6 months of believable history for a fictitious crew of four techs by
driving the app's REAL pipelines, not by inserting finished rows:

- Invoices go through `email_poller._process_extracted_data` (the same path a
  PDF from the Cloudflare email worker takes after parsing), so alias / fuzzy /
  package matching, unmatched parts and stock shortfalls come from app logic.
- The PWA tech deducts with `inventory.pwa_deduct` and closes jobs with
  `job_captures.create_job_capture`, exactly like tech_pwa/app.js does.
- Winston reports are created with `create_draft` + `confirm_draft`, which runs
  the real reconciliation and opens disputes.

Every module function stamps rows with "now", so each simulated day ends with
`_stamp_day()`, which rewrites the timestamps of the rows created during that
day to that day. Deterministic for a given (today, seed): same input, same DB.

All people, addresses, WO numbers and money amounts are invented.
"""
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from modules import door_orders as door_orders_mod
from modules import inventory
from modules import job_captures as job_captures_mod
from modules import pwa
from modules import reorder as reorder_mod
from modules import winston_reports as winston_reports_mod
from modules.email_poller import _process_extracted_data
from modules.email_service import log_email

HISTORY_DAYS = 182          # ~6 months, fills the "Monthly Deductions" chart
FIELD_OPS_DAYS = 28         # PWA job captures / reconciliation / Winston reports window
DEMO_PWA_TOKEN_DEFAULT = "demo-tech-andre"

# ─── Cast (fictitious) ────────────────────────────────────────────────────────

WAREHOUSES = [("wh-warranty", "DoorFlex"), ("wh-sub", "Total Garage Solutions")]

TRUCKS = [
    ("T-01", "Truck 1 · Ford Transit", "wh-sub"),
    ("T-02", "Truck 2 · Ram ProMaster", "wh-warranty"),
    ("T-03", "Truck 3 · Ford E-350", "wh-warranty"),
    ("T-04", "Truck 4 · Chevy Express", "wh-sub"),
]

# (name, truck, warehouse, deduction_source, invoices per day range)
TECHS = [
    ("Marco Díaz", "T-02", "wh-warranty", "email", (2, 4)),
    ("Kyle Bennett", "T-03", "wh-warranty", "email", (2, 4)),
    ("Luis Ortega", "T-01", "wh-sub", "email", (1, 3)),
    ("Andre Wallace", "T-04", "wh-sub", "pwa", (3, 5)),
]
PWA_TECH = "Andre Wallace"

DISTRIBUTORS = [
    ("service-spring", "Service Spring Corp", "orders@servicespring.example"),
    ("ddm", "DDM Garage Doors", "sales@ddm.example"),
]

# (id, name, category, cost, aliases, truck_min, wh_min, usage_weight, distributor)
PARTS = [
    ("SPR-207-31", "Torsion Spring .207 x 2 x 31", "Springs", 38.0, ["spring 207x2x31", ".207 spring"], 4, 12, 6, "service-spring"),
    ("SPR-218-32", "Torsion Spring .218 x 2 x 32", "Springs", 42.0, ["spring 218x2x32", ".218 spring"], 4, 12, 9, "service-spring"),
    ("SPR-225-33", "Torsion Spring .225 x 2 x 33", "Springs", 46.0, ["spring 225x2x33", ".225 spring"], 4, 10, 7, "service-spring"),
    ("SPR-234-34", "Torsion Spring .234 x 2 x 34", "Springs", 52.0, ["spring 234x2x34"], 2, 8, 3, "service-spring"),
    ("EXT-SPR-140", "Extension Spring 140 lb (pair)", "Springs", 24.0, ["extension springs"], 2, 6, 2, "service-spring"),
    ("CAB-7FT", "Lift Cable 7ft (pair)", "Cables", 14.0, ["cables 7ft", "7ft cables"], 4, 16, 8, "service-spring"),
    ("CAB-8FT", "Lift Cable 8ft (pair)", "Cables", 16.0, ["cables 8ft"], 3, 12, 4, "service-spring"),
    ("RLR-NYL-13", "Nylon Roller 13-Ball", "Rollers", 4.5, ["rollers", "roller", "nylon rollers"], 30, 120, 10, "ddm"),
    ("BRG-END", "End Bearing Plate", "Bearings", 9.0, ["bearings", "end bearing"], 6, 24, 6, "ddm"),
    ("BRG-CTR", "Center Bearing", "Bearings", 11.0, ["center bearing plate"], 3, 12, 3, "ddm"),
    ("HNG-1", "Hinge #1", "Hinges", 3.2, ["hinge 1", "#1 hinge"], 10, 40, 4, "ddm"),
    ("HNG-2", "Hinge #2", "Hinges", 3.4, ["hinge 2", "#2 hinge"], 8, 30, 3, "ddm"),
    ("HNG-3", "Hinge #3", "Hinges", 3.6, ["hinge 3", "#3 hinge"], 8, 30, 3, "ddm"),
    ("DRM-400", "Cable Drum 400-8 (pair)", "Hardware", 28.0, ["drums", "cable drums"], 2, 8, 3, "service-spring"),
    ("SFT-1IN-8", "Torsion Shaft 1in x 8ft", "Hardware", 34.0, ["shaft"], 1, 4, 1, "service-spring"),
    ("RTN-SPR", "Spring Retention", "Hardware", 6.0, ["spring retention"], 4, 16, 5, "ddm"),
    ("TRK-STRAP", "Track Strap", "Hardware", 4.0, ["track straps"], 6, 20, 2, "ddm"),
    ("STR-16", "Reinforcement Strut 16ft", "Hardware", 36.0, ["strut"], 1, 4, 1, "ddm"),
    ("SEAL-BTM-16", "Bottom Seal 16ft", "Seals", 22.0, ["bottom seal", "bottom rubber"], 3, 12, 5, "ddm"),
    ("SEAL-TOP-9", "Top Seal 9ft", "Seals", 12.0, ["top seal", "weather seal"], 2, 8, 2, "ddm"),
    ("OPN-LM-87504", "LiftMaster 87504 Belt Opener", "Openers", 389.0, ["liftmaster 87504", "belt opener"], 1, 3, 2, "ddm"),
    ("OPN-LM-8500", "LiftMaster 8500W Wall Mount Opener", "Openers", 520.0, ["wall mount opener", "liftmaster 8500"], 0, 2, 1, "ddm"),
    ("OPN-CH-B4613", "Chamberlain B4613T Opener", "Openers", 260.0, ["chamberlain opener"], 1, 3, 1, "ddm"),
    ("SNS-SAFETY", "Safety Sensor Kit", "Electrical", 38.0, ["safety sensors", "photo eyes", "safety eyes"], 2, 8, 3, "ddm"),
    ("RMT-893MAX", "LiftMaster 893MAX Remote", "Electrical", 24.0, ["remote", "893max remote"], 3, 12, 3, "ddm"),
    ("KPD-877MAX", "LiftMaster 877MAX Keypad", "Electrical", 32.0, ["keypad"], 1, 6, 1, "ddm"),
    ("LGB-041D", "Logic Board 041D8160", "Electrical", 89.0, ["logic board"], 1, 4, 1, "ddm"),
    ("LUB-SPRAY", "Garage Door Lubricant", "Consumables", 7.0, ["lube", "lubricant"], 4, 24, 6, "ddm"),
]

PACKAGES = [
    ("Spring Replacement Package", [("SPR-218-32", 2), ("CAB-7FT", 1), ("BRG-END", 2), ("RTN-SPR", 1)]),
    ("Tune-Up Package", [("RLR-NYL-13", 10), ("LUB-SPRAY", 1)]),
    ("Opener Install Package", [("OPN-LM-87504", 1), ("SNS-SAFETY", 1), ("RMT-893MAX", 1)]),
]

# Ways a tech types a part on an invoice: exact name, alias, or a near-miss
# that the fuzzy matcher still resolves on its own.
VARIANTS = {
    "RLR-NYL-13": ["Rollers", "Nylon Roller 13-Ball", "Nylon rollers"],
    "BRG-END": ["Bearings", "End bearing plate"],
    "SPR-218-32": ["Spring .218 x 2 x 32", "Torsion Spring .218 x 2 x 32", "spring 218x2x32"],
    "SPR-225-33": ["Torsion Spring .225 x 2 x 33", "spring 225x2x33"],
    "SPR-207-31": ["Torsion Spring .207 x 2 x 31", ".207 spring"],
    "CAB-7FT": ["Lift Cable 7ft (pair)", "Cables 7ft"],
    "SEAL-BTM-16": ["Bottom seal", "Bottom Seal 16ft"],
    "RTN-SPR": ["Spring retention"],
    "SNS-SAFETY": ["Safety sensors", "Photo eyes"],
    "LUB-SPRAY": ["Lube"],
}

# Typical jobs: list of (part_id, qty range)
JOB_RECIPES = [
    ("Broken spring", [("SPR-218-32", (1, 2)), ("RTN-SPR", (1, 1)), ("LUB-SPRAY", (1, 1))]),
    ("Broken spring", [("SPR-225-33", (1, 2)), ("CAB-7FT", (1, 1))]),
    ("Broken spring", [("SPR-207-31", (2, 2)), ("BRG-END", (2, 2))]),
    ("Noisy door", [("RLR-NYL-13", (8, 12)), ("BRG-END", (1, 2)), ("LUB-SPRAY", (1, 1))]),
    ("Off track", [("RLR-NYL-13", (4, 10)), ("HNG-1", (1, 3)), ("HNG-2", (0, 2)), ("TRK-STRAP", (0, 2))]),
    ("Snapped cable", [("CAB-7FT", (1, 1)), ("DRM-400", (0, 1))]),
    ("Snapped cable", [("CAB-8FT", (1, 1)), ("BRG-CTR", (0, 1))]),
    ("Opener not working", [("LGB-041D", (1, 1)), ("SNS-SAFETY", (0, 1))]),
    ("Opener not working", [("SNS-SAFETY", (1, 1)), ("RMT-893MAX", (0, 1))]),
    ("Weather seal", [("SEAL-BTM-16", (1, 1)), ("SEAL-TOP-9", (0, 1))]),
    ("New opener", [("OPN-LM-87504", (1, 1)), ("RMT-893MAX", (1, 2)), ("KPD-877MAX", (0, 1))]),
    ("New opener", [("OPN-CH-B4613", (1, 1)), ("SNS-SAFETY", (1, 1))]),
    ("Panel damage", [("HNG-2", (2, 4)), ("HNG-3", (1, 3)), ("STR-16", (0, 1))]),
]
JOB_PRICES = {  # what the homeowner pays, by job type (min, max)
    "Broken spring": (289, 489), "Noisy door": (149, 269), "Off track": (189, 349),
    "Snapped cable": (179, 299), "Opener not working": (129, 389), "Weather seal": (119, 219),
    "New opener": (589, 889), "Panel damage": (249, 549),
}

# Raw names that do NOT exist in the catalog: they land in Unmatched for a
# human to map (one is close enough to be a fuzzy suggestion).
UNKNOWN_ITEM_DAYS = {9: "Hinge #4 heavy duty", 7: "Weatherstrip PVC 9ft", 5: "Torsion Spring .243 x 2 x 36",
                     3: "Nylon Roller 11-Ball", 1: "Opener rail extension 8ft"}

STREETS = ["E Coyote Rd", "N Desert Willow Dr", "W Saguaro Ln", "S Ironwood Ave", "E Ocotillo St",
           "N Palo Verde Dr", "W Mesquite Trl", "E Cactus Wren Way", "S Agave Ct", "N Juniper Pl",
           "E Sonoran Blvd", "W Javelina Rd", "S Red Rock Dr", "E Canyon View Ln"]
CITIES = [("Scottsdale", "85259"), ("Mesa", "85212"), ("Gilbert", "85297"), ("Chandler", "85249"),
          ("Queen Creek", "85142"), ("Tempe", "85284"), ("Phoenix", "85048")]

CLOSING_NOTES = {
    "Broken spring": "Replaced broken torsion spring(s), new retention, lubed and balanced door. Tested 5 cycles OK.",
    "Noisy door": "Swapped worn rollers and bearings, full lube. Door runs quiet now, customer happy.",
    "Off track": "Put door back on track, replaced bent hinges/rollers, adjusted tracks. Tested OK.",
    "Snapped cable": "Replaced lift cables, re-wound drums, balanced door. Tested full open/close.",
    "Opener not working": "Diagnosed opener, replaced faulty component, reset limits and force. Working.",
    "Weather seal": "Installed new bottom/top seal, adjusted closing limit for a tight seal.",
    "New opener": "Removed old opener, installed new unit + sensors, programmed remotes and keypad.",
    "Panel damage": "Reinforced damaged section, replaced hinges, tested balance and travel.",
}

# ─── Timestamp bookkeeping ────────────────────────────────────────────────────

# table -> timestamp columns that the module functions fill with "now"
_TS_COLUMNS = {
    "log": ["timestamp"],
    "stock_alerts": ["created_at"],
    "app_events": ["ts"],
    "unmatched_parts": ["created_at"],
    "email_log": ["created_at"],
    "reorder_requests": ["created_at"],
    "expected_jobs": ["created_at", "updated_at"],
    "job_captures": ["received_at", "created_at"],
    "disputes": ["created_at", "updated_at"],
    "door_orders": ["captured_at", "updated_at"],
    "winston_reports": ["received_at", "processed_at"],
    "reconciliation_runs": ["created_at"],
    "deduction_batches": ["processed_at"],
}


def _marks(conn):
    return {t: conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {t}").fetchone()[0] for t in _TS_COLUMNS}


def _stamp_day(conn, marks, day, start_hour=7, span_hours=10):
    """Rewrite the timestamps of every row created since `marks` to `day`,
    spread over the working day in creation order (so a log's stock_before/
    stock_after chain still reads chronologically)."""
    base = datetime.combine(day, datetime.min.time()) + timedelta(hours=start_hour)
    for table, cols in _TS_COLUMNS.items():
        rows = conn.execute(f"SELECT rowid FROM {table} WHERE rowid > ? ORDER BY rowid", (marks[table],)).fetchall()
        if not rows:
            continue
        step = (span_hours * 3600) / max(len(rows), 1)
        for i, r in enumerate(rows):
            ts = (base + timedelta(seconds=int(i * step))).strftime("%Y-%m-%d %H:%M:%S")
            sets = ", ".join(f"{c}=?" for c in cols)
            conn.execute(f"UPDATE {table} SET {sets} WHERE rowid=?", (*([ts] * len(cols)), r[0]))
    conn.commit()


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ─── Builder ──────────────────────────────────────────────────────────────────


class _World:
    def __init__(self, conn, today, seed, assets_dir):
        self.conn = conn
        self.today = today
        self.rng = random.Random(f"{seed}:{today.isoformat()}")
        self.assets_dir = Path(assets_dir) if assets_dir else None
        self.tech_rows = {}
        self.invoice_seq = 1400
        self.wo_seq = 112_400_000 + self.rng.randint(0, 9000)
        self.capture_seq = 0
        self.summary = {"invoices": 0, "captures": 0, "disputes": 0, "reports": 0}
        self.door_specs = list(DOOR_ORDER_SPECS)
        self.door_orders = []   # (order_id, final_status, captured_day)
        self.current_day = today
        self._unknown_done = {}

    # ── catalog / fleet ──
    def setup(self):
        c = self.conn
        for wid, name in WAREHOUSES:
            c.execute("INSERT INTO warehouses (id, name) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, active=1",
                      (wid, name))
        for tid, name, wid in TRUCKS:
            c.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES (?, ?, ?) "
                      "ON CONFLICT(id) DO UPDATE SET name=excluded.name, warehouse_id=excluded.warehouse_id, active=1",
                      (tid, name, wid))
        for did, name, email in DISTRIBUTORS:
            c.execute("INSERT INTO distributors (id, name, email) VALUES (?, ?, ?) "
                      "ON CONFLICT(id) DO UPDATE SET name=excluded.name, email=excluded.email", (did, name, email))
        for pid, name, cat, cost, aliases, tmin, wmin, _w, dist in PARTS:
            c.execute("""INSERT INTO parts (id, name, aliases, unit, category, brand, supplier, cost)
                         VALUES (?, ?, ?, 'piece', ?, '', ?, ?)""",
                      (pid, name, json.dumps(aliases), cat, dict((d[0], d[1]) for d in DISTRIBUTORS)[dist], cost))
            c.execute("""INSERT INTO product_distributors (product_id, distributor_id, distributor_sku,
                         distributor_part_name, is_active) VALUES (?, ?, ?, ?, 1)""",
                      (pid, dist, f"{dist[:3].upper()}-{pid}", name))
            for wid, _ in WAREHOUSES:
                c.execute("INSERT INTO warehouse_stock (warehouse_id, part_id, quantity, min_threshold) VALUES (?, ?, ?, ?)",
                          (wid, pid, wmin * 3, wmin))
        for name, items in PACKAGES:
            pkg_id = name.lower().replace(" ", "-")
            c.execute("INSERT INTO packages (id, name, parts_json) VALUES (?, ?, ?)",
                      (pkg_id, name, json.dumps([{"part_id": p, "qty": q} for p, q in items])))
        for name, truck, wh, source, _rng in TECHS:
            cur = c.execute("INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) VALUES (?, ?, ?, ?)",
                            (name, truck, wh, source))
            self.tech_rows[name] = cur.lastrowid
        for tid, _name, _wh in TRUCKS:
            for pid, *_rest in PARTS:
                tmin = next(p[5] for p in PARTS if p[0] == pid)
                c.execute("INSERT INTO stock (truck_id, part_id, quantity, min_threshold) VALUES (?, ?, ?, ?)",
                          (tid, pid, tmin * 2, tmin))
        c.commit()

    def tech(self, name):
        return dict(self.conn.execute("SELECT * FROM techs WHERE id=?", (self.tech_rows[name],)).fetchone())

    # ── helpers ──
    def _pick_job(self):
        weights = [3, 2, 2, 3, 2, 2, 1, 2, 2, 2, 1, 1, 1]
        jtype, recipe = self.rng.choices(JOB_RECIPES, weights=weights)[0]
        items = []
        for pid, (lo, hi) in recipe:
            qty = self.rng.randint(lo, hi)
            if qty > 0:
                items.append((pid, qty))
        return jtype, items

    def _raw_name(self, pid):
        if pid in VARIANTS and self.rng.random() < 0.6:
            return self.rng.choice(VARIANTS[pid])
        return next(p[1] for p in PARTS if p[0] == pid)

    def _address(self):
        city, zipc = self.rng.choice(CITIES)
        return f"{self.rng.randint(1200, 24999)} {self.rng.choice(STREETS)}", city, zipc

    def _next_wo(self):
        self.wo_seq += self.rng.randint(7, 380)
        return str(self.wo_seq)

    # ── daily operations ──
    def email_tech_invoices(self, day, days_ago):
        for name, _truck, _wh, source, (lo, hi) in TECHS:
            if source != "email":
                continue
            n = self.rng.randint(lo, hi)
            if day.weekday() == 5:
                n = max(0, n - 2)
            for _ in range(n):
                self.invoice_seq += 1
                inv_no = self.invoice_seq
                invoice_id = f"<inv-{inv_no}@dispatch.example>"
                invoice_name = f"Invoice_{inv_no}.pdf"
                roll = self.rng.random()
                tech_name = name
                if roll < 0.06:
                    data = {"technician_name": tech_name, "format": "package_pricing", "parts_list": [],
                            "package_description": self.rng.choice(PACKAGES)[0]}
                else:
                    _jtype, items = self._pick_job()
                    parts_list = [{"raw_name": self._raw_name(pid), "quantity": q} for pid, q in items]
                    if days_ago in UNKNOWN_ITEM_DAYS and not self._unknown_done.get(days_ago):
                        self._unknown_done[days_ago] = True
                        parts_list.append({"raw_name": UNKNOWN_ITEM_DAYS[days_ago], "quantity": 1})
                    data = {"technician_name": tech_name, "format": "itemized", "parts_list": parts_list,
                            "package_description": ""}
                self.conn.execute("INSERT OR IGNORE INTO processed_invoices (invoice_id, email_id) VALUES (?, ?)",
                                  (invoice_id, f"cf-worker-{inv_no}"))
                self.conn.commit()
                _process_extracted_data(self.conn, data, invoice_id, invoice_name=invoice_name)
                self.summary["invoices"] += 1

    def unknown_tech_invoice(self):
        """A subcontractor invoice signed by someone not in Techs yet."""
        self.invoice_seq += 1
        data = {"technician_name": "Brandon K", "format": "itemized",
                "parts_list": [{"raw_name": "Rollers", "quantity": 10}, {"raw_name": "Lube", "quantity": 1}],
                "package_description": ""}
        _process_extracted_data(self.conn, data, f"<inv-{self.invoice_seq}@dispatch.example>",
                                invoice_name=f"Invoice_{self.invoice_seq}.pdf")

    def pwa_tech_day(self, day, days_ago):
        """Andre's day: Dispatch jobs show up as expected jobs; he closes each
        one in the PWA (charge + closing notes + receipt photo) and taps the
        parts he used. Older days only keep the parts deductions."""
        tech = self.tech(PWA_TECH)
        n = self.rng.randint(3, 5) if day.weekday() < 5 else self.rng.randint(1, 2)
        in_field_window = days_ago <= FIELD_OPS_DAYS
        jobs = []
        for j in range(n):
            wo = self._next_wo()
            jtype, items = self._pick_job()
            start = datetime.combine(day, datetime.min.time()) + timedelta(hours=8 + 2 * j)
            addr, city, zipc = self._address()
            if in_field_window:
                job_captures_mod.create_expected_job(self.conn, {
                    "wo_number": wo, "tech_id": tech["id"], "address": addr, "city": city, "state": "AZ",
                    "zip": zipc, "window_start": _fmt(start), "window_end": _fmt(start + timedelta(hours=2)),
                    "job_type": jtype, "warranty_company": "Winston Home Warranty",
                })
            jobs.append({"wo": wo, "type": jtype, "items": items, "start": start,
                         "address": f"{addr}, {city} AZ {zipc}"})

        is_today = days_ago == 0
        for idx, job in enumerate(jobs):
            if is_today and idx >= 2:
                continue  # the rest of today's route is still pending in the PWA
            ts = int(job["start"].timestamp() * 1000)
            for pid, qty in job["items"]:
                inventory.pwa_deduct(self.conn, tech["truck_id"], tech["warehouse_id"], pid, qty,
                                     tech["name"], ts=ts, wo_number=job["wo"] if in_field_window else "")
            pwa.mark_batch_processed(self.conn, tech["id"], f"ded-{job['wo']}", [
                {"part_id": p, "qty": q} for p, q in job["items"]])
            if not in_field_window:
                continue
            # A door sale every few days (20, 13, 6, 1 days ago): early enough
            # for the office to have ordered/installed some, late enough for
            # others to still be waiting.
            if self.door_specs and days_ago <= DOOR_SALE_DAYS[len(DOOR_ORDER_SPECS) - len(self.door_specs)]:
                spec = dict(self.door_specs.pop(0))
                final = spec.pop("_final")
                order, _st = door_orders_mod.create_door_order(self.conn, tech, dict(
                    spec, job_wo=job["wo"], customer_address=job["address"],
                    notes="Customer approved the quote on site."))
                if order:
                    self.door_orders.append((order["id"], final, day))
            uncaptured = (not is_today) and self.rng.random() < 0.04 and days_ago > 1
            if uncaptured:
                job["uncaptured"] = True
                continue
            self.capture(tech, job, day)
        return jobs

    def capture(self, tech, job, day):
        lo, hi = JOB_PRICES[job["type"]]
        not_collected = self.rng.random() < 0.07
        method = "not_collected" if not_collected else self.rng.choices(["card", "cash", "check"], [7, 2, 1])[0]
        amount = None if not_collected else float(self.rng.randrange(lo, hi, 5))
        self.capture_seq += 1
        batch_id = f"cap-{job['wo']}"
        photo = ""
        if self.assets_dir and not not_collected:
            from modules import demo_assets
            photo = demo_assets.write_receipt(self.assets_dir / "job_captures", batch_id, job, amount, method, day)
        job_captures_mod.create_job_capture(self.conn, tech, {
            "batch_id": batch_id, "wo_number": job["wo"], "payment_method": method,
            "amount_charged": amount,
            "check_number": str(self.rng.randint(1001, 4999)) if method == "check" else "",
            "not_collected_reason": "Homeowner disputes charge, will call Winston" if not_collected else "",
            "closing_notes": CLOSING_NOTES[job["type"]],
            "receipt_photo_path": photo,
            "captured_at": _fmt(job["start"] + timedelta(minutes=self.rng.randint(55, 110))),
        })
        job["captured"] = True
        job["amount"] = amount
        job["method"] = method
        self.summary["captures"] += 1

    def morning_load(self):
        """Each morning the techs top their trucks back up to par (2x min)
        from their warehouse, which is what drains warehouse stock and
        triggers Friday's distributor reorders."""
        for tid, _n, wid in TRUCKS:
            for r in self.conn.execute("SELECT part_id, quantity, min_threshold FROM stock WHERE truck_id=?",
                                       (tid,)).fetchall():
                par = max(r["min_threshold"] * 2, 1)
                if r["quantity"] < r["min_threshold"] or r["quantity"] < 0:
                    inventory.restock(self.conn, tid, r["part_id"], par - r["quantity"], wid, source="bulk_restock")

    def friday_reorders(self, send_emails=True):
        rows = reorder_mod.find_low_warehouse_stock(self.conn)
        by_dist = {}
        for r in rows:
            if reorder_mod.has_pending_reorder(self.conn, r["part_id"], r["warehouse_id"]):
                continue
            qty = reorder_mod.compute_qty(r["min_threshold"], r["quantity"])
            reorder_mod.create_reorder(self.conn, r["part_id"], r["active_distributor_id"] or "", r["warehouse_id"], qty)
            by_dist.setdefault((r["active_distributor_id"], r["active_distributor_email"]), []).append(r)
        for (did, email), items in by_dist.items():
            if not send_emails or not did:
                continue
            log_email(self.conn, "reorder", f"DoorFlex purchase order - {len(items)} item(s)", email, "sent",
                      distributor_id=did, attempts=1, response="demo: simulated send")
            self.conn.execute("UPDATE reorder_requests SET status='sent' WHERE distributor_id=? AND status='pending'", (did,))
        if by_dist and send_emails:
            log_email(self.conn, "weekly_alert", "DoorFlex weekly low-stock alert", "office@doorflex.example", "sent",
                      attempts=1, response="demo: simulated send")
        self.conn.commit()

    def receive_reorders(self):
        """Tuesday: last Friday's distributor orders arrive at the warehouse."""
        for r in self.conn.execute("SELECT * FROM reorder_requests WHERE status IN ('pending','sent')").fetchall():
            cur = self.conn.execute("SELECT quantity FROM warehouse_stock WHERE warehouse_id=? AND part_id=?",
                                    (r["warehouse_id"], r["part_id"])).fetchone()
            if cur is None:
                continue
            inventory.adjust(self.conn, "warehouse", r["warehouse_id"], r["part_id"], cur["quantity"] + r["qty"],
                             reason="distributor_delivery")
            reorder_mod.resolve_reorder(self.conn, r["id"], mode="manual")

    def reconciliation(self, day):
        result = job_captures_mod.compute_reconciliation(self.conn, day.isoformat())
        if any(result[k] for k in ("matched", "not_collected", "uncaptured", "removed")):
            job_captures_mod.save_reconciliation_run(self.conn, result, report_sent=True)
            self.conn.execute("UPDATE reconciliation_runs SET report_sent_at=? WHERE id=(SELECT MAX(id) FROM reconciliation_runs)",
                              (f"{day.isoformat()} 17:30:00",))
            self.conn.commit()


def _winston_parsed(week_jobs, period_start, period_end, rng, ocr_slip=False):
    """What Winston's weekly payment report says. Winston pays 60% of the sale
    to DoorFlex as payout; DoorFlex's commission is 50% of payout. Jobs Winston
    removed from Dispatch are simply missing — that's the money the app finds."""
    rows = []
    for j in week_jobs:
        sales = j["amount"]
        payout = round(sales * 0.6, 2)
        rows.append({"date": j["start"].date().isoformat(), "wo_number": j["wo"], "total_sales": sales,
                     "payout": payout, "commission": round(payout * 0.5, 2), "notes": None})
    if ocr_slip and rows:
        # A realistic vision misread for the review screen to flag.
        rows[1 if len(rows) > 1 else 0]["wo_number"] = rows[1 if len(rows) > 1 else 0]["wo_number"][:-1] + "O"
    parts = round(sum(rng.choice([18, 24, 31, 42]) for _ in rows[: max(1, len(rows) // 3)]), 2)
    sub_sales = round(sum(r["total_sales"] for r in rows), 2)
    sub_payout = round(sum(r["payout"] for r in rows), 2)
    total_comm = round(sum(r["commission"] for r in rows) - parts, 2)
    return {"tech_name": PWA_TECH, "period_start": period_start.isoformat(), "period_end": period_end.isoformat(),
            "jobs": rows, "subtotal_sales": sub_sales, "subtotal_payout": sub_payout,
            "parts_deduction": parts, "total_commission": total_comm}


def build_demo(conn, today=None, seed=2026, assets_dir=None, pwa_token=DEMO_PWA_TOKEN_DEFAULT):
    """Populate an EMPTY, schema-initialized DB. Returns a summary dict."""
    today = today or date.today()
    w = _World(conn, today, seed, assets_dir)
    w.setup()
    pwa.set_tech_token(conn, w.tech_rows[PWA_TECH], pwa_token)

    first = today - timedelta(days=HISTORY_DAYS)
    field_jobs = []          # PWA jobs inside the field-ops window
    removed_plan = set()     # days_ago values where Winston deletes a job
    for d in (19, 11, 4):
        removed_plan.add(d)

    day = first
    while day <= today:
        days_ago = (today - day).days
        marks = _marks(conn)
        w.current_day = day
        _advance_door_orders(w)
        if day.weekday() != 6 and days_ago > 0:
            w.morning_load()
        if day.weekday() == 1:
            w.receive_reorders()
        if day.weekday() != 6:
            w.email_tech_invoices(day, days_ago)
            jobs = w.pwa_tech_day(day, days_ago)
            if days_ago <= FIELD_OPS_DAYS:
                field_jobs.extend(jobs)
                if days_ago in removed_plan:
                    victim = next((j for j in jobs if j.get("captured") and j.get("method") != "not_collected"), None)
                    if victim:
                        victim["removed"] = True
                        ej = conn.execute("SELECT id FROM expected_jobs WHERE wo_number=?", (victim["wo"],)).fetchone()
                        job_captures_mod.mark_removed_by_dispatch(conn, ej["id"])
                        w.summary["disputes"] += 1
            if days_ago == 6:
                w.unknown_tech_invoice()
        if day.weekday() == 4:
            w.friday_reorders(send_emails=days_ago > 0)
        if days_ago <= 14 and days_ago > 0 and day.weekday() != 6:
            w.reconciliation(day)
        _stamp_day(conn, marks, day)
        day += timedelta(days=1)

    _winston_reports(w, field_jobs)
    # The office works through shortfalls as they happen; only this week's
    # are still open, like a real Monday morning.
    conn.execute("UPDATE stock_alerts SET resolved=1 WHERE created_at < ?",
                 ((today - timedelta(days=5)).isoformat(),))
    _dispute_statuses(conn)
    _seed_misc_events(w)
    conn.commit()
    w.summary["pwa_token"] = pwa_token
    return w.summary


DOOR_SALE_DAYS = [20, 13, 6, 1]

DOOR_ORDER_SPECS = [
    {"brand": "Clopay", "model": "coachman", "model_label": "Coachman", "series_design": "Design 12 - REC13",
     "insulation": "Intellicore", "width": "16'", "height": "7'", "windows": True, "window_style": "SQ24",
     "glass_type": "Clear", "color_base": "Standard White", "construction": "Steel + composite overlay",
     "insulated": True, "_final": "installed"},
    {"brand": "Amarr", "model": "hillcrest", "model_label": "Hillcrest", "series_design": "Short panel",
     "insulation": "Polystyrene", "width": "9'", "height": "7'", "windows": False, "color_base": "Almond",
     "construction": "Steel", "insulated": True, "_final": "ordered"},
    {"brand": "Clopay", "model": "avante", "model_label": "Avante", "series_design": "Full view aluminum",
     "width": "16'", "height": "8'", "windows": True, "glass_type": "Satin", "color_base": "Black anodized",
     "construction": "Aluminum", "_final": "captured"},
    {"brand": "Other", "model": "other", "model_label": "Wayne Dalton 9100", "series_design": "Long panel",
     "width": "8'", "height": "7'", "windows": False, "color_base": "White", "construction": "Steel",
     "_final": "captured"},
]


def _advance_door_orders(w):
    """Office side of the door workflow: Maria orders a captured door two
    days later and marks it installed about a week after that."""
    for order_id, final, captured_day in list(w.door_orders):
        age = (w.current_day - captured_day).days
        row = w.conn.execute("SELECT status FROM door_orders WHERE id=?", (order_id,)).fetchone()
        if final in ("ordered", "installed") and row["status"] == "captured" and age >= 2:
            door_orders_mod.set_status(w.conn, order_id, "ordered", "Maria")
        if final == "installed" and row["status"] == "ordered" and age >= 8:
            door_orders_mod.set_status(w.conn, order_id, "installed", "Maria")


def _winston_reports(w, field_jobs):
    """One weekly report per completed week in the window: confirmed, except
    the most recent which is waiting for human review (with a vision slip)."""
    conn = w.conn
    today = w.today
    last_sat = today - timedelta(days=(today.weekday() - 5) % 7 or 7)
    weeks = []
    for k in range(3, -1, -1):
        end = last_sat - timedelta(weeks=k)
        start = end - timedelta(days=5)
        if (today - start).days > FIELD_OPS_DAYS:
            continue
        weeks.append((start, end))
    for i, (start, end) in enumerate(weeks):
        week_jobs = [j for j in field_jobs
                     if start <= j["start"].date() <= end and j.get("captured")
                     and j.get("method") != "not_collected" and not j.get("removed")]
        # Winston also reports one job late: drop the last one of the week.
        late = week_jobs[-1:] if i < len(weeks) - 1 else []
        reported = [j for j in week_jobs if j not in late]
        if not reported:
            continue
        is_latest = i == len(weeks) - 1
        parsed = _winston_parsed(reported, start, end, w.rng, ocr_slip=is_latest)
        image_name = ""
        if w.assets_dir:
            from modules import demo_assets
            image_name = demo_assets.write_winston_report(w.assets_dir / "winston_reports",
                                                          f"report-{end.isoformat()}", parsed)
        marks = _marks(conn)
        report_id = winston_reports_mod.create_draft(conn, parsed, image_name)
        received = end + timedelta(days=2)
        if not is_latest:
            winston_reports_mod.confirm_draft(conn, report_id, parsed["jobs"], {
                k: parsed[k] for k in ("tech_name", "period_start", "period_end", "subtotal_sales",
                                       "subtotal_payout", "parts_deduction", "total_commission")})
        _stamp_day(conn, marks, min(received, today), start_hour=9, span_hours=1)
        if is_latest:
            conn.execute("UPDATE winston_reports SET processed_at='' WHERE id=?", (report_id,))
        conn.commit()
        w.summary["reports"] += 1


def _dispute_statuses(conn):
    rows = conn.execute("SELECT id FROM disputes ORDER BY id").fetchall()
    notes = [
        ("resolved", "Winston re-added the job after we sent the receipt photo + closing notes. Paid on next report."),
        ("sent", "Sent capture evidence (receipt + notes + timestamps) to Winston dispatch. Waiting on reply."),
    ]
    for row, (status, note) in zip(rows, notes):
        conn.execute("UPDATE disputes SET status=?, notes=? WHERE id=?", (status, note, row["id"]))
    conn.commit()


def _seed_misc_events(w):
    """A couple of operational events the Observe page would normally show."""
    from modules import events as events_mod
    conn = w.conn
    marks = _marks(conn)
    events_mod.log_event(conn, "info", "system", "demo.seeded",
                         "Demo dataset generated (synthetic data, resets nightly)",
                         detail={k: v for k, v in w.summary.items() if k != "pwa_token"})
    conn.commit()
    _stamp_day(conn, marks, w.today, start_hour=3, span_hours=0)
