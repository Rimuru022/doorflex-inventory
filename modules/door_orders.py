"""Door order capture — NOT an inventory deduction.

A tech sells a garage door in the field and captures the spec on the PWA so
the office (Nick + Maria) gets notified immediately and can place the actual
order with the distributor. This module never touches `stock`/`log`/
`stock_alerts` — door orders are a fully separate table and workflow.

Catalog data (which models/sizes/insulations are valid, and how to compute a
real distributor item number) is loaded from static JSON files under
data/door_catalog/, extracted from the manufacturer's official price books.
Any brand/model without a JSON file here falls back to a generic, structured
(non-catalog) capture — see `generic_form_options()`.
"""
import json
from pathlib import Path

from modules import events as events_mod
from modules import job_captures as job_captures_mod

CATALOG_DIR = Path(__file__).parent.parent / "door_catalog"

BRANDS = ["Clopay", "Amarr", "Other"]

# brand -> {model_key: {"label": ..., "flow": "coachman"|"avante"|"generic"}}
# Canyon Ridge is a real, named Clopay collection (per the approved plan's
# wizard order: "Coachman / Avante / Canyon Ridge* / Other") but has no price
# book on file yet, so it uses the generic structured cascade rather than the
# catalog-driven one — it's listed here (flow="generic") so it shows up as its
# own button in the Model step instead of being buried under "Other".
CLOPAY_MODELS = {
    "coachman": {"label": "Coachman", "flow": "coachman"},
    "avante": {"label": "Avante", "flow": "avante"},
    "canyon_ridge": {"label": "Canyon Ridge", "flow": "generic"},
}

# Amarr collections, built from amarr.json's "collections" (flow="amarr",
# design/insulation/window/color driven, see list_designs/list_insulations
# below) and "aluminum_full_view" (flow="amarr_aluminum" — commercial
# full-view doors, structurally like Avante: no insulation step, model
# variant doubles as the design step). See amarr.json's "_source" note —
# built from public Amarr spec pages, not a Nick-provided price book yet.
AMARR_MODELS = {
    "classica": {"label": "Classica", "flow": "amarr"},
    "classica_full_view": {"label": "Classica Full View", "flow": "amarr"},
    "classica_flush": {"label": "Classica Flush", "flow": "amarr"},
    "carriage_court": {"label": "Carriage Court", "flow": "amarr"},
    "designers_choice": {"label": "Designer's Choice", "flow": "amarr"},
    "hillcrest": {"label": "Hillcrest", "flow": "amarr"},
    "heritage": {"label": "Heritage", "flow": "amarr"},
    "olympus": {"label": "Olympus", "flow": "amarr"},
    "northwoods": {"label": "Northwoods", "flow": "amarr"},
    "3552": {"label": "3552 — Aluminum Full View", "flow": "amarr_aluminum"},
    "3502": {"label": "3502 — Aluminum Full View", "flow": "amarr_aluminum"},
}

_catalog_cache = {}


def _load(model_key):
    if model_key in _catalog_cache:
        return _catalog_cache[model_key]
    path = CATALOG_DIR / f"{model_key}.json"
    if not path.exists():
        _catalog_cache[model_key] = None
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _catalog_cache[model_key] = data
    return data


def flow_for_model(model_key):
    """Returns 'coachman', 'avante', 'amarr', 'amarr_aluminum', or 'generic'."""
    entry = CLOPAY_MODELS.get(model_key) or AMARR_MODELS.get(model_key)
    return entry["flow"] if entry else "generic"


def _amarr_collection(model_key):
    data = _load("amarr")
    return data["collections"].get(model_key) if data else None


def _amarr_aluminum(model_key):
    data = _load("amarr")
    return data["aluminum_full_view"].get(model_key) if data else None


def list_models(brand):
    """Structured models available for a brand. Clopay and Amarr get their
    catalog-backed models plus a generic fallback; Other only gets the
    generic fallback."""
    models = []
    if brand == "Clopay":
        for key, meta in CLOPAY_MODELS.items():
            models.append({"key": key, "label": meta["label"], "flow": meta["flow"]})
    elif brand == "Amarr":
        for key, meta in AMARR_MODELS.items():
            models.append({"key": key, "label": meta["label"], "flow": meta["flow"]})
    models.append({"key": "other", "label": "Other / Not listed", "flow": "generic"})
    return models


