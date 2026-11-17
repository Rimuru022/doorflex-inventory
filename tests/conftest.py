"""Shared fixtures for the DoorFlex test suite.

Uses an in-memory SQLite database built from the real SCHEMA so tests exercise
the actual constraints (single active distributor, FKs, etc.) without touching
the production data file.
"""
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SCHEMA, _migrate, _seed_distributors  # noqa: E402


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    _migrate(connection)  # applies amazon_sku and other additive columns
    _seed_distributors(connection)
    yield connection
    connection.close()


@pytest.fixture()
def seed_catalog(conn):
    """A couple of parts + warehouse stock below min, with a mapping set."""

    def _seed():
        conn.executescript(
            """
            INSERT INTO warehouses (id, name) VALUES ('wh-warranty', 'DoorFlex');
            INSERT INTO parts (id, name, amazon_sku) VALUES
                ('spring-4x36', 'Spring 4x36', ''),
                ('bearing', 'Bearings', 'https://www.amazon.com/dp/B0TEST'),
                ('rollers', 'Rollers', '');
            INSERT INTO warehouse_stock (warehouse_id, part_id, quantity, min_threshold) VALUES
                ('wh-warranty', 'spring-4x36', 2, 5),
                ('wh-warranty', 'bearing', 1, 10),
                ('wh-warranty', 'rollers', 4, 4);
            """
        )
        conn.commit()

    _seed()
    return conn
