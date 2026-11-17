"""Reorder engine tests: low-stock detection, grouping, quantity formula,
idempotency, and auto/manual resolution."""
import pytest

from modules import distributors, reorder


@pytest.fixture()
def test_distributors(seed_catalog):
    """Create the placeholder distributors these tests reference. The
    _seed_distributors fixture only seeds 'amazon' now; tests that need
    dist_a / dist_b must create them first."""
    distributors.create_distributor(seed_catalog, "dist_a", "Distributor A", email="")
    distributors.create_distributor(seed_catalog, "dist_b", "Distributor B", email="")
    return seed_catalog


def test_compute_qty_formula():
    assert reorder.compute_qty(5, 2) == 8   # 5*2 - 2
    assert reorder.compute_qty(4, 4) == 4   # min*2 - current (exactly at min)
    assert reorder.compute_qty(5, 9) == 1   # clamps to a minimum of 1


def test_find_low_warehouse_stock_respects_threshold(seed_catalog):
    low = reorder.find_low_warehouse_stock(seed_catalog)
    ids = {r["part_id"] for r in low}
    # spring (2<=5) and bearing (1<=10) are low; rollers (4<=4) is exactly at min
    assert ids == {"spring-4x36", "bearing", "rollers"}


def test_quantity_formula_uses_warehouse_values(seed_catalog):
    orders = reorder.build_distributor_orders(seed_catalog)
    # spring: min 5, current 2 -> 5*2-2 = 8
    spring = next(it for it in orders.get("unassigned", []) if it["part_id"] == "spring-4x36")
    assert spring["qty"] == 8


def test_grouping_by_active_distributor(test_distributors):
    distributors.update_distributor(test_distributors, "dist_a", email="a@example.com")
    distributors.set_active(test_distributors, "spring-4x36", "dist_a")
    distributors.set_active(test_distributors, "bearing", "amazon")
    orders = reorder.build_distributor_orders(test_distributors)
    assert orders.get("dist_a") is not None
    assert [it["part_id"] for it in orders["dist_a"]] == ["spring-4x36"]
    assert [it["part_id"] for it in orders["amazon"]] == ["bearing"]
    # rollers has no distributor -> unassigned
    assert any(it["part_id"] == "rollers" for it in orders.get("unassigned", []))


def test_run_weekly_reorder_is_idempotent(test_distributors):
    distributors.update_distributor(test_distributors, "dist_a", email="a@example.com")
    distributors.set_active(test_distributors, "spring-4x36", "dist_a")
    first = reorder.run_weekly_reorder(test_distributors, None)
    second = reorder.run_weekly_reorder(test_distributors, None)
    assert len(first["reorders"]) == 1
    # no duplicate reorder rows for the same part+warehouse
    rows = test_distributors.execute(
        "SELECT part_id, warehouse_id, COUNT(*) c FROM reorder_requests GROUP BY part_id, warehouse_id"
    ).fetchall()
    assert all(r["c"] == 1 for r in rows)


def test_no_reorder_sent_when_distributor_has_no_email(test_distributors):
    distributors.set_active(test_distributors, "spring-4x36", "dist_a")  # no email
    result = reorder.run_weekly_reorder(test_distributors, None)
    assert result["reorders"] == []
    # the item should fall into the unassigned bucket
    assert any(it["part_id"] == "spring-4x36" for it in result["unassigned"])


def test_auto_resolve_by_stock(test_distributors):
    distributors.update_distributor(test_distributors, "dist_a", email="a@example.com")
    distributors.set_active(test_distributors, "spring-4x36", "dist_a")
    reorder.run_weekly_reorder(test_distributors, None)
    assert test_distributors.execute("SELECT COUNT(*) c FROM reorder_requests WHERE status='pending'").fetchone()["c"] == 1
    # bring warehouse back above min
    test_distributors.execute(
        "UPDATE warehouse_stock SET quantity=8 WHERE part_id='spring-4x36'")
    test_distributors.commit()
    resolved = reorder.auto_resolve_by_stock(test_distributors)
    assert resolved == 1
    status = test_distributors.execute(
        "SELECT status FROM reorder_requests WHERE part_id='spring-4x36'").fetchone()["status"]
    assert status == "received"


def test_manual_resolve(seed_catalog):
    rr = reorder.create_reorder(seed_catalog, "bearing", "amazon", "wh-warranty", 19)
    reorder.resolve_reorder(seed_catalog, rr["id"], mode="manual")
    status = seed_catalog.execute("SELECT status FROM reorder_requests WHERE id=?", (rr["id"],)).fetchone()["status"]
    assert status == "received"


def test_batch_set_active_only_one_distributor(test_distributors):
    distributors.set_active(test_distributors, "spring-4x36", "dist_a")
    distributors.set_active(test_distributors, "spring-4x36", "dist_b")
    active = distributors.list_mappings_for_product(test_distributors, "spring-4x36")
    on = [m for m in active if m["is_active"]]
    assert len(on) == 1 and on[0]["distributor_id"] == "dist_b"
