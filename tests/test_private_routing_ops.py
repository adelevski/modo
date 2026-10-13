"""Exercise repair safety on synthetic SQLite data without running maintenance."""

import pathlib
import sqlite3
import subprocess
import sys

import pytest

OPS = pathlib.Path(__file__).resolve().parents[1] / "ops" / "private-national-routing"
SCHEMA = """
CREATE TABLE admins (
    admin_level INTEGER, iso_code TEXT, parent_admin INTEGER,
    name TEXT, name_en TEXT, drive_on_right INTEGER,
    allow_intersection_names INTEGER, default_language TEXT,
    supported_languages TEXT, geom TEXT
);
"""


class Collect:
    """Stand in for geometry aggregation; these tests concern parent selection."""

    def __init__(self):
        self.value = None

    def step(self, value):
        self.value = value

    def finalize(self):
        return self.value


@pytest.mark.parametrize("states,existing_parent", [(0, False), (2, False), (2, True)])
def test_admin_parent_insert_is_guarded(states, existing_parent):
    with sqlite3.connect(":memory:") as db:
        db.executescript(SCHEMA)
        db.create_aggregate("ST_Collect", 1, Collect)
        db.create_function("ST_UnaryUnion", 1, lambda geom: geom)
        db.create_function("CastToMultiPolygon", 1, lambda geom: geom)
        for _ in range(states):
            db.execute("INSERT INTO admins (admin_level, geom) VALUES (4, 'state')")
        if existing_parent:
            db.execute("INSERT INTO admins (admin_level, iso_code) VALUES (2, 'US')")
        db.commit()
        sql = (OPS / "data" / "repair-us-admin-parent.sql").read_text()
        for _ in range(2):
            db.executescript(sql)
            parents = db.execute(
                "SELECT rowid FROM admins WHERE admin_level = 2 AND iso_code = 'US'"
            ).fetchall()
            assert len(parents) == bool(states or existing_parent)
            if states:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM admins WHERE admin_level = 4 AND parent_admin = ?",
                        parents[0],
                    ).fetchone()[0]
                    == states
                )


@pytest.mark.parametrize("states,existing_parent", [(0, False), (2, False), (2, True)])
def test_repair_database_preflight_is_read_only(tmp_path, states, existing_parent):
    path = tmp_path / "admins.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for _ in range(states):
            db.execute("INSERT INTO admins (admin_level) VALUES (4)")
        if existing_parent:
            db.execute("INSERT INTO admins (admin_level, iso_code) VALUES (2, 'US')")
    before = path.read_bytes()
    script = (OPS / "rebuild-with-repaired-admins.sh").read_text()
    # Execute only the read-only Python preflight, never the maintenance shell.
    preflight = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    result = subprocess.run(
        [sys.executable, "-", str(path)],
        input=preflight,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) == bool(states and not existing_parent)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["admins.sqlite"]
    if existing_parent:
        assert "already has a US parent" in result.stderr
    elif not states:
        assert "no state rows" in result.stderr