def list_designs(model_key):
    """Coachman: Series/Design codes. Avante: model variants (the closest
    equivalent to a design step). Amarr: the collection's named panel
    designs. Amarr aluminum (3502/3552): panel construction options (the
    closest equivalent to a design step, mirroring Avante). Anything else:
    empty."""
    flow = flow_for_model(model_key)
    if flow == "coachman":
        data = _load("coachman")
        return [
            {"code": d["design"], "series": d["series"], "windows_allowed": d["windows_allowed"]}
            for d in data["designs"]
        ]
    if flow == "avante":
        data = _load("avante")
        return [
            {"code": m["code"], "series": None, "windows_allowed": False}
            for m in data["models"]
        ]
    if flow == "amarr":
        collection = _amarr_collection(model_key)
        if not collection:
            return []
        windows_allowed = collection.get("windows", {}).get("allowed", False)
        return [
            {"code": d, "series": None, "windows_allowed": windows_allowed}
            for d in collection.get("designs", [])
        ]
    if flow == "amarr_aluminum":
        model = _amarr_aluminum(model_key)
        if not model:
            return []
        return [
            {"code": p, "series": None, "windows_allowed": False}
            for p in model.get("panel_options", [])
        ]
    return []


def list_insulations(model_key, design_code):
    flow = flow_for_model(model_key)
    if flow == "coachman":
        data = _load("coachman")
        design = next((d for d in data["designs"] if d["design"] == design_code), None)
        if not design:
            return []
        return [
            {"code": code, "label": data["insulations"][code]["label"]}
            for code in design["insulations"]
        ]
    if flow == "amarr":
        # Insulation options are collection-wide in Amarr's public spec pages
        # (unlike Coachman, they don't vary by the chosen panel design).
        collection = _amarr_collection(model_key)
        if not collection:
            return []
        return [
            {"code": ins["code"], "label": f"{ins['label']} (R-{ins['r_value']})" if ins["r_value"] else ins["label"]}
            for ins in collection.get("insulations", [])
        ]
    return []


def _height_list(min_in, max_in, step_in):
    heights = []
    v = min_in
    while v <= max_in:
        ft, inch = divmod(v, 12)
        heights.append(f"{ft}'{inch}\"" if inch else f"{ft}'0\"")
        v += step_in
    return heights


def list_sizes(model_key, design_code, insulation_code=None):
    flow = flow_for_model(model_key)
    if flow == "coachman":
        data = _load("coachman")
        design = next((d for d in data["designs"] if d["design"] == design_code), None)
        if not design:
            return {"widths": [], "heights": []}
        group = data["width_groups"][design["width_group"]]
        insulation_tier = "CD" if insulation_code == "CD" else "CG_CGU"
        widths = group.get(insulation_tier, [])
        rule = data["height_rules"]["series_2" if design["series"] == 2 else "series_1_3_4"]
        heights = _height_list(rule["min_in"], rule["max_in"], rule["step_in"])
        return {"widths": widths, "heights": heights}
    if flow == "avante":
        data = _load("avante")
        model = next((m for m in data["models"] if m["code"] == design_code), None)
        if not model:
            return {"widths": [], "heights": []}
        width_rule = data["width_rule"]
        widths = []
        v_in = width_rule["min_ft"] * 12
        max_in = model["width_max_ft"] * 12
        while v_in <= max_in:
            ft, inch = divmod(v_in, 12)
            widths.append(f"{ft}'{inch}\"" if inch else f"{ft}'0\"")
            v_in += width_rule["step_in"]
        hr = data["height_rule"]
        heights = _height_list(hr["min_in"], hr["max_in"], hr["step_in"])
        return {"widths": widths, "heights": heights}
    if flow == "amarr":
        # No confirmed per-collection size chart from public sources — uses
        # the same standard US garage-door increments as amarr.json's
        # top-level size_rule (flagged _verify_with_nick there).
        data = _load("amarr")
        rule = data["size_rule"]
        return {"widths": rule["widths"], "heights": rule["heights"]}
    if flow == "amarr_aluminum":
        model = _amarr_aluminum(model_key)
        if not model or not model.get("width_max_in") or not model.get("height_max_in"):
            return {"widths": [], "heights": []}
        widths = _height_list(72, model["width_max_in"], 12)
        heights = _height_list(72, model["height_max_in"], 12)
        return {"widths": widths, "heights": heights}
    return {"widths": [], "heights": []}


