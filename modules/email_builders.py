"""Email HTML builders — render the Friday templates via Jinja2.

Every automated email ships as a separate template file under templates/;
this module loads them with the same Jinja2 environment the dashboard uses.
"""
import jinja2

from config import REPORT_FROM

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader("templates"),
    autoescape=jinja2.select_autoescape(),
)

DISTRIBUTOR_LABELS = {
    "amazon": "Amazon",
}


def _render(name, **ctx):
    return _env.get_template(name).render(**ctx)


def friday_alert_html(sections, total_low, date_label):
    """Section A — weekly low-stock summary grouped by distributor."""
    return _render(
        "email-friday-alert.html",
        sections=sections,
        total_low=total_low,
        date_label=date_label,
        labels=DISTRIBUTOR_LABELS,
    )


def reorder_html(distributor_name, items, warehouse_label):
    """C.1 — the order email sent to one distributor."""
    return _render(
        "email-reorder.html",
        distributor_name=distributor_name,
        items=items,
        warehouse_label=warehouse_label,
    )


def internal_summary_html(reorders, amazon_items, unassigned, date_label):
    """C.2+C.4 merged — one internal email: orders sent + Amazon picks."""
    return _render(
        "email-internal-summary.html",
        reorders=reorders,
        amazon_items=amazon_items,
        unassigned=unassigned,
        date_label=date_label,
        labels=DISTRIBUTOR_LABELS,
    )


def door_order_html(order, dashboard_url=""):
    """Door order capture notification — NOT part of the C.1-C.4 reorder
    family (separate workflow, never touches stock)."""
    return _render(
        "email-door-order.html",
        dashboard_url=dashboard_url,
        **order,
    )


def reconciliation_html(result, date_label):
    """PRD §4.5 — the daily 5:30pm job reconciliation email (captured /
    not-collected / uncaptured / removed-by-dispatch buckets)."""
    return _render(
        "email-reconciliation.html",
        result=result,
        date_label=date_label,
    )


def get_report_from():
    return REPORT_FROM
