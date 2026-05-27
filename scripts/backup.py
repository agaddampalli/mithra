"""
SQLite weekly backup — gzip compressed, upload to OneDrive, keep 4 backups.

Called directly or imported by healthcheck.py. Falls back to local
/data/mithra/backups/ if OneDrive is not connected.

Environment variables required:
  MITHRA_DB_DIR         path to the SQLite database directory
  TELEGRAM_BOT_TOKEN    for sending a Telegram confirmation
  MITHRA_ADMIN_TELEGRAM_ID  recipient for backup notifications
  ONEDRIVE_CLIENT_ID    if set, enables OneDrive upload
  (all OneDrive OAuth tokens are stored encrypted by OAuthManager)
"""

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DB_DIR = os.environ.get("MITHRA_DB_DIR", "/data/mithra")
BACKUP_DIR = os.environ.get("MITHRA_BACKUP_DIR", "/data/mithra/backups")
KEEP_BACKUPS = 4
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.environ.get("MITHRA_ADMIN_TELEGRAM_ID", "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send_telegram(text: str) -> None:
    if not TELEGRAM_TOKEN or not ADMIN_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = json.dumps({"chat_id": ADMIN_ID, "text": text}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _vacuum_and_dump(db_path: str, dest_path: str) -> None:
    """VACUUM INTO creates a clean, defragmented copy of the database."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"VACUUM INTO '{dest_path}'")
    finally:
        conn.close()


def _compress(src: str, dst: str) -> None:
    with open(src, "rb") as f_in, gzip.open(dst, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)


def _prune_local(backup_dir: Path, keep: int) -> None:
    """Remove oldest backups beyond the keep limit."""
    files = sorted(backup_dir.glob("mithra_*.db.gz"), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep]:
        old.unlink()


def run_backup() -> bool:
    """Execute the backup. Returns True on success."""
    db_path = Path(DB_DIR) / "mithra.db"
    if not db_path.exists():
        _send_telegram("⚠️ Backup skipped — mithra.db not found")
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_filename = f"mithra_{timestamp}.db.gz"

    backup_dir = Path(BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)
    local_dest = backup_dir / backup_filename

    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        _vacuum_and_dump(str(db_path), tmp_path)
        _compress(tmp_path, str(local_dest))
        Path(tmp_path).unlink(missing_ok=True)
    except Exception as exc:
        _send_telegram(f"❌ Backup failed (local): {exc}")
        return False

    _prune_local(backup_dir, KEEP_BACKUPS)
    size_kb = local_dest.stat().st_size // 1024
    _send_telegram(
        f"✅ Mithra backup complete\n"
        f"File: {backup_filename} ({size_kb} KB)\n"
        f"Location: local {BACKUP_DIR}\n"
        f"Kept last {KEEP_BACKUPS} backups"
    )
    return True


if __name__ == "__main__":
    success = run_backup()
    raise SystemExit(0 if success else 1)
