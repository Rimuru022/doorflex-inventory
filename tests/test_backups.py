"""Backup/restore tests — full snapshot (DB + photo directories), retention
across both the current .zip format and legacy .db-only backups, delete,
and the owner-panel HTTP routes (create/delete/restore, both file types).

Uses a real file-on-disk DB (unlike the in-memory `conn` fixture elsewhere)
since backup_db()/restore_everything_from_zip() operate on the filesystem,
not just a connection."""
import time
import zipfile
from pathlib import Path

import pytest

import database


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "data" / "inventory.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.delenv("BACKUP_PATH", raising=False)
    database.init_db(seed=True)
    return db_path


def _photo(dir_path, name, content=b"fake-jpeg-bytes"):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / name).write_bytes(content)


# ─── backup_db: full snapshot ───────────────────────────────────────────────


def test_backup_db_includes_photo_directories(db_env):
    db_path = db_env
    _photo(db_path.parent / "job_captures", "receipt1.jpg")
    _photo(db_path.parent / "winston_reports", "report1.jpg")

    path = database.backup_db()
    assert path is not None
    assert path.endswith(".zip")

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert "inventory.db" in names
        assert "job_captures/receipt1.jpg" in names
        assert "winston_reports/report1.jpg" in names


def test_backup_db_includes_a_future_photo_dir_automatically(db_env):
    """_backup_data_subdirs() auto-discovers any data subdirectory (excluding
    backups/) rather than a hardcoded list — a brand-new photo-saving feature
    must be captured with zero code changes to the backup routine."""
    db_path = db_env
    _photo(db_path.parent / "job_captures", "receipt1.jpg")
    _photo(db_path.parent / "some_future_photos", "evidence1.jpg")

    path = database.backup_db()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert "job_captures/receipt1.jpg" in names
        assert "some_future_photos/evidence1.jpg" in names


def test_backup_db_excludes_the_backups_dir_itself(db_env):
    path1 = database.backup_db()
    # A second backup must not recursively include the first backup zip —
    # the backups/ folder itself is excluded from _backup_data_subdirs().
    path2 = database.backup_db()
    with zipfile.ZipFile(path2) as zf:
        assert not any(n.startswith("backups/") for n in zf.namelist())


def test_backup_db_skips_missing_photo_dirs_without_error(db_env):
    # No job_captures/winston_reports directories exist at all yet — must
    # not error, and the zip just won't have those entries.
    path = database.backup_db()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        assert names == ["inventory.db"]


def test_backup_db_returns_none_without_a_live_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "data" / "inventory.db"))
    assert database.backup_db() is None


# ─── retention across formats ───────────────────────────────────────────────


def test_prune_keeps_newest_n_across_zip_and_legacy_formats(db_env, monkeypatch):
    monkeypatch.setattr(database, "BACKUP_RETENTION", 3)
    backup_dir = database.get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Two legacy-format files (older) + enough new backups to exceed retention.
    for i in range(2):
        (backup_dir / f"inventory.backup-2020010{i}-000000.db").write_bytes(b"legacy")
        time.sleep(0.01)
    for _ in range(4):
        database.backup_db()
        time.sleep(0.01)

    remaining = database.list_backups()
    assert len(remaining) == 3  # retention enforced across both patterns combined
    # The newest 3 are the most recent backup_db() calls, not the legacy files.
    assert all(r["kind"] == "full" for r in remaining)


def test_list_backups_reports_kind(db_env):
    backup_dir = database.get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "inventory.backup-20200101-000000.db").write_bytes(b"legacy")
    database.backup_db()

    kinds = {b["kind"] for b in database.list_backups()}
    assert kinds == {"full", "db_only"}


# ─── delete_backup ───────────────────────────────────────────────────────────


def test_delete_backup_removes_file(db_env):
    path = Path(database.backup_db())
    assert database.delete_backup(path.name) is True
    assert not path.exists()


def test_delete_backup_rejects_path_traversal(db_env):
    assert database.delete_backup("../../etc/passwd") is False
    assert database.delete_backup("sub/dir.zip") is False
    assert database.delete_backup("") is False


def test_delete_backup_missing_file_returns_false(db_env):
    assert database.delete_backup("doorflex-backup-99999999-000000.zip") is False


# ─── restore_everything_from_zip ────────────────────────────────────────────


