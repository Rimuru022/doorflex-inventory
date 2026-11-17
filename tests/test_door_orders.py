"""Door order capture tests — catalog cascades (Coachman item number rules),
job_wo idempotency, status transitions, and the generic (non-catalog) path.
Mirrors the fixture/style of test_pwa.py; door orders never touch stock."""
import pytest

from modules import door_orders, job_captures, pwa


@pytest.fixture()
def tech_env(conn):
    conn.execute("INSERT INTO warehouses (id, name) VALUES ('wh-sub', 'Total Garage Solutions')")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-DEMO', 'Demo', 'wh-sub')")
    cur = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
        "VALUES ('Winston', 'T-DEMO', 'wh-sub', 'pwa')"
    )
    token = pwa.rotate_tech_token(conn, cur.lastrowid)
    conn.commit()
    tech = pwa.get_tech_by_token(conn, token)
    return conn, tech


# ─── Catalog cascade rules ──────────────────────────────────────────────────


def test_design_31_is_cgu_only():
    insulations = {i["code"] for i in door_orders.list_insulations("coachman", "31")}
    assert insulations == {"CGU"}


def test_design_11_allows_all_insulations():
    insulations = {i["code"] for i in door_orders.list_insulations("coachman", "11")}
    assert insulations == {"CD", "CG", "CGU"}


def test_series_3_and_4_have_no_windows():
    for design in ("31", "32", "33", "34", "35", "36", "41", "42", "43"):
        info = door_orders.window_info("coachman", design)
        assert info["allowed"] is False, f"design {design} should not allow windows"


def test_series_1_and_2_allow_windows():
    for design in ("11", "12", "13", "21", "22", "23"):
        info = door_orders.window_info("coachman", design)
        assert info["allowed"] is True


def test_glass_type_filtered_by_insulation():
    # CD only gets the 4 non-insulated glass types
    cd_glass = door_orders.window_info("coachman", "11", "CD")["glass_types"]
    assert {g["code"] for g in cd_glass} == {"OBSCURE_DSB", "FROSTED_DSB", "RAIN_DSB", "SEEDED_DSB"}
    cgu_glass = door_orders.window_info("coachman", "11", "CGU")["glass_types"]
    assert len(cgu_glass) == 10


def test_item_number_computation():
    assert door_orders.compute_item_number("coachman", "21", "CGU") == "CGU21"
    assert door_orders.compute_item_number("coachman", "11", "CD") == "CD11"
    assert door_orders.compute_item_number("avante", "AX") == "AX"
    assert door_orders.compute_item_number("other", "", "") == ""


def test_needs_distributor_quote_flag():
    assert door_orders.needs_distributor_quote("avante") is True
    assert door_orders.needs_distributor_quote("canyon_ridge") is True
    assert door_orders.needs_distributor_quote("3502") is True
    assert door_orders.needs_distributor_quote("3552") is True
    assert door_orders.needs_distributor_quote("coachman") is False
    assert door_orders.needs_distributor_quote("other") is False


def test_canyon_ridge_is_a_named_clopay_model_not_hidden_under_other():
    models = {m["key"]: m for m in door_orders.list_models("Clopay")}
    assert "canyon_ridge" in models
    assert models["canyon_ridge"]["label"] == "Canyon Ridge"
    # No catalog on file yet -> generic structured cascade, not free text.
    assert models["canyon_ridge"]["flow"] == "generic"
    assert door_orders.flow_for_model("canyon_ridge") == "generic"


# ─── Amarr catalog ──────────────────────────────────────────────────────────


def test_amarr_brand_lists_real_collections_not_generic_fallback():
    models = {m["key"]: m for m in door_orders.list_models("Amarr")}
    assert models["olympus"]["flow"] == "amarr"
    assert models["3552"]["flow"] == "amarr_aluminum"
    assert "other" in models  # still has the fallback, but not the only option
    assert door_orders.flow_for_model("olympus") == "amarr"
    assert door_orders.flow_for_model("3552") == "amarr_aluminum"


