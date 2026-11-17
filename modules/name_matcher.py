import re
import difflib
import json


_NUMBER_RE = re.compile(r"\d+")


def _numbers(text):
    """Sorted numeric tokens with leading zeros dropped: '.218 x 2 x 32' ->
    ['2', '218', '32']."""
    return sorted(n.lstrip("0") or "0" for n in _NUMBER_RE.findall(text))


def match_part(conn, raw_name):
    raw_lower = raw_name.strip().lower()
    parts = conn.execute("SELECT * FROM parts WHERE active=1").fetchall()

    for part in parts:
        if part["name"].strip().lower() == raw_lower:
            return part["id"], 1.0, "exact"
        aliases = json.loads(part["aliases"])
        for alias in aliases:
            if alias.strip().lower() == raw_lower:
                return part["id"], 1.0, "exact"

    best_part = None
    best_ratio = 0
    best_text = ""

    for part in parts:
        for text in [part["name"], *json.loads(part["aliases"])]:
            ratio = difflib.SequenceMatcher(None, raw_lower, text.strip().lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_part = part["id"]
                best_text = text

    if best_part:
        confidence = round(best_ratio, 2)
        # Sizes live in the numbers (.243 vs .234 spring, 18ft vs 16ft seal,
        # 11- vs 13-ball roller). Text similarity can't see that, so a fuzzy
        # match whose numbers differ is never applied without a human.
        raw_numbers = _numbers(raw_lower)
        sizes_agree = not raw_numbers or raw_numbers == _numbers(best_text)
        if confidence >= 0.85 and sizes_agree:
            return best_part, confidence, "fuzzy_auto"
        if confidence >= 0.7:
            return best_part, confidence, "fuzzy_pending"

    return None, round(best_ratio, 2), "no_match"


def match_package(conn, description):
    import difflib
    desc_lower = description.strip().lower()
    packages = conn.execute("SELECT * FROM packages WHERE active=1").fetchall()

    for pkg in packages:
        if pkg["name"].strip().lower() == desc_lower:
            return pkg, json.loads(pkg["parts_json"]), 1.0

    best_pkg = None
    best_ratio = 0
    for pkg in packages:
        ratio = difflib.SequenceMatcher(None, desc_lower, pkg["name"].strip().lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_pkg = pkg

    if best_pkg and best_ratio >= 0.7:
        return best_pkg, json.loads(best_pkg["parts_json"]), round(best_ratio, 2)

    return None, [], round(best_ratio, 2)