def test_restore_everything_replaces_db_and_photos(db_env):
    db_path = db_env
    _photo(db_path.parent / "job_captures", "old.jpg", b"old-photo")
    backup_path = database.backup_db()

    # Mutate live state after the backup: new photo, DB gets an extra row.
    _photo(db_path.parent / "job_captures", "new.jpg", b"new-photo")
    conn = database.get_db()
    conn.execute("INSERT INTO warehouses (id, name) VALUES ('wh-temp', 'Temp')")
    conn.commit()
    conn.close()

    database.restore_everything_from_zip(backup_path)

    # DB reverted to the backed-up state (no wh-temp).
    conn = database.get_db()
    assert conn.execute("SELECT 1 FROM warehouses WHERE id='wh-temp'").fetchone() is None
    conn.close()

    # Photo directory replaced wholesale: old.jpg back, new.jpg (added after
    # the backup) gone.
    jc_dir = db_path.parent / "job_captures"
    assert (jc_dir / "old.jpg").read_bytes() == b"old-photo"
    assert not (jc_dir / "new.jpg").exists()


def test_restore_leaves_photo_dir_untouched_if_archive_lacks_it(db_env):
    db_path = db_env
    # Backup taken BEFORE any job_captures photos exist.
    backup_path = database.backup_db()
    # Photos appear only after the backup.
    _photo(db_path.parent / "job_captures", "keep-me.jpg", b"keep")

    database.restore_everything_from_zip(backup_path)

    # The archive had no job_captures/ entries at all, so the live folder
    # must be left alone rather than wiped.
    assert (db_path.parent / "job_captures" / "keep-me.jpg").exists()


def test_restore_everything_rejects_archive_without_db(db_env, tmp_path):
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("job_captures/photo.jpg", b"orphaned photo, no db")
    with pytest.raises(ValueError):
        database.restore_everything_from_zip(bad_zip)


# ─── HTTP: owner panel routes ───────────────────────────────────────────────


def _reload_app_with_tmp_db(tmp_path, monkeypatch, db_name):
    """Mirrors test_observability.py's test_owner_observe_renders_when_authenticated:
    the app is already imported (with the real DATABASE_PATH/ADMIN_* baked into
    config's module constants) by an earlier test in the session, so point the
    DB at a tmp path and reload config -> database -> main to pick it up. Owner
    creds come from whatever the real environment/.env already has — same as
    that existing test — since ADMIN_ENABLED is a config-import-time constant."""
    from database import init_db
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / db_name))
    init_db(seed=True)
    import importlib
    import config as cfg_mod
    importlib.reload(cfg_mod)
    import database as db_mod
    importlib.reload(db_mod)
    import main as main_mod
    importlib.reload(main_mod)
    from fastapi.testclient import TestClient
    return TestClient(main_mod.app), main_mod.ADMIN_PATH, cfg_mod.ADMIN_ACCESS_PASSWORD


def test_http_backup_create_delete_and_restore(tmp_path, monkeypatch):
    client, admin, password = _reload_app_with_tmp_db(tmp_path, monkeypatch, "data/inventory.db")
    _photo(tmp_path / "data" / "job_captures", "receipt.jpg")

    login = client.post(f"{admin}/login", data={"password": password}, follow_redirects=False)
    assert login.status_code == 302

    # Create
    r = client.post(f"{admin}/backups/create", follow_redirects=False)
    assert r.status_code == 302
    listing = client.get(f"{admin}/backups")
    assert ".zip" in listing.text

    # Extract the created backup's filename from the listing page.
    import re
    m = re.search(r'value="(doorflex-backup-[^"]+\.zip)"', listing.text)
    assert m, "backup filename not found in listing page"
    fname = m.group(1)

    # Download works
    dl = client.get(f"{admin}/backups/download?file={fname}")
    assert dl.status_code == 200
    zip_bytes = dl.content

    # Restore from the downloaded zip (round-trip)
    r = client.post(
        f"{admin}/backups/restore",
        files={"file": (fname, zip_bytes, "application/zip")},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert (tmp_path / "data" / "job_captures" / "receipt.jpg").exists()

    # Delete
    r = client.post(f"{admin}/backups/delete", data={"file": fname}, follow_redirects=False)
    assert r.status_code == 302
    # The filename legitimately appears in the "Deleted backup: <name>" flash
    # confirmation, so check the download link (i.e. the table row) is gone
    # rather than the bare name being absent from the page entirely.
    assert f'file={fname}"' not in client.get(f"{admin}/backups").text

    # Delete rejects path traversal even via the HTTP form
    r = client.post(f"{admin}/backups/delete", data={"file": "../../etc/passwd"})
    assert r.status_code == 404


def test_http_restore_rejects_garbage_upload(tmp_path, monkeypatch):
    client, admin, password = _reload_app_with_tmp_db(tmp_path, monkeypatch, "data2/inventory.db")
    client.post(f"{admin}/login", data={"password": password})

    r = client.post(
        f"{admin}/backups/restore",
        files={"file": ("notarealbackup.txt", b"just some text", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "isn&#39;t a recognized backup" in client.get(f"{admin}/backups").text
    # DB must be untouched — still a valid, queryable database.
    conn = database.get_db()
    assert conn.execute("SELECT 1").fetchone() is not None
    conn.close()
