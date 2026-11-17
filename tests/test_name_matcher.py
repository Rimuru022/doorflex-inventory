"""Part-name matching for invoice line items.

A fuzzy match that is applied automatically deducts stock with no human in
the loop, so it must never swap one size for another: a .243 spring is not a
.234 spring, an 18ft seal is not a 16ft seal.
"""
import json

import pytest

from modules.name_matcher import match_part


@pytest.fixture()
def catalog(conn):
    parts = [
        ("SPR-234-34", "Torsion Spring .234 x 2 x 34", ["spring 234x2x34"]),
        ("SPR-207-31", "Torsion Spring .207 x 2 x 31", [".207 spring"]),
        ("RLR-NYL-13", "Nylon Roller 13-Ball", ["rollers"]),
        ("SEAL-BTM-16", "Bottom Seal 16ft", ["bottom seal"]),
        ("HNG-1", "Hinge #1", ["hinge 1"]),
        ("CAB-7FT", "Lift Cable 7ft (pair)", ["cables 7ft"]),
    ]
    for pid, name, aliases in parts:
        conn.execute("INSERT INTO parts (id, name, aliases) VALUES (?, ?, ?)", (pid, name, json.dumps(aliases)))
    conn.commit()
    return conn


@pytest.mark.parametrize("raw", [
    "Torsion Spring .243 x 2 x 36",
    "Torsion spring 250 x 2 x 36",
    "Nylon Roller 11-Ball",
    "Bottom seal 18ft",
    "Hinge 4",
])
def test_different_size_is_never_auto_matched(catalog, raw):
    _part_id, _conf, match_type = match_part(catalog, raw)
    assert match_type != "fuzzy_auto"


@pytest.mark.parametrize("raw,expected", [
    ("Torsion Spring .234 x 2 x 34", "SPR-234-34"),   # exact name
    ("rollers", "RLR-NYL-13"),                         # exact alias
    ("Spring .234 x 2 x 34", "SPR-234-34"),            # same numbers, wording differs
    ("Nylon rollers 13 Ball", "RLR-NYL-13"),
    ("Lift cables 7ft pair", "CAB-7FT"),
])
def test_same_size_still_matches_automatically(catalog, raw, expected):
    part_id, _conf, match_type = match_part(catalog, raw)
    assert part_id == expected
    assert match_type in ("exact", "fuzzy_auto")


def test_size_mismatch_is_offered_for_human_confirmation(catalog):
    part_id, _conf, match_type = match_part(catalog, "Spring .243 x 2 x 34")
    assert match_type == "fuzzy_pending"
    assert part_id == "SPR-234-34"
