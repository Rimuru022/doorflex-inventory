"""Monthly report — one-page professional PDF with the vital data only.

Built on top of the dashboard's own queries so what you see here matches what
the team sees there. No fluff: key metrics, stock health, unresolved queue,
top movers and top critical low stock.
"""
import io
import sqlite3
from datetime import datetime, timedelta

import requests
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from config import REPORT_FROM


def _month_window(now=None):
    now = now or datetime.now()
    start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
    end = now.replace(day=1) - timedelta(days=1)
    return start, end


def build_report_data(conn: sqlite3.Connection, now=None):
    """Aggregate the vital data the one-page PDF needs."""
    start, end = _month_window(now)
    month_label = start.strftime("%B %Y")
    period = f"{start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}"

    techs = conn.execute("SELECT COUNT(*) c FROM techs WHERE active=1").fetchone()["c"]
    trucks = conn.execute("SELECT COUNT(*) c FROM trucks WHERE active=1").fetchone()["c"]
    parts = conn.execute("SELECT COUNT(*) c FROM parts WHERE active=1").fetchone()["c"]

    inv_month = conn.execute(
        """SELECT COUNT(DISTINCT invoice_id) c, COALESCE(SUM(quantity_change),0) qty
           FROM log WHERE source='email'
             AND timestamp >= ? AND timestamp <= ? || ' 23:59:59'""",
        (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
    ).fetchone()
    invoices_month = inv_month["c"] or 0
    line_items_month = inv_month["qty"] or 0

    low_truck = conn.execute(
        """SELECT p.name part, t.name truck, s.quantity qty, s.min_threshold mn
           FROM stock s
           JOIN parts p ON p.id=s.part_id
           JOIN trucks t ON t.id=s.truck_id
           WHERE s.quantity < s.min_threshold AND p.active=1 AND t.active=1
           ORDER BY (s.min_threshold - s.quantity) DESC LIMIT 5"""
    ).fetchall()
    low_wh = conn.execute(
        """SELECT p.name part, w.name wh, ws.quantity qty, ws.min_threshold mn
           FROM warehouse_stock ws
           JOIN parts p ON p.id=ws.part_id
           JOIN warehouses w ON w.id=ws.warehouse_id
           WHERE ws.quantity < ws.min_threshold AND p.active=1 AND w.active=1
           ORDER BY (ws.min_threshold - ws.quantity) DESC LIMIT 5"""
    ).fetchall()
    low_count_truck = conn.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(s.min_threshold - s.quantity),0) g
           FROM stock s JOIN parts p ON p.id=s.part_id JOIN trucks t ON t.id=s.truck_id
           WHERE s.quantity < s.min_threshold AND p.active=1 AND t.active=1"""
    ).fetchone()
    low_count_wh = conn.execute(
        """SELECT COUNT(*) c, COALESCE(SUM(ws.min_threshold - ws.quantity),0) g
           FROM warehouse_stock ws JOIN parts p ON p.id=ws.part_id JOIN warehouses w ON w.id=ws.warehouse_id
           WHERE ws.quantity < ws.min_threshold AND p.active=1 AND w.active=1"""
    ).fetchone()
    low_count = (low_count_truck["c"] or 0) + (low_count_wh["c"] or 0)
    shortfall = (low_count_truck["g"] or 0) + (low_count_wh["g"] or 0)

    unmatched_parts = conn.execute(
        "SELECT COUNT(*) c FROM unmatched_parts WHERE kind='part' AND mapped_to_part_id IS NULL"
    ).fetchone()["c"]
    unmatched_packages = conn.execute(
        "SELECT COUNT(*) c FROM unmatched_parts WHERE kind='package' AND mapped_to_package_id IS NULL"
    ).fetchone()["c"]
    tech_confirms = conn.execute(
        "SELECT COUNT(*) c FROM unmatched_parts WHERE kind='tech' AND tech_name=''"
    ).fetchone()["c"]
    stock_alerts = conn.execute(
        "SELECT COUNT(*) c FROM stock_alerts WHERE resolved=0"
    ).fetchone()["c"]

    top_movers = conn.execute(
        """SELECT p.name part, SUM(ABS(l.quantity_change)) qty
           FROM log l JOIN parts p ON p.id=l.part_id
           WHERE l.source='email'
             AND l.timestamp >= ? AND l.timestamp <= ? || ' 23:59:59'
           GROUP BY l.part_id ORDER BY qty DESC LIMIT 5""",
        (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
    ).fetchall()

    return {
        "month_label": month_label,
        "period": period,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M AZ"),
        "metrics": {
            "Techs": techs,
            "Trucks": trucks,
            "Active parts": parts,
            "Invoices (month)": invoices_month,
            "Line items (month)": line_items_month,
        },
        "stock": {
            "low_count": low_count,
            "shortfall": shortfall,
            "top_low": [dict(r) for r in low_truck] + [dict(r) for r in low_wh],
        },
        "unresolved": {
            "unmatched_parts": unmatched_parts,
            "unmatched_packages": unmatched_packages,
            "tech_confirmations": tech_confirms,
            "stock_alerts": stock_alerts,
        },
        "top_movers": [dict(r) for r in top_movers],
    }


def render_report_pdf(data: dict) -> bytes:
    """One-page professional PDF. Letter size, tight margins."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.4 * inch, bottomMargin=0.4 * inch,
        title="DoorFlex Inventory — Monthly Report",
        author="DoorFlex Inventory",
    )
    styles = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=styles["Heading1"], fontSize=15, spaceAfter=2, textColor=colors.HexColor("#1a1a1a"))
    sub = ParagraphStyle("s", parent=styles["Normal"], fontSize=8.5, textColor=colors.HexColor("#666"), spaceAfter=6)
    section = ParagraphStyle("h", parent=styles["Heading3"], fontSize=10, spaceBefore=4, spaceAfter=3, textColor=colors.HexColor("#9a5a1f"))
    cell = ParagraphStyle("c", parent=styles["Normal"], fontSize=8.5, leading=10)
    cellb = ParagraphStyle("cb", parent=styles["Normal"], fontSize=8.5, leading=10, fontName="Helvetica-Bold")
    foot = ParagraphStyle("f", parent=styles["Normal"], fontSize=7.5, textColor=colors.HexColor("#888"))

    story = []
    story.append(Paragraph("DOORFLEX INVENTORY — MONTHLY REPORT", title))
    story.append(Paragraph(
        f"<b>{data['month_label']}</b> &nbsp;·&nbsp; Period: {data['period']} &nbsp;·&nbsp; Generated: {data['generated_at']}",
        sub,
    ))

    metrics = data["metrics"]
    metrics_table = Table(
        [[Paragraph(f"<b>{v}</b>", cellb), Paragraph(k, cell)] for k, v in metrics.items()],
        colWidths=[0.9 * inch, 1.7 * inch],
    )
    metrics_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f6f1ea")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e0d8c8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    stk = data["stock"]
    stock_table = Table(
        [[Paragraph(f"<b>{stk['low_count']}</b>", cellb), Paragraph("items below min", cell)],
         [Paragraph(f"<b>{stk['shortfall']}</b>", cellb), Paragraph("total shortfall units", cell)]],
        colWidths=[0.9 * inch, 1.7 * inch],
    )
    stock_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#fbeae6")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e0d8c8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    u = data["unresolved"]
    unresolved_table = Table(
        [[Paragraph(f"<b>{u['unmatched_parts']}</b>", cellb), Paragraph("unmatched parts", cell)],
         [Paragraph(f"<b>{u['unmatched_packages']}</b>", cellb), Paragraph("unmatched packages", cell)],
         [Paragraph(f"<b>{u['tech_confirmations']}</b>", cellb), Paragraph("tech confirmations", cell)],
         [Paragraph(f"<b>{u['stock_alerts']}</b>", cellb), Paragraph("stock alerts unresolved", cell)]],
        colWidths=[0.9 * inch, 1.7 * inch],
    )
    unresolved_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#fbf6e6")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e0d8c8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))

    expl_style = ParagraphStyle("e", parent=styles["Normal"], fontSize=7.5, leading=9.2, textColor=colors.HexColor("#5a5a5a"))

    def col_cell(header_text, explain_text, data_table):
        return [
            Paragraph(f"<b>{header_text}</b>", section),
            Paragraph(explain_text, expl_style),
            Spacer(1, 2),
            data_table,
        ]

    intro = Paragraph(
        "A plain-language snapshot of DoorFlex this month: what's on the move, what's running low, "
        "and what still needs your attention. Each section below is explained as you read.",
        ParagraphStyle("i", parent=styles["Normal"], fontSize=8.5, leading=11,
                       textColor=colors.HexColor("#4a4a4a"), spaceAfter=5),
    )
    story.append(intro)

    body = Table(
        [[col_cell(
            "Key Metrics",
            "Your operation at a glance — active techs, trucks and parts, plus how many invoices arrived "
            "this month and how many parts were actually used.",
            metrics_table),
          col_cell(
            "Stock Health",
            "How inventory is holding up. \"Below Min\" items dropped under their minimum: Qty is what's "
            "on hand, Min is what you want to keep — these are the ones to reorder.",
            stock_table),
          col_cell(
            "Unresolved Queue",
            "Things waiting on a decision so your numbers stay accurate — parts or packages that didn't "
            "match the catalog, techs to confirm, and stock alerts to review. Zero is the goal.",
            unresolved_table)]],
        colWidths=[2.6 * inch, 2.6 * inch, 2.6 * inch],
    )
    body.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(body)
    story.append(Spacer(1, 0.1 * inch))

    def movers_table(rows, title):
        data_rows = [[Paragraph("<b>#</b>", cellb), Paragraph(f"<b>{title}</b>", cellb), Paragraph("<b>Qty</b>", cellb)]]
        for i, r in enumerate(rows, 1):
            data_rows.append([Paragraph(str(i), cell), Paragraph(r.get("part") or r.get("part_name", ""), cell), Paragraph(str(r.get("qty") or r.get("qty_used", 0)), cell)])
        if len(data_rows) == 1:
            data_rows.append([Paragraph("—", cell), Paragraph("No activity this period.", cell), Paragraph("", cell)])
        t = Table(data_rows, colWidths=[0.3 * inch, 4.7 * inch, 0.5 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3efe7")),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e0d8c8")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return t

    def low_table(rows):
        data_rows = [[Paragraph("<b>Part</b>", cellb), Paragraph("<b>Location</b>", cellb),
                      Paragraph("<b>Qty</b>", cellb), Paragraph("<b>Min</b>", cellb)]]
        for r in rows:
            loc = r.get("truck") or r.get("wh") or "—"
            data_rows.append([
                Paragraph(r.get("part") or r.get("part_name", ""), cell),
                Paragraph(loc, cell),
                Paragraph(f"<b><font color='#b00020'>{r['qty']}</font></b>", cell),
                Paragraph(str(r["mn"]), cell),
            ])
        if len(data_rows) == 1:
            data_rows.append([Paragraph("All stock above minimum.", cell), "", "", ""])
        t = Table(data_rows, colWidths=[2.5 * inch, 1.8 * inch, 0.6 * inch, 0.6 * inch])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f3efe7")),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e0d8c8")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return t

    body2 = Table(
        [[col_cell(
            "Top Movers (this month)",
            "Which parts your techs used the most — a good guide for what to keep in stock next.",
            movers_table(data["top_movers"], "Part")),
          col_cell(
            "Most Critical Low Stock",
            "Where stock is hurting the most, by shortfall. Location, current Qty and Min show exactly "
            "what to order first.",
            low_table(data["stock"]["top_low"]))]],
        colWidths=[5.5 * inch, 5.5 * inch],
    )
    body2.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(body2)
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        "Generated automatically by DoorFlex Inventory. Internal use only.",
        foot,
    ))

    doc.build(story)
    return buf.getvalue()


def send_report_pdf(pdf_bytes: bytes, to_emails, month_label: str, api_key: str):
    """Email the PDF as an attachment via Resend."""
    import base64
    from config import DEMO_MODE
    if DEMO_MODE:
        return {"id": "demo-simulated", "demo": True}
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": REPORT_FROM,
            "to": to_emails,
            "subject": f"DoorFlex Inventory — {month_label} Report",
            "text": f"DoorFlex Inventory monthly report for {month_label}. See attached PDF.",
            "attachments": [{
                "filename": f"DoorFlex-{month_label.replace(' ', '-')}.pdf",
                "content": base64.b64encode(pdf_bytes).decode(),
            }],
        },
    )
    resp.raise_for_status()
    return resp.json()
