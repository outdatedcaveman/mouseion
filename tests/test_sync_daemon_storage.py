from datetime import datetime, timedelta, timezone
import os
import time

from mouseion import sync_daemon


class _Settings:
    def __init__(self, value):
        self.value = value

    def get_setting(self, _key):
        return self.value


def test_db_backup_is_not_due_immediately_after_success():
    now = datetime.now(timezone.utc).isoformat()
    assert sync_daemon._db_backup_due(_Settings(now)) is False


def test_db_backup_is_due_after_interval():
    old = (
        datetime.now(timezone.utc)
        - timedelta(seconds=sync_daemon.DB_BACKUP_INTERVAL_SECONDS + 1)
    ).isoformat()
    assert sync_daemon._db_backup_due(_Settings(old)) is True


def test_cleanup_removes_only_stale_generated_snapshots(tmp_path, monkeypatch):
    system_temp = tmp_path / "system-temp"
    staging = tmp_path / "staging"
    system_temp.mkdir()
    staging.mkdir()
    stale = system_temp / "mouseion_backup_stale.db"
    fresh = staging / "mouseion_backup_fresh.db"
    unrelated = system_temp / "notes.db"
    for path in (stale, fresh, unrelated):
        path.write_bytes(b"test")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    monkeypatch.setattr(sync_daemon.tempfile, "gettempdir", lambda: system_temp)
    monkeypatch.setattr(sync_daemon, "_BACKUP_STAGING_DIR", staging)

    removed = sync_daemon._cleanup_stale_db_backups(max_age_seconds=1800)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()
    assert unrelated.exists()