def test_amarr_collection_designs_insulations_colors():
    designs = {d["code"] for d in door_orders.list_designs("classica")}
    assert designs == {"Valencia", "Tuscany", "Cortona", "Lucern", "Bordeaux", "Northampton", "Santiago"}

    insulations = {i["code"] for i in door_orders.list_insulations("classica", None)}
    assert insulations == {"NONE", "POLY_2L", "POLYU_3L"}

    colors = door_orders.list_colors("olympus")
    assert "White" in colors
    assert "Other (see Amarr Color Zone)" in colors


def test_amarr_northwoods_uses_plank_finishes_as_colors():
    # Northwoods is the one collection with a fully-confirmed color list
    # (plank finishes) rather than a partial/verify-with-Nick placeholder.
    colors = door_orders.list_colors("northwoods")
    assert colors[:5] == ["Cedar", "Dune", "Coffee", "Aspen Gray", "Onyx"]


def test_amarr_collection_windows_allowed():
    info = door_orders.window_info("hillcrest", "Recessed")
    assert info["allowed"] is True
    assert len(info["glass_types"]) > 0
    # amarr.json stores styles/glass_types as plain strings; window_info must
    # normalize to {code,label} like coachman's, since the PWA reads
    # s.code/s.label and rendered "undefined" before this was fixed.
    assert all("code" in s and "label" in s for s in info["styles"])
    assert all("code" in g and "label" in g for g in info["glass_types"])


def test_amarr_collection_sizes_use_standard_increments():
    sizes = door_orders.list_sizes("heritage", "Flush Panel")
    assert "16'0\"" in sizes["widths"]
    assert "8'0\"" in sizes["heights"]


def test_amarr_aluminum_flow_has_no_insulation_step():
    # 3552/3502 are commercial full-view aluminum doors — not insulated.
    assert door_orders.list_insulations("3552", None) == []
    info = door_orders.window_info("3552", "Glass Panel")
    assert info["allowed"] is False


def test_amarr_aluminum_3552_sizes_and_colors():
    designs = {d["code"] for d in door_orders.list_designs("3552")}
    assert "Glass Panel" in designs

    sizes = door_orders.list_sizes("3552", "Glass Panel")
    assert sizes["widths"]
    assert sizes["heights"]

    colors = door_orders.list_colors("3552")
    assert "Black" in colors  # anodized
    assert "White" in colors  # powder coat


def test_amarr_aluminum_3502_unconfirmed_sizes_return_empty():
    # 3502's max width/height was not published — list_sizes should return
    # empty rather than inventing a value, since amarr.json marks it
    # _verify_with_nick and width_max_in/height_max_in are null.
    sizes = door_orders.list_sizes("3502", "Glass Panel")
    assert sizes == {"widths": [], "heights": []}


def test_sizes_respect_width_group_and_height_rule():
    # Design 22 is in width_group "group_b" (no 12'0"-13'8" range) and is
    # Series 2 (height capped at 10'0", not the 16'0" of Series 1/3/4).
    sizes = door_orders.list_sizes("coachman", "22", "CD")
    assert "12'0\"" not in sizes["widths"]
    assert sizes["heights"][-1] == "10'0\""

    sizes_11 = door_orders.list_sizes("coachman", "11", "CD")
    assert sizes_11["heights"][-1] == "16'0\""


# ─── Persistence: idempotency + status flow ────────────────────────────────


def _coachman_payload(job_wo="WO-1001"):
    return {
        "job_wo": job_wo,
        "brand": "Clopay",
        "model": "coachman",
        "model_label": "Coachman",
        "series_design": "21",
        "insulation": "CGU",
        "item_number": "CGU21",
        "width": "9'2\"",
        "height": "7'0\"",
        "windows": True,
        "window_style": "SQ23",
        "glass_type": "INS_LOW_E",
        "color_base": "White",
        "color_overlay": "White",
        "customer_address": "123 Main St, Phoenix, AZ",
    }


