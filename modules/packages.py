import json
import re


def _slug(name):
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    s = s.strip('-')
    return "pkg-" + s


def list_packages(conn):
    return conn.execute("SELECT * FROM packages WHERE active=1 ORDER BY name").fetchall()


def get_package(conn, package_id):
    return conn.execute("SELECT * FROM packages WHERE id=?", (package_id,)).fetchone()


def create_package(conn, name, parts_list):
    package_id = _slug(name)
    parts_json = json.dumps(parts_list)
    existing = conn.execute("SELECT id FROM packages WHERE id=?", (package_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE packages SET name=?, parts_json=?, active=1 WHERE id=?",
            (name, parts_json, package_id),
        )
    else:
        conn.execute(
            "INSERT INTO packages (id, name, parts_json) VALUES (?, ?, ?)",
            (package_id, name, parts_json),
        )
    conn.commit()
    return get_package(conn, package_id)


def update_package(conn, package_id, name=None, parts_list=None):
    fields = []
    params = []
    if name is not None:
        fields.append("name=?")
        params.append(name)
    if parts_list is not None:
        fields.append("parts_json=?")
        params.append(json.dumps(parts_list))
    if not fields:
        return get_package(conn, package_id)
    params.append(package_id)
    conn.execute(f"UPDATE packages SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()
    return get_package(conn, package_id)


def deactivate_package(conn, package_id):
    conn.execute("UPDATE packages SET active=0 WHERE id=?", (package_id,))
    conn.commit()
    return get_package(conn, package_id)