def window_info(model_key, design_code, insulation_code=None):
    flow = flow_for_model(model_key)
    if flow == "coachman":
        data = _load("coachman")
        design = next((d for d in data["designs"] if d["design"] == design_code), None)
        if not design or not design["windows_allowed"]:
            return {"allowed": False, "styles": [], "glass_types": []}
        glass_types = data["windows"]["glass_types"]
        if insulation_code:
            glass_types = [g for g in glass_types if insulation_code in g["insulations"]]
        return {"allowed": True, "styles": data["windows"]["styles"], "glass_types": glass_types}
    if flow == "amarr":
        collection = _amarr_collection(model_key)
        windows = (collection or {}).get("windows", {})
        if not windows.get("allowed"):
            return {"allowed": False, "styles": [], "glass_types": []}
        # amarr.json stores styles/glass_types as plain strings (unlike
        # coachman.json's {code,label[,insulations]} objects) — normalize so
        # the PWA's `s.code`/`s.label` lookups don't render "undefined".
        styles = [{"code": s, "label": s} for s in windows.get("styles", [])]
        glass_types = [{"code": g, "label": g} for g in windows.get("glass_types", [])]
        return {"allowed": True, "styles": styles, "glass_types": glass_types}
    # amarr_aluminum doors are inherently full-view glass — glazing is chosen
    # via list_colors' glass_types, not a separate window add-on step.
    return {"allowed": False, "styles": [], "glass_types": []}


def list_colors(model_key):
    flow = flow_for_model(model_key)
    if flow == "coachman":
        return _load("coachman")["colors"]
    if flow == "avante":
        return _load("avante")["colors"]
    if flow == "amarr":
        collection = _amarr_collection(model_key)
        colors = (collection or {}).get("colors", {})
        return colors.get("known", []) + ["Other (see Amarr Color Zone)"]
    if flow == "amarr_aluminum":
        model = _amarr_aluminum(model_key)
        colors = (model or {}).get("colors", {})
        return colors.get("anodized", []) + colors.get("powder_coat", [])
    return ["White", "Almond", "Desert Tan", "Sandtone", "Other"]


def compute_item_number(model_key, design_code, insulation_code=None):
    flow = flow_for_model(model_key)
    if flow == "coachman" and insulation_code and design_code:
        return f"{insulation_code}{design_code}"
    if flow == "avante" and design_code:
        return design_code
    return ""


# Quote-required models: Nick must get a distributor quote before the office
# can quote the customer (high-value/rare lines). 3502/3552 are commercial
# aluminum full-view doors (storefronts, service stations) — rare and
# high-value like Avante/Canyon Ridge, same reasoning.
QUOTE_REQUIRED_MODELS = {"avante", "canyon_ridge", "3502", "3552"}


def needs_distributor_quote(model_key):
    return model_key in QUOTE_REQUIRED_MODELS


def generic_form_options():
    """Static options for the non-catalog fallback form (any brand/model we
    don't have a JSON catalog for — Canyon Ridge, "Other", or "Other / Not
    listed" under any brand)."""
    return {
        "construction": ["Steel", "Wood", "Aluminum-Glass", "Composite"],
        "sizes": ["8x7", "9x7", "16x7", "8x8", "16x8", "18x7"],
        "window_styles": ["Arch", "Square", "Rectangular", "Full-view", "None"],
        "colors": ["White", "Almond", "Desert Tan", "Sandtone", "Other"],
    }


# ─── Persistence ────────────────────────────────────────────────────────────


def get_by_job_wo(conn, job_wo):
    row = conn.execute("SELECT * FROM door_orders WHERE job_wo=?", (job_wo,)).fetchone()
    return dict(row) if row else None


def get_orders_by_wo(conn, wo_numbers):
    """Door orders keyed by job_wo, for the given list — backs a "Doors"
    column on the jobs-today dashboard alongside get_captures_by_wo/
    get_parts_used_by_wo. A WO with no door order just won't have a key
    here (most WOs aren't door jobs)."""
    wo_numbers = [w for w in (wo_numbers or []) if w]
    if not wo_numbers:
        return {}
    placeholders = ",".join("?" for _ in wo_numbers)
    rows = conn.execute(
        f"SELECT * FROM door_orders WHERE job_wo IN ({placeholders})", wo_numbers,
    ).fetchall()
    return {r["job_wo"]: dict(r) for r in rows}