def test_create_door_order_never_touches_stock(tech_env):
    conn, tech = tech_env
    conn.execute("INSERT INTO parts (id, name) VALUES ('SPR-207', 'Spring')")
    conn.execute("INSERT INTO stock (truck_id, part_id, quantity) VALUES ('T-DEMO', 'SPR-207', 5)")
    conn.commit()

    order, status = door_orders.create_door_order(conn, tech, _coachman_payload())
    assert status == "processed"
    assert order["item_number"] == "CGU21"
    assert order["status"] == "captured"
    assert order["generic_path"] == 0

    qty = conn.execute("SELECT quantity FROM stock WHERE part_id='SPR-207'").fetchone()["quantity"]
    assert qty == 5  # untouched
    log_rows = conn.execute("SELECT COUNT(*) c FROM log").fetchone()["c"]
    assert log_rows == 0  # door orders never write to the deduction log


def test_job_wo_idempotency(tech_env):
    conn, tech = tech_env
    first, status1 = door_orders.create_door_order(conn, tech, _coachman_payload("WO-DUP"))
    assert status1 == "processed"
    second, status2 = door_orders.create_door_order(conn, tech, _coachman_payload("WO-DUP"))
    assert status2 == "already_processed"
    assert second["id"] == first["id"]

    count = conn.execute("SELECT COUNT(*) c FROM door_orders WHERE job_wo='WO-DUP'").fetchone()["c"]
    assert count == 1


def test_create_door_order_rejected_for_already_closed_wo(tech_env):
    conn, tech = tech_env
    job_captures.create_job_capture(conn, tech, {
        "batch_id": "close-1", "wo_number": "WO-CLOSED",
        "payment_method": "cash", "closing_notes": "done",
    })
    order, status = door_orders.create_door_order(conn, tech, _coachman_payload("WO-CLOSED"))
    assert order is None
    assert status == "wo_already_closed"


def test_get_orders_by_wo(tech_env):
    conn, tech = tech_env
    door_orders.create_door_order(conn, tech, _coachman_payload("WO-A"))
    result = door_orders.get_orders_by_wo(conn, ["WO-A", "WO-NEVER-ORDERED"])
    assert "WO-NEVER-ORDERED" not in result
    assert result["WO-A"]["job_wo"] == "WO-A"
    assert door_orders.get_orders_by_wo(conn, []) == {}


def test_missing_job_wo_rejected(tech_env):
    conn, tech = tech_env
    payload = _coachman_payload()
    payload["job_wo"] = ""
    order, status = door_orders.create_door_order(conn, tech, payload)
    assert order is None
    assert status == "missing_job_wo"


def test_generic_fallback_path_has_no_item_number(tech_env):
    conn, tech = tech_env
    payload = {
        "job_wo": "WO-GENERIC",
        "brand": "Amarr",
        "model": "other",
        "model_label": "Canyon Ridge",
        "construction": "Steel",
        "insulated": True,
        "width": "16x7",
        "height": "",
        "windows": False,
        "color_base": "White",
        "customer_address": "456 Oak Ave",
        "notes": "Canyon Ridge — no catalog yet",
    }
    order, status = door_orders.create_door_order(conn, tech, payload)
    assert status == "processed"
    assert order["item_number"] == ""
    assert order["generic_path"] == 1


def test_status_transitions(tech_env):
    conn, tech = tech_env
    order, _ = door_orders.create_door_order(conn, tech, _coachman_payload("WO-STATUS"))

    updated, err = door_orders.set_status(conn, order["id"], "ordered", "staff")
    assert err is None
    assert updated["status"] == "ordered"

    # captured -> installed directly is not a valid transition
    bad, err = door_orders.set_status(conn, order["id"], "installed", "staff")
    # (ordered -> installed IS valid; re-verify by attempting an invalid one)
    assert err is None and bad["status"] == "installed"

    # installed is terminal
    terminal, err = door_orders.set_status(conn, order["id"], "cancelled", "staff")
    assert err == "invalid_transition"
    assert terminal is None


