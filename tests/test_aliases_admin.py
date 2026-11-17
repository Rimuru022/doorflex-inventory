"""Aliases admin: bulk add and remove on canonical parts."""
import json

from modules import parts


def _make_part(conn, pid="X-1", name="Widget", aliases=None):
    parts.create_part(conn, pid, name=name, aliases=aliases or [])


def test_bulk_add_basic(conn):
    _make_part(conn, aliases=["red spring"])
    result = parts.bulk_add_aliases(conn, "X-1", "blue spring, 207 spring")
    assert "red spring" in result
    assert "blue spring" in result
    assert "207 spring" in result
    assert len(result) == 3


def test_bulk_add_dedupes(conn):
    _make_part(conn, aliases=["red spring"])
    result = parts.bulk_add_aliases(conn, "X-1", "red spring, blue spring, blue spring")
    assert result.count("red spring") == 1
    assert result.count("blue spring") == 1


def test_bulk_add_case_insensitive_dedup(conn):
    _make_part(conn, aliases=["Red Spring"])
    result = parts.bulk_add_aliases(conn, "X-1", "red spring, RED SPRING")
    assert len(result) == 1


def test_bulk_add_handles_whitespace_and_empties(conn):
    _make_part(conn)
    result = parts.bulk_add_aliases(conn, "X-1", "  a , , b ,  ,c")
    assert result == ["a", "b", "c"]


def test_bulk_add_missing_part_returns_none(conn):
    assert parts.bulk_add_aliases(conn, "NOPE", "a, b") is None


def test_bulk_add_persists(conn):
    _make_part(conn)
    parts.bulk_add_aliases(conn, "X-1", "x, y")
    stored = json.loads(conn.execute("SELECT aliases FROM parts WHERE id='X-1'").fetchone()["aliases"])
    assert stored == ["x", "y"]


def test_remove_alias_basic(conn):
    _make_part(conn, aliases=["a", "b", "c"])
    assert parts.remove_alias(conn, "X-1", "b") is True
    stored = json.loads(conn.execute("SELECT aliases FROM parts WHERE id='X-1'").fetchone()["aliases"])
    assert stored == ["a", "c"]


def test_remove_alias_missing(conn):
    _make_part(conn, aliases=["a"])
    assert parts.remove_alias(conn, "X-1", "zzz") is False
    stored = json.loads(conn.execute("SELECT aliases FROM parts WHERE id='X-1'").fetchone()["aliases"])
    assert stored == ["a"]


def test_remove_alias_part_missing(conn):
    assert parts.remove_alias(conn, "NOPE", "a") is False


def test_legacy_add_alias_still_works(conn):
    _make_part(conn)
    parts.add_alias(conn, "X-1", "legacy alias")
    stored = json.loads(conn.execute("SELECT aliases FROM parts WHERE id='X-1'").fetchone()["aliases"])
    assert stored == ["legacy alias"]