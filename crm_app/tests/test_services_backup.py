"""
Tests for BackupService (app/services.py)

This is the highest-risk service in the app: it swaps out the live database.
Coverage here is deliberately paranoid - a full download->restore round trip,
plus every rejection path that must leave existing data untouched:
  - not a zip / no manifest / wrong signature / newer format
  - zip-slip (path traversal) archives
  - a bundled file that isn't really a SQLite app DB
  - a backup from a newer schema version
"""

import io
import json
import os
import sqlite3
import zipfile

import pytest

from app.services import (
    BackupService, BACKUP_SIGNATURE, BACKUP_FORMAT_VERSION,
    _MANIFEST_NAME, _DB_ARCNAME,
)
from app.database import Database, SCHEMA_VERSION
from app.exceptions import ValidationError, NotFoundError


class FakeUpload:
    """Stands in for a werkzeug FileStorage: has .filename and .save(path)."""

    def __init__(self, data: bytes, filename="backup.zip"):
        self._data = data
        self.filename = filename

    def save(self, path):
        with open(path, "wb") as f:
            f.write(self._data)


@pytest.fixture
def backup_service(container, tmp_config):
    return BackupService(
        container.db, tmp_config.DATABASE_PATH,
        tmp_config.PRODUCT_UPLOAD_FOLDER, tmp_config.SCHEMA_PATH,
    )