def create_door_order(conn, tech, payload):
    """Idempotent on job_wo: a retry with the same job_wo returns the existing
    row instead of creating a duplicate (mirrors the deduction_batches pattern
    used for stock deductions, but keyed on job_wo since that's the natural
    dedup key here — a tech only submits one order per job)."""
    job_wo = str(payload.get("job_wo", "")).strip()
    if not job_wo:
        return None, "missing_job_wo"

    existing = get_by_job_wo(conn, job_wo)
    if existing:
        return existing, "already_processed"

    if job_captures_mod.is_wo_closed(conn, job_wo):
        return None, "wo_already_closed"

    model_key = str(payload.get("model", "")).strip()
    generic = flow_for_model(model_key) == "generic"

    cur = conn.execute(
        """INSERT INTO door_orders (
            job_wo, tech_id, tech_name, brand, model, model_label, series_design,
            insulation, item_number, width, height, windows, window_style,
            glass_type, color_base, color_overlay, construction, insulated,
            customer_address, notes, needs_distributor_quote, generic_path, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'captured')""",
        (
            job_wo,
            tech["id"],
            tech["name"],
            str(payload.get("brand", "")).strip(),
            model_key,
            str(payload.get("model_label", "")).strip(),
            str(payload.get("series_design", "")).strip(),
            str(payload.get("insulation", "")).strip(),
            str(payload.get("item_number", "")).strip(),
            str(payload.get("width", "")).strip(),
            str(payload.get("height", "")).strip(),
            1 if payload.get("windows") else 0,
            str(payload.get("window_style", "")).strip(),
            str(payload.get("glass_type", "")).strip(),
            str(payload.get("color_base", "")).strip(),
            str(payload.get("color_overlay", "")).strip(),
            str(payload.get("construction", "")).strip(),
            1 if payload.get("insulated") else 0,
            str(payload.get("customer_address", "")).strip(),
            str(payload.get("notes", "")).strip(),
            1 if needs_distributor_quote(model_key) else 0,
            1 if generic else 0,
        ),
    )
    conn.commit()
    order = get_by_job_wo(conn, job_wo)
    events_mod.log_event(
        conn, "info", "pwa", "door_order.captured",
        f"Door order captured by {tech['name']}: {order['item_number'] or order['model_label'] or model_key} (WO {job_wo})",
        detail={"tech": tech["name"], "job_wo": job_wo, "model": model_key},
        ref=job_wo,
    )
    conn.commit()
    return order, "processed"


def list_pending(conn):
    rows = conn.execute(
        "SELECT * FROM door_orders WHERE status='captured' ORDER BY captured_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def list_all(conn):
    rows = conn.execute("SELECT * FROM door_orders ORDER BY captured_at DESC").fetchall()
    return [dict(r) for r in rows]


VALID_TRANSITIONS = {
    "captured": {"ordered", "cancelled"},
    "ordered": {"installed", "cancelled"},
    "installed": set(),
    "cancelled": set(),
}


def cancel_by_tech(conn, tech, job_wo):
    """Tech-facing cancel: only their own order, only while still 'captured'
    (once the office has ordered/installed it, only staff can change status —
    this is stricter than the general VALID_TRANSITIONS state machine, which
    would otherwise also allow 'ordered' -> 'cancelled')."""
    order = get_by_job_wo(conn, job_wo)
    if not order:
        return None, "not_found"
    if order["tech_id"] != tech["id"]:
        return None, "not_owner"
    if order["status"] != "captured":
        return None, "invalid_transition"
    return set_status(conn, order["id"], "cancelled", tech["name"])


def set_status(conn, order_id, new_status, actor):
    order = conn.execute("SELECT * FROM door_orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        return None, "not_found"
    if new_status not in VALID_TRANSITIONS.get(order["status"], set()):
        return None, "invalid_transition"
    conn.execute(
        "UPDATE door_orders SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
        (new_status, order_id),
    )
    conn.commit()
    events_mod.log_event(
        conn, "info", "door_orders", "door_order.status_changed",
        f"Door order {order['job_wo']} → {new_status} (by {actor})",
        detail={"job_wo": order["job_wo"], "from": order["status"], "to": new_status, "actor": actor},
        ref=order["job_wo"],
    )
    conn.commit()
    return get_by_job_wo(conn, order["job_wo"]), None
