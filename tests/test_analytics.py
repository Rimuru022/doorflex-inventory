"""Usage-tracking reports must include PWA-driven deductions (source='pwa'),
not just distributor-invoice-driven ones (source='email') — previously these
functions hardcoded source='email', silently hiding all tech PWA activity
from the daily/weekly/monthly usage-by-tech/truck/part dashboard views."""
from modules import analytics


def _seed(conn):
    conn.executescript(
        """
        INSERT INTO warehouses (id, name) VALUES ('wh-1', 'Main');
        INSERT INTO trucks (id, name, warehouse_id) VALUES ('T-1', 'Truck 1', 'wh-1');
        INSERT INTO parts (id, name) VALUES ('SPR-207', 'Spring');
        """
    )
    conn.execute(
        """INSERT INTO log (tech_name, truck_id, warehouse_id, part_id,
            quantity_change, stock_before, stock_after, source)
           VALUES ('Winston', 'T-1', 'wh-1', 'SPR-207', -2, 10, 8, 'pwa')"""
    )
    conn.execute(
        """INSERT INTO log (tech_name, truck_id, warehouse_id, part_id,
            quantity_change, stock_before, stock_after, source)
           VALUES ('', 'T-1', 'wh-1', 'SPR-207', -3, 8, 5, 'email')"""
    )
    conn.commit()


def test_top_movers_includes_pwa_and_email_sources(conn):
    _seed(conn)
    rows = analytics.top_movers(conn, warehouse_id="wh-1", days=30)
    assert dict(rows[0])["total_used"] == 5  # 2 (pwa) + 3 (email)


def test_usage_by_truck_includes_pwa(conn):
    _seed(conn)
    rows = analytics.usage_by_truck(conn, "wh-1", days=30)
    assert dict(rows[0])["total_qty_used"] == 5


def test_usage_by_part_includes_pwa(conn):
    _seed(conn)
    rows = analytics.usage_by_part(conn, "T-1", days=30)
    assert dict(rows[0])["total_qty"] == 5


def test_usage_by_tech_includes_pwa_rows(conn):
    _seed(conn)
    rows = analytics.usage_by_tech(conn, "wh-1", days=30)
    # Only the pwa row has a tech_name — the email row's tech_name is ''
    # and is already excluded by the existing tech_name != '' filter.
    assert len(rows) == 1
    assert dict(rows[0])["tech_name"] == "Winston"
    assert dict(rows[0])["total_qty_used"] == 2


def test_usage_by_part_for_tech_includes_pwa(conn):
    _seed(conn)
    rows = analytics.usage_by_part_for_tech(conn, "Winston", days=30)
    assert dict(rows[0])["total_qty"] == 2
