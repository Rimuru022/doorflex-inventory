"""Email template builder tests — render the 4 templates with sample data and
assert on subjects/body content. No network, no real emails."""
from modules.email_builders import (
    friday_alert_html,
    internal_summary_html,
    reorder_html,
)


def sample_items():
    return [
        {
            "sku": "spring-4x36", "part_name": "Spring 4x36", "current": 2, "min": 5,
            "diff": 3, "distributor_sku": "A-1182", "warehouse_name": "DoorFlex",
        }
    ]


def test_friday_alert_includes_all_distributor_sections():
    html = friday_alert_html(
        {"amazon": sample_items(), "unassigned": sample_items()},
        3, "2026-08-14",
    )
    assert "Weekly Low-Stock Alert" in html
    assert "Amazon" in html
    assert "no active distributor" in html.lower()
    assert "spring-4x36" in html
    assert "A-1182" in html


def test_friday_alert_has_no_low_stock_empty_state():
    html = friday_alert_html({}, 0, "2026-08-14")
    assert "above minimum" in html


def test_reorder_email_lists_distributor_sku_and_qty():
    html = reorder_html(
        "Distributor A",
        [{"distributor_sku": "A-1182", "distributor_part_name": "Spring 4x36",
          "part_name": "Spring 4x36", "qty": 8, "sku": "spring-4x36"}],
        "DoorFlex",
    )
    assert "Purchase Order" in html
    assert "Distributor A" in html
    assert "A-1182" in html
    assert "8" in html


def test_internal_summary_has_orders_and_amazon_sections():
    html = internal_summary_html(
        reorders=[{
            "distributor_id": "dist_a", "distributor_name": "Distributor A",
            "email": "a@example.com", "items": [{"part_name": "Spring 4x36", "qty": 8}],
        }],
        amazon_items=[{
            "part_name": "Bearings", "sku": "bearing", "qty": 19,
            "amazon_url": "https://www.amazon.com/dp/B0TEST",
        }],
        unassigned=[{"sku": "rollers", "part_name": "Rollers", "current": 4, "min": 4}],
        date_label="2026-08-14",
    )
    assert "Orders sent to distributors" in html
    assert "Amazon purchases needed" in html
    assert "without a distributor" in html
    assert "https://www.amazon.com/dp/B0TEST" in html
    assert "Distributor A" in html