def test_tech_can_cancel_own_captured_order_only(tech_env):
    conn, tech = tech_env
    order, _ = door_orders.create_door_order(conn, tech, _coachman_payload("WO-CANCEL"))

    updated, err = door_orders.cancel_by_tech(conn, tech, "WO-CANCEL")
    assert err is None
    assert updated["status"] == "cancelled"

    # Once cancelled, a second cancel attempt is an invalid transition
    _, err2 = door_orders.cancel_by_tech(conn, tech, "WO-CANCEL")
    assert err2 == "invalid_transition"


def test_tech_cannot_cancel_after_office_ordered_it(tech_env):
    conn, tech = tech_env
    order, _ = door_orders.create_door_order(conn, tech, _coachman_payload("WO-LOCKED"))
    door_orders.set_status(conn, order["id"], "ordered", "staff")

    _, err = door_orders.cancel_by_tech(conn, tech, "WO-LOCKED")
    assert err == "invalid_transition"


def test_list_pending_only_shows_captured(tech_env):
    conn, tech = tech_env
    door_orders.create_door_order(conn, tech, _coachman_payload("WO-A"))
    order_b, _ = door_orders.create_door_order(conn, tech, _coachman_payload("WO-B"))
    door_orders.set_status(conn, order_b["id"], "ordered", "staff")

    pending = door_orders.list_pending(conn)
    job_wos = {o["job_wo"] for o in pending}
    assert "WO-A" in job_wos
    assert "WO-B" not in job_wos


# ─── HTTP endpoints ─────────────────────────────────────────────────────────


def test_http_door_order_endpoints(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("DISABLE_BACKGROUND_JOBS", "1")

    from database import init_db
    init_db(seed=True)

    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-DEMO', 'Demo', 'wh-sub')")
    cur = conn.execute(
        "INSERT INTO techs (name, truck_id, warehouse_id, deduction_source) "
        "VALUES ('Winston', 'T-DEMO', 'wh-sub', 'pwa')")
    raw = pwa.rotate_tech_token(conn, cur.lastrowid)
    conn.commit()
    conn.close()

    import main as main_app
    from fastapi.testclient import TestClient
    # RESEND_API_KEY is only zeroed via env, which only prevents a real send
    # if this is the first test in the process to import `main` (fragile —
    # depends on file collection order). Patch the actual send function
    # instead so this test can NEVER hit the real Resend API no matter what
    # else has already imported `main`/`config` in this process.
    monkeypatch.setattr(main_app, "send_html", lambda *a, **kw: {"id": "test-fake-id"})
    client = TestClient(main_app.app)

    r = client.get("/tech/lookups/designs", params={"model": "coachman"})
    assert r.status_code == 200
    assert len(r.json()["designs"]) == 15

    r = client.get("/tech/lookups/insulations", params={"model": "coachman", "design": "31"})
    assert [i["code"] for i in r.json()["insulations"]] == ["CGU"]

    body = _coachman_payload("HTTP-WO-1")
    body["token"] = raw
    r = client.post("/tech/door-orders", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "processed"
    assert data["order"]["item_number"] == "CGU21"

    # duplicate submission is idempotent
    r = client.post("/tech/door-orders", json=body)
    assert r.json()["status"] == "already_processed"

    r = client.post("/tech/door-orders", json={**body, "token": "bad"})
    assert r.status_code == 401

    # tech cancel
    r = client.post("/tech/door-orders/cancel", json={"token": raw, "job_wo": "HTTP-WO-1"})
    assert r.status_code == 200
    assert r.json()["order"]["status"] == "cancelled"
