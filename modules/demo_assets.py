"""Synthetic images and files for DEMO_MODE.

- write_receipt(): a card-terminal style receipt for a PWA job capture.
- write_winston_report(): a "phone photo of a spreadsheet" for the weekly
  Winston payment report, so the vision parse + human review flow can be shown
  on data that matches the rest of the demo.
- sample_invoice_pdf(): a Dispatch-style itemized invoice that the real
  deterministic parser (text_extractor.InvoiceDeterministicParser) reads, for
  visitors to try Catalog -> Upload.

Everything rendered here is fictitious.
"""
import io
import random
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def _font(size, bold=False):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _money(v):
    return f"${v:,.2f}"


def write_receipt(out_dir, batch_id, job, amount, method, day):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    w, h = 420, 640
    img = Image.new("RGB", (w, h), (250, 249, 246))
    d = ImageDraw.Draw(img)
    y = 28
    d.text((w // 2, y), "WINSTON HOME WARRANTY", font=_font(20, True), fill=(20, 20, 20), anchor="mt")
    y += 30
    d.text((w // 2, y), "Field payment receipt", font=_font(15), fill=(90, 90, 90), anchor="mt")
    y += 40
    rows = [
        ("Date", day.strftime("%b %d, %Y")),
        ("Work order", job["wo"]),
        ("Service", job["type"]),
        ("Address", job["address"][:30]),
        ("Method", {"card": "Card (contactless)", "cash": "Cash", "check": "Check"}.get(method, method)),
    ]
    for k, v in rows:
        d.text((32, y), k, font=_font(15), fill=(110, 110, 110))
        d.text((w - 32, y), str(v), font=_font(15, True), fill=(25, 25, 25), anchor="ra")
        y += 32
    y += 10
    d.line((32, y, w - 32, y), fill=(200, 200, 200), width=2)
    y += 24
    d.text((32, y), "TOTAL", font=_font(22, True), fill=(20, 20, 20))
    d.text((w - 32, y), _money(amount or 0), font=_font(26, True), fill=(20, 20, 20), anchor="ra")
    y += 70
    d.text((w // 2, y), "APPROVED", font=_font(18, True), fill=(30, 120, 60), anchor="mt")
    y += 34
    d.text((w // 2, y), f"Auth {abs(hash(batch_id)) % 900000 + 100000}", font=_font(14), fill=(120, 120, 120), anchor="mt")
    # Slight rotation + blur so it reads as a photo, not a screenshot.
    img = img.rotate(-2.2, expand=True, fillcolor=(214, 206, 192)).filter(ImageFilter.GaussianBlur(0.5))
    name = f"{batch_id}.jpg"
    img.convert("RGB").save(out_dir / name, "JPEG", quality=78)
    return name


def render_winston_report(parsed, seed="x"):
    rng = random.Random(seed)
    jobs = parsed["jobs"]
    w = 1100
    row_h = 40
    h = 260 + row_h * (len(jobs) + 5)
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((40, 30), f"Weekly Payout - {parsed['tech_name']}", font=_font(30, True), fill=(20, 20, 20))
    d.text((40, 76), f"Period: {parsed['period_start']}  to  {parsed['period_end']}", font=_font(20), fill=(60, 60, 60))
    cols = [("Date", 40), ("WO #", 210), ("Total Sales", 420), ("Payout", 620), ("Commission", 800)]
    y = 140
    d.rectangle((30, y - 8, w - 30, y + row_h - 8), fill=(225, 236, 247))
    for label, x in cols:
        d.text((x, y), label, font=_font(19, True), fill=(20, 40, 70))
    y += row_h
    for i, j in enumerate(jobs):
        if i % 2:
            d.rectangle((30, y - 8, w - 30, y + row_h - 8), fill=(246, 246, 246))
        vals = [j["date"], j["wo_number"], _money(j["total_sales"]), _money(j["payout"]), _money(j["commission"])]
        for (label, x), v in zip(cols, vals):
            d.text((x, y), v, font=_font(19), fill=(30, 30, 30))
        y += row_h
    y += 16
    d.line((30, y, w - 30, y), fill=(120, 120, 120), width=2)
    y += 18
    for label, v in (("Subtotal sales", parsed["subtotal_sales"]), ("Subtotal payout", parsed["subtotal_payout"]),
                     ("Parts deduction", -parsed["parts_deduction"]), ("TOTAL COMMISSION", parsed["total_commission"])):
        bold = label.startswith("TOTAL")
        d.text((560, y), label, font=_font(20, bold), fill=(20, 20, 20))
        d.text((w - 60, y), _money(v), font=_font(20, bold), fill=(20, 20, 20), anchor="ra")
        y += 36
    # Photo of a screen: perspective-ish tilt, warm cast, soft focus.
    img = img.rotate(rng.uniform(-3.5, -1.5), expand=True, fillcolor=(58, 52, 46), resample=Image.BICUBIC)
    overlay = Image.new("RGB", img.size, (255, 236, 205))
    img = Image.blend(img, overlay, 0.08).filter(ImageFilter.GaussianBlur(0.8))
    return img


def write_winston_report(out_dir, stem, parsed):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{stem}.jpg"
    render_winston_report(parsed, seed=stem).save(out_dir / name, "JPEG", quality=80)
    return name


def winston_report_bytes(parsed):
    buf = io.BytesIO()
    render_winston_report(parsed, seed="sample").save(buf, "JPEG", quality=82)
    return buf.getvalue()


def sample_invoice_pdf(tech_name, invoice_no, items, issue=None):
    """Itemized invoice laid out so pdfplumber's text matches what the
    Dispatch PDFs produce (see text_extractor regexes)."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    issue = issue or date.today()
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    _w, h = LETTER
    y = h - 60

    def line(text, size=11, bold=False, x=60):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(x, y, text)
        y -= size + 7

    line("From", 10, True)
    line("DoorFlex LLC 1200 E Demo Rd Queen Creek, AZ 85142 office@doorflex.example +15550100100", 9)
    line("Invoice for Jordan Demo", 11, True)
    line(f"Job ID # {112_000_000 + invoice_no}", 10)
    line(f"Invoice # {invoice_no}", 10)
    line(f"Issue date {issue.strftime('%b %d, %Y')}", 10)
    line("jordan.demo@example.com", 10)
    line(f"Work start date {issue.strftime('%b %d, %Y')}", 10)
    line("+15550142200", 10)
    line("Job Address 4821 E Desert Willow Dr, Mesa AZ, 85212", 10)
    y -= 6
    line("Item Quantity Price Total", 10, True)
    subtotal = 0.0
    for desc, qty, price in items:
        total = qty * price
        subtotal += total
        line(f"{desc} {qty} ${price:,.2f} ${total:,.2f}", 10)
    line(f"Labor 1 $195.00 $195.00", 10)
    subtotal += 195
    tax = round(subtotal * 0.081, 2)
    line(f"Tax 1 ${tax:,.2f} ${tax:,.2f}", 10)
    y -= 6
    line(f"Subtotal ${subtotal:,.2f}", 10)
    line(f"Tax ${tax:,.2f}", 10)
    line(f"Total ${subtotal + tax:,.2f}", 10, True)
    y -= 10
    line("Signatures", 10, True)
    line(f"Jordan Demo - {issue.strftime('%m/%d/%Y')} 10:42am MST {tech_name}", 10)
    line("Customer", 10)
    line("DoorFlex LLC", 10)
    line("Terms", 10, True)
    line("Demo invoice generated for the DoorFlex public demo. Not a real transaction.", 9)
    c.showPage()
    c.save()
    return buf.getvalue()