def _zip_bytes(members: dict) -> bytes:
    """Build an in-memory zip from {arcname: bytes}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _good_manifest(**over):
    m = {
        "signature": BACKUP_SIGNATURE,
        "format_version": BACKUP_FORMAT_VERSION,
        "app": "crm",
        "schema_version": SCHEMA_VERSION,
        "created_at": "2026-01-01T00:00:00",
        "db_filename": _DB_ARCNAME,
        "contents": ["database", "uploads"],
    }
    m.update(over)
    return json.dumps(m).encode()


# ==========================================================================
# Creating a backup
# ==========================================================================
class TestCreateBackupZip:
    def test_produces_a_zip_with_manifest_and_db(self, backup_service, seed):
        zip_path, download_name = backup_service.create_backup_zip()
        try:
            assert download_name.startswith("crm-backup-") and download_name.endswith(".zip")
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                assert _MANIFEST_NAME in names
                assert _DB_ARCNAME in names
                manifest = json.loads(zf.read(_MANIFEST_NAME))
            assert manifest["signature"] == BACKUP_SIGNATURE
            assert manifest["schema_version"] == SCHEMA_VERSION
        finally:
            os.remove(zip_path)

    def test_includes_product_upload_files(self, backup_service, tmp_config, seed):
        os.makedirs(tmp_config.PRODUCT_UPLOAD_FOLDER, exist_ok=True)
        with open(os.path.join(tmp_config.PRODUCT_UPLOAD_FOLDER, "photo.png"), "wb") as f:
            f.write(b"fake-image")
        zip_path, _ = backup_service.create_backup_zip()
        try:
            with zipfile.ZipFile(zip_path) as zf:
                assert "uploads/products/photo.png" in zf.namelist()
        finally:
            os.remove(zip_path)


# ==========================================================================
# Full round trip: back up, change data, restore, verify rollback
# ==========================================================================
class TestRestoreRoundTrip:
    def test_restore_brings_back_the_snapshotted_data(self, backup_service, container, seed):
        # Snapshot while only the seeded users exist.
        zip_path, _ = backup_service.create_backup_zip()
        try:
            # Mutate: add a tenant that is NOT in the backup.
            container.tenant_repo.create("Added After Backup", "after")
            assert any(t.slug == "after" for t in container.tenant_repo.list_active())

            with open(zip_path, "rb") as f:
                payload = f.read()
            result = backup_service.restore_from_zip(FakeUpload(payload))

            assert result["current_schema_version"] == SCHEMA_VERSION
            # The post-backup tenant is gone; the seeded one survived.
            slugs = [t.slug for t in container.tenant_repo.list_active()]
            assert "after" not in slugs
            assert "test-exports" in slugs
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)

    def test_restore_writes_a_pre_restore_snapshot(self, backup_service, seed):
        zip_path, _ = backup_service.create_backup_zip()
        try:
            with open(zip_path, "rb") as f:
                backup_service.restore_from_zip(FakeUpload(f.read()))
            names = [b["name"] for b in backup_service.list_auto_backups()]
            assert any("pre_restore" in n for n in names)
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)


# ==========================================================================
# Rejection paths - each must raise and leave data intact
# ==========================================================================
class TestRestoreRejections:
    def test_no_file_rejected(self, backup_service):
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(None)

    def test_empty_filename_rejected(self, backup_service):
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(b"x", filename=""))

    def test_not_a_zip_rejected(self, backup_service):
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(b"i am not a zip file"))

    def test_zip_without_manifest_rejected(self, backup_service):
        payload = _zip_bytes({"random.txt": b"hello"})
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(payload))

    def test_unparseable_manifest_rejected(self, backup_service):
        payload = _zip_bytes({_MANIFEST_NAME: b"{not json"})
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(payload))

    def test_wrong_signature_rejected(self, backup_service):
        payload = _zip_bytes({_MANIFEST_NAME: _good_manifest(signature="some-other-app")})
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(payload))

    def test_newer_format_version_rejected(self, backup_service):
        payload = _zip_bytes({
            _MANIFEST_NAME: _good_manifest(format_version=BACKUP_FORMAT_VERSION + 1)})
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(payload))

    def test_missing_db_member_rejected(self, backup_service):
        payload = _zip_bytes({_MANIFEST_NAME: _good_manifest()})
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(payload))

    def test_db_member_that_is_not_sqlite_rejected(self, backup_service):
        payload = _zip_bytes({
            _MANIFEST_NAME: _good_manifest(),
            _DB_ARCNAME: b"definitely not a sqlite file",
        })
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(payload))

    def test_sqlite_db_without_our_tables_rejected(self, backup_service, tmp_path):
        # A real SQLite file, but not one of our app databases.
        stranger = str(tmp_path / "stranger.db")
        conn = sqlite3.connect(stranger)
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()
        with open(stranger, "rb") as f:
            db_bytes = f.read()
        payload = _zip_bytes({_MANIFEST_NAME: _good_manifest(), _DB_ARCNAME: db_bytes})
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(payload))

    def test_backup_from_newer_schema_version_rejected(self, backup_service, container, tmp_path):
        # A genuine app DB, but stamped with a schema version we can't downgrade to.
        newer = str(tmp_path / "newer.db")
        container.db.create_backup_copy(newer)
        conn = sqlite3.connect(newer)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        conn.commit()
        conn.close()
        with open(newer, "rb") as f:
            db_bytes = f.read()
        payload = _zip_bytes({_MANIFEST_NAME: _good_manifest(), _DB_ARCNAME: db_bytes})
        with pytest.raises(ValidationError) as exc:
            backup_service.restore_from_zip(FakeUpload(payload))
        assert "newer app version" in str(exc.value)

    def test_failed_restore_leaves_live_data_intact(self, backup_service, container, seed):
        before = [t.slug for t in container.tenant_repo.list_active()]
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(b"garbage, not a zip"))
        after = [t.slug for t in container.tenant_repo.list_active()]
        assert before == after


# ==========================================================================
# Zip-slip / path traversal guard
# ==========================================================================
class TestZipSlipGuard:
    def test_parent_traversal_member_rejected(self, backup_service, tmp_path):
        with pytest.raises(ValidationError):
            BackupService._assert_no_zip_slip(["../evil.txt"], str(tmp_path))

    def test_absolute_path_member_rejected(self, backup_service, tmp_path):
        with pytest.raises(ValidationError):
            BackupService._assert_no_zip_slip(["/etc/passwd"], str(tmp_path))

    def test_normal_members_allowed(self, tmp_path):
        # Should not raise.
        BackupService._assert_no_zip_slip(
            [_MANIFEST_NAME, _DB_ARCNAME, "uploads/products/a.png"], str(tmp_path))

    def test_traversal_archive_is_rejected_end_to_end(self, backup_service):
        payload = _zip_bytes({"../escaped.txt": b"evil", _MANIFEST_NAME: _good_manifest()})
        with pytest.raises(ValidationError):
            backup_service.restore_from_zip(FakeUpload(payload))


# ==========================================================================
# Auto-backup listing / safe path resolution
# ==========================================================================
class TestAutoBackups:
    def test_list_is_empty_when_no_backups_dir(self, backup_service):
        assert backup_service.list_auto_backups() == []

    def test_list_reports_snapshots(self, backup_service, seed):
        backup_service._snapshot_current("manual_test")
        items = backup_service.list_auto_backups()
        assert len(items) >= 1
        entry = items[0]
        assert entry["name"].endswith(".db")
        assert "size_mb" in entry and "modified" in entry

    def test_get_path_rejects_traversal(self, backup_service):
        with pytest.raises(ValidationError):
            backup_service.get_auto_backup_path("../../etc/passwd")

    def test_get_path_rejects_non_db_extension(self, backup_service):
        with pytest.raises(ValidationError):
            backup_service.get_auto_backup_path("notes.txt")

    def test_get_path_missing_file_is_not_found(self, backup_service):
        with pytest.raises(NotFoundError):
            backup_service.get_auto_backup_path("nope.db")

    def test_get_path_returns_existing_snapshot(self, backup_service, seed):
        backup_service._snapshot_current("findme")
        name = backup_service.list_auto_backups()[0]["name"]
        assert os.path.isfile(backup_service.get_auto_backup_path(name))


# ==========================================================================
# _assert_valid_app_db in isolation
# ==========================================================================
class TestAssertValidAppDb:
    def test_accepts_a_real_app_db(self, container, tmp_path):
        path = str(tmp_path / "ok.db")
        container.db.create_backup_copy(path)
        BackupService._assert_valid_app_db(path)  # must not raise

    def test_rejects_non_sqlite_bytes(self, tmp_path):
        path = str(tmp_path / "bad.db")
        with open(path, "wb") as f:
            f.write(b"nope")
        with pytest.raises(ValidationError):
            BackupService._assert_valid_app_db(path)
