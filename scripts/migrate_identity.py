#!/usr/bin/env python3
"""
One-time identity migration: household.yaml flat config -> normalized SQLite tables.

Creates and seeds:
  households, members, member_channels, member_preferences, household_config

Updates all existing records to use new hh_XXXX / usr_XXXX IDs.
Rewrites household.yaml to a single line: household_id: hh_XXXX

Safe to run twice — exits immediately if already migrated.

Usage:
    python scripts/migrate_identity.py
    python scripts/migrate_identity.py --db-path /data/mithra/mithra.db
    python scripts/migrate_identity.py --db-path /data/mithra/mithra.db --yaml-path household.yaml
"""

from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS households (
            id           TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            timezone     TEXT NOT NULL DEFAULT 'UTC',
            locale       TEXT NOT NULL DEFAULT 'en-US',
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS members (
            id           TEXT PRIMARY KEY,
            household_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            role         TEXT NOT NULL DEFAULT 'member',
            status       TEXT NOT NULL DEFAULT 'active',
            invited_by   TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS member_channels (
            user_id      TEXT NOT NULL,
            household_id TEXT NOT NULL,
            channel      TEXT NOT NULL,
            channel_id   TEXT NOT NULL,
            verified     INTEGER NOT NULL DEFAULT 1,
            created_at   TEXT NOT NULL,
            PRIMARY KEY (user_id, channel)
        );

        CREATE TABLE IF NOT EXISTS member_preferences (
            user_id      TEXT NOT NULL,
            household_id TEXT NOT NULL,
            key          TEXT NOT NULL,
            value        TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );

        CREATE TABLE IF NOT EXISTS household_config (
            household_id TEXT NOT NULL,
            key          TEXT NOT NULL,
            value        TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (household_id, key)
        );
    """)
    conn.commit()


def _already_migrated(conn: sqlite3.Connection) -> bool:
    try:
        count = conn.execute("SELECT COUNT(*) FROM households").fetchone()[0]
        return count > 0
    except sqlite3.OperationalError:
        return False


def _records_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='records'"
    ).fetchone()
    return row is not None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Mithra identity to normalized SQLite tables."
    )
    parser.add_argument(
        "--db-path",
        default="/data/mithra/mithra.db",
        help="Path to the Mithra SQLite database (default: /data/mithra/mithra.db)",
    )
    parser.add_argument(
        "--yaml-path",
        default="household.yaml",
        help="Path to household.yaml (default: household.yaml)",
    )
    args = parser.parse_args()

    yaml_path = Path(args.yaml_path)
    if not yaml_path.exists():
        print(f"ERROR: {yaml_path} not found.", file=sys.stderr)
        sys.exit(1)

    with open(yaml_path) as f:
        config = yaml.safe_load(f) or {}

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    _create_tables(conn)

    if _already_migrated(conn):
        print("Already migrated. Nothing to do.")
        conn.close()
        return

    old_household_id = config.get("household_id", "mithra_household_001")
    members_yaml = config.get("members", [])

    if not members_yaml:
        print("ERROR: no members found in household.yaml. Cannot migrate.", file=sys.stderr)
        sys.exit(1)

    # --- Generate new IDs ---
    new_household_id = "hh_" + secrets.token_hex(4)
    # old_user_id -> new usr_XXXX, in member declaration order
    id_map: dict[str, str] = {
        m["user_id"]: "usr_" + secrets.token_hex(4) for m in members_yaml
    }

    # --- Print generated IDs clearly ---
    sep = "=" * 60
    print(sep)
    print("GENERATED IDs — record these before proceeding:")
    print(sep)
    print(f"  household   (new): {new_household_id}")
    print(f"              (was): {old_household_id}")
    for m in members_yaml:
        old_id = m["user_id"]
        new_id = id_map[old_id]
        name = m.get("display_name", old_id)
        print(f"  {name:<20s} (new): {new_id}   (was: {old_id})")
    print(sep)
    print()

    answer = input("Proceed with migration? (yes/no): ").strip().lower()
    if answer != "yes":
        print("Aborted.")
        conn.close()
        return

    now = _now()

    # --- Seed households ---
    conn.execute(
        """
        INSERT OR IGNORE INTO households
            (id, display_name, timezone, locale, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            new_household_id,
            "Gaddampalli Family",
            config.get("timezone", "America/Chicago"),
            config.get("locale", "en-US"),
            now,
            now,
        ),
    )
    conn.commit()

    # --- Seed members ---
    # First member with is_admin=True becomes owner; remaining admins become admin;
    # everyone else becomes member. All seeded as status=active.
    owner_assigned = False
    for m in members_yaml:
        old_id = m["user_id"]
        new_id = id_map[old_id]
        is_admin = m.get("is_admin", False)
        if is_admin and not owner_assigned:
            role = "owner"
            owner_assigned = True
        elif is_admin:
            role = "admin"
        else:
            role = "member"
        conn.execute(
            """
            INSERT OR IGNORE INTO members
                (id, household_id, display_name, role, status, invited_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                new_household_id,
                m.get("display_name", old_id),
                role,
                "active",
                None,
                now,
                now,
            ),
        )
    conn.commit()

    # --- Migrate member_channels from records table ---
    channels_migrated = 0
    if _records_table_exists(conn):
        rows = conn.execute(
            "SELECT key, user_id, data FROM records WHERE domain='identity' AND layer='state'"
        ).fetchall()
        for row in rows:
            try:
                data = json.loads(row["data"])
            except (json.JSONDecodeError, TypeError):
                continue

            if data.get("type") != "telegram_mapping":
                continue

            telegram_id = str(data.get("telegram_id", ""))
            old_user_id = data.get("user_id", "")
            new_user_id = id_map.get(old_user_id)
            if not telegram_id or not new_user_id:
                continue

            conn.execute(
                """
                INSERT OR IGNORE INTO member_channels
                    (user_id, household_id, channel, channel_id, verified, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (new_user_id, new_household_id, "telegram", telegram_id, 1, now),
            )
            channels_migrated += 1
        conn.commit()

    # --- Update all existing records with new IDs ---
    records_updated = 0
    if _records_table_exists(conn):
        cur = conn.execute(
            "UPDATE records SET household_id=? WHERE household_id=?",
            (new_household_id, old_household_id),
        )
        records_updated += cur.rowcount

        for old_id, new_id in id_map.items():
            cur = conn.execute(
                "UPDATE records SET user_id=? WHERE user_id=?",
                (new_id, old_id),
            )
            records_updated += cur.rowcount

        # Identity records are now in member_channels — remove from records table
        conn.execute("DELETE FROM records WHERE domain='identity'")
        conn.commit()

    # --- Rewrite household.yaml to single line ---
    yaml_path.write_text(f"household_id: {new_household_id}\n")

    # --- Summary ---
    print()
    print(sep)
    print("Migration complete.")
    print(f"  Households seeded : 1  ({new_household_id})")
    print(f"  Members seeded    : {len(members_yaml)}")
    print(f"  Channels migrated : {channels_migrated}")
    print(f"  Records updated   : {records_updated}")
    print(f"  household.yaml    : rewritten to single line")
    print(sep)

    conn.close()


if __name__ == "__main__":
    main()
