"""Distributor Excel catalog import — parser + idempotent importer."""
import os

from modules import excel_import


EXCEL_PATH = os.path.join(os.path.dirname(__file__), "..", "Service_Spring Excel.xlsx")


def test_parse_usage_sheet_returns_35_rows():
    with open(EXCEL_PATH, "rb") as f:
        rows = excel_import.parse_usage_sheet(f.read())
    assert len(rows) == 35
    assert all("part_no" in r and "description" in r and "category" in r for r in rows)


def test_parse_usage_sheet_unique_part_nos():
    with open(EXCEL_PATH, "rb") as f:
        rows = excel_import.parse_usage_sheet(f.read())
    part_nos = [r["part_no"] for r in rows]
    assert len(part_nos) == len(set(part_nos))


def test_parse_usage_sheet_has_expected_categories():
    with open(EXCEL_PATH, "rb") as f:
        rows = excel_import.parse_usage_sheet(f.read())
    cats = {r["category"] for r in rows}
    assert "Torsion Springs" in cats
    assert "Hardware" in cats


def test_parse_usage_sheet_missing_sheet_raises():
    import openpyxl
    from openpyxl import Workbook
    buf = openpyxl.writer.excel.ExcelWriter
    wb = Workbook()
    wb.active.title = "Other"
    wb.active.append(["a", "b"])
    from io import BytesIO
    b = BytesIO()
    wb.save(b)
    b.seek(0)
    try:
        excel_import.parse_usage_sheet(b.read())
    except ValueError as e:
        assert "Sheet" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_importer_creates_35_canonicals(conn):
    from modules import distributors as dist_mod
    dist_mod.create_distributor(conn, "service_spring", "Service Spring")
    with open(EXCEL_PATH, "rb") as f:
        rows = excel_import.parse_usage_sheet(f.read())
    result = excel_import.import_distributor_catalog(conn, "service_spring", rows)
    assert result["created"] == 35
    assert result["updated"] == 0
    assert result["errors"] == []
    canonical_count = conn.execute("SELECT COUNT(*) FROM parts WHERE id LIKE 'P-%'").fetchone()[0]
    assert canonical_count == 35
    mapping_count = conn.execute(
        "SELECT COUNT(*) FROM product_distributors WHERE distributor_id='service_spring'"
    ).fetchone()[0]
    assert mapping_count == 35


def test_importer_is_idempotent(conn):
    from modules import distributors as dist_mod
    dist_mod.create_distributor(conn, "service_spring", "Service Spring")
    with open(EXCEL_PATH, "rb") as f:
        rows = excel_import.parse_usage_sheet(f.read())
    excel_import.import_distributor_catalog(conn, "service_spring", rows)
    excel_import.import_distributor_catalog(conn, "service_spring", rows)
    canonical_count = conn.execute("SELECT COUNT(*) FROM parts WHERE id LIKE 'P-%'").fetchone()[0]
    assert canonical_count == 35


def test_importer_aliases_contain_part_no(conn):
    from modules import distributors as dist_mod
    import json
    dist_mod.create_distributor(conn, "service_spring", "Service Spring")
    with open(EXCEL_PATH, "rb") as f:
        rows = excel_import.parse_usage_sheet(f.read())
    excel_import.import_distributor_catalog(conn, "service_spring", rows)
    row = conn.execute("SELECT aliases FROM parts WHERE id LIKE 'P-%' LIMIT 1").fetchone()
    aliases = json.loads(row["aliases"])
    assert len(aliases) >= 1


def test_importer_sets_active_mapping(conn):
    from modules import distributors as dist_mod
    dist_mod.create_distributor(conn, "service_spring", "Service Spring")
    with open(EXCEL_PATH, "rb") as f:
        rows = excel_import.parse_usage_sheet(f.read())
    excel_import.import_distributor_catalog(conn, "service_spring", rows)
    actives = conn.execute(
        "SELECT COUNT(*) FROM product_distributors "
        "WHERE distributor_id='service_spring' AND is_active=1"
    ).fetchone()[0]
    assert actives == 35


def test_importer_unknown_distributor(conn):
    with open(EXCEL_PATH, "rb") as f:
        rows = excel_import.parse_usage_sheet(f.read())
    result = excel_import.import_distributor_catalog(conn, "ghost", rows)
    assert result["created"] == 0
    assert any("Unknown distributor" in e for e in result["errors"])