#!/usr/bin/env python3
"""Disaster Recovery & Database Resilience Unit Tests for product-template.

Verifies WAL mode, PRAGMA configuration, online backup, corruption detection,
and seamless disaster recovery restore without data loss.
Zero external pip dependencies required.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Add parent directory to sys.path
PARENT_DIR = Path(__file__).resolve().parent.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

import app


class TestproducttemplateDatabaseResilience(unittest.TestCase):
    """Resilience and Disaster Recovery test suite for product-template."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "resilience_test.db"
        self.backup_path = Path(self.temp_dir.name) / "resilience_test.backup.db"
        self.db = app.DatabaseManager(self.db_path)
        self.db.apply_migrations(PARENT_DIR / "migrations")

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_wal_and_pragmas_configuration(self) -> None:
        """Verify WAL mode, busy_timeout, foreign_keys, and synchronous pragmas."""
        res = self.db.verify_integrity()
        self.assertTrue(res["healthy"])
        self.assertEqual(res["journal_mode"].lower(), "wal")
        self.assertEqual(res["foreign_keys"], 1)
        self.assertGreaterEqual(res["busy_timeout"], 30000)
        self.assertTrue(res["integrity_ok"])
        self.assertTrue(res["quick_ok"])
        self.assertTrue(res["foreign_key_ok"])

    def test_synchronous_modes(self) -> None:
        """Verify synchronous PRAGMA handles NORMAL (1) and FULL (2)."""
        # Default is NORMAL (1)
        conn = self.db.get_connection()
        sync_val = conn.execute("PRAGMA synchronous;").fetchone()[0]
        conn.close()
        self.assertEqual(sync_val, 1)

        # Test FULL (2) via environment
        os.environ["SQLITE_SYNCHRONOUS"] = "FULL"
        try:
            conn2 = self.db.get_connection()
            sync_val2 = conn2.execute("PRAGMA synchronous;").fetchone()[0]
            conn2.close()
            self.assertEqual(sync_val2, 2)
        finally:
            del os.environ["SQLITE_SYNCHRONOUS"]

    def test_wal_checkpoint_modes(self) -> None:
        """Verify PASSIVE, FULL, RESTART, and TRUNCATE checkpoints."""
        # Insert records into WAL
        for i in range(25):
            self.db.create_item({"name": f"Checkpoint Item {i}", "category": "test"})

        # Checkpoint PASSIVE
        pass_res = self.db.checkpoint("PASSIVE")
        self.assertEqual(pass_res["mode"], "PASSIVE")
        self.assertEqual(pass_res["busy"], 0)

        # Insert more records
        for i in range(25, 50):
            self.db.create_item({"name": f"Checkpoint Item {i}", "category": "test"})

        # Checkpoint TRUNCATE
        trunc_res = self.db.checkpoint("TRUNCATE")
        self.assertEqual(trunc_res["mode"], "TRUNCATE")
        self.assertEqual(trunc_res["busy"], 0)

        # Verify integrity post checkpoint
        res = self.db.verify_integrity()
        self.assertTrue(res["healthy"])
        self.assertEqual(res["row_count"], 50)

    def test_online_backup_api(self) -> None:
        """Verify Python sqlite3.Connection.backup() creates a valid verified backup."""
        self.db.seed_data(PARENT_DIR / "seed" / "seed.json")
        baseline_items = self.db.list_items()
        self.assertGreater(len(baseline_items), 0)

        out_path = self.db.backup(self.backup_path)
        self.assertTrue(out_path.is_file())
        self.assertGreater(out_path.stat().st_size, 0)

        # Verify backup integrity
        backup_db = app.DatabaseManager(self.backup_path)
        backup_report = backup_db.verify_integrity()
        self.assertTrue(backup_report["healthy"])
        self.assertEqual(backup_report["row_count"], len(baseline_items))

    def test_sqlite3_cli_backup_parity(self) -> None:
        """Verify native sqlite3 CLI backup produces parity with database."""
        self.db.seed_data(PARENT_DIR / "seed" / "seed.json")
        cli_backup_path = Path(self.temp_dir.name) / "cli_backup.db"

        cmd = f'sqlite3 "{self.db_path}" ".backup \'{cli_backup_path}\'"'
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"sqlite3 CLI backup failed: {proc.stderr}")
        self.assertTrue(cli_backup_path.is_file())

        backup_db = app.DatabaseManager(cli_backup_path)
        backup_report = backup_db.verify_integrity()
        self.assertTrue(backup_report["healthy"])
        self.assertEqual(backup_report["row_count"], self.db.count_entities())

    def test_concurrent_writer_during_backup(self) -> None:
        """Verify online backup succeeds concurrently while active writes occur in WAL mode."""
        self.db.seed_data(PARENT_DIR / "seed" / "seed.json")
        stop_event = threading.Event()
        writer_errors: list[str] = []
        written_count = [0]

        def writer_loop() -> None:
            c = 0
            while not stop_event.is_set():
                try:
                    self.db.create_item({
                        "name": f"Concurrent Item {c}",
                        "category": "stress",
                        "payload": {"idx": c, "ts": time.time()}
                    })
                    c += 1
                    written_count[0] = c
                    time.sleep(0.005)
                except Exception as err:
                    writer_errors.append(str(err))
                    break

        writer_thread = threading.Thread(target=writer_loop, daemon=True)
        writer_thread.start()

        # Let some writes happen, then execute online backup
        time.sleep(0.05)
        try:
            self.db.backup(self.backup_path)
            backup_ok = True
        except Exception as err:
            backup_ok = False
            writer_errors.append(f"Backup failed: {err}")

        stop_event.set()
        writer_thread.join(timeout=3.0)

        self.assertTrue(backup_ok)
        self.assertEqual(len(writer_errors), 0, f"Errors encountered during concurrent backup: {writer_errors}")
        self.assertGreater(written_count[0], 0)

        # Verify backup integrity
        backup_db = app.DatabaseManager(self.backup_path)
        backup_report = backup_db.verify_integrity()
        self.assertTrue(backup_report["healthy"])

    def test_seed_backup_corruption_and_seamless_restore(self) -> None:
        """End-to-end disaster recovery: seed data, backup, corrupt database, restore, verify zero data loss."""
        # 1. Seed database with baseline fixture records
        seeded_count = self.db.seed_data(PARENT_DIR / "seed" / "seed.json")
        self.assertGreater(seeded_count, 0)

        # Add additional domain records
        for i in range(10):
            self.db.create_item({
                "name": f"Production Work Item #{i + 1}",
                "category": "critical_workload",
                "status": "processing",
                "payload": {"uuid": f"item-uuid-{i}", "checksum": hashlib.sha256(f"record-{i}".encode()).hexdigest()},
            })

        # 2. Capture canonical baseline records & hash digest
        baseline_items = self.db.list_items(limit=1000)
        baseline_count = len(baseline_items)
        canonical_json = json.dumps(baseline_items, sort_keys=True)
        canonical_digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        # 3. Create verified online backup
        self.db.backup(self.backup_path)
        self.assertTrue(self.backup_path.is_file())

        # 4. Simulate catastrophic corruption: write garbage bytes over database file
        # Checkpoint first to close WAL handles
        self.db.checkpoint("TRUNCATE")
        with open(self.db_path, "r+b") as f:
            f.seek(0)
            f.write(b"CORRUPTED_GARBAGE_BYTES_OVERWRITING_SQLITE_HEADER_AND_PAGES_DEAD_BEEF_0000000000000000")
            f.truncate(1024)

        # Verify that SQLite detects the corruption
        corrupt_check = self.db.check_health()
        self.assertFalse(corrupt_check[0], "Database should be unhealthy after corruption!")

        # 5. Execute Disaster Recovery Restore
        restored = self.db.restore(self.backup_path)
        self.assertTrue(restored, "Restore operation must succeed")

        # 6. Verify restored database integrity and zero data loss
        restored_report = self.db.verify_integrity()
        self.assertTrue(restored_report["healthy"], f"Restored database must be healthy: {restored_report}")
        self.assertEqual(restored_report["journal_mode"].lower(), "wal")
        self.assertEqual(restored_report["row_count"], baseline_count)

        # Validate byte-exact and content-exact parity for all items
        restored_items = self.db.list_items(limit=1000)
        restored_json = json.dumps(restored_items, sort_keys=True)
        restored_digest = hashlib.sha256(restored_json.encode("utf-8")).hexdigest()

        self.assertEqual(canonical_digest, restored_digest, "Restored records must match pre-disaster state 100%!")


if __name__ == "__main__":
    unittest.main()
