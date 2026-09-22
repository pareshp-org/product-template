#!/usr/bin/env python3
"""Automated endpoint verification tests for MasterSpec Section 41.2 endpoints and domain APIs."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

# Add parent directory to sys.path to import app
PARENT_DIR = Path(__file__).resolve().parent.parent
if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))

import app


class TestEndpoints(unittest.TestCase):
    server: ThreadingHTTPServer
    server_thread: threading.Thread
    port: int
    temp_dir: tempfile.TemporaryDirectory
    db_path: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test_app.db"
        os.environ["DB_PATH"] = str(cls.db_path)
        os.environ["PRODUCT_ID"] = "product-template"
        os.environ["LOG_LEVEL"] = "error"

        # Apply migrations and seed
        cls.db = app.DatabaseManager(cls.db_path)
        cls.db.apply_migrations(PARENT_DIR / "migrations")
        cls.db.seed_data(PARENT_DIR / "seed" / "seed.json")

        # Set DB on request handler
        app.ServiceRequestHandler.db = cls.db

        # Bind to an ephemeral port
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.ServiceRequestHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2.0)
        try:
            cls.temp_dir.cleanup()
        except Exception:
            pass

    def _get(self, path: str) -> tuple[int, dict | str, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                headers = dict(resp.getheaders())
                content_type = headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8")
                if "application/json" in content_type:
                    return status, json.loads(raw), headers
                return status, raw, headers
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8")
            try:
                return err.code, json.loads(raw), dict(err.headers)
            except Exception:
                return err.code, raw, dict(err.headers)

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def _delete(self, path: str) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method="DELETE")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_01_health_endpoint(self) -> None:
        """Section 41.2: GET /health returns 200, status healthy, and dependency status."""
        status, data, _ = self._get("/health")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("status"), "healthy")
        self.assertEqual(data.get("application"), "healthy")
        self.assertIn("database", data.get("dependencies", {}))
        self.assertEqual(data["dependencies"]["database"], "healthy")

    def test_02_version_endpoint(self) -> None:
        """Section 41.2: GET /version returns 200, contract_version 2, and digest."""
        status, data, _ = self._get("/version")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, dict)
        self.assertEqual(data.get("contract_version"), 2)
        self.assertEqual(data.get("product_id"), "product-template")
        self.assertTrue(data.get("digest", "").startswith("sha256:"))

    def test_03_metrics_endpoint(self) -> None:
        """Section 41.2: GET /metrics returns standard Prometheus exposition text."""
        status, text, headers = self._get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers.get("Content-Type", ""))
        self.assertIn("up 1", text)
        self.assertIn("http_requests_total", text)
        self.assertIn("domain_entities_total", text)

    def test_04_domain_crud(self) -> None:
        """Verify REST CRUD operations on /api/v1/items."""
        # List initial seeded items
        status, list_data, _ = self._get("/api/v1/items")
        self.assertEqual(status, 200)
        initial_count = list_data.get("count", 0)
        self.assertGreater(initial_count, 0)

        # Create new item
        create_payload = {
            "name": "Integration Test Entity",
            "category": "test",
            "status": "active",
            "payload": {"source": "automated_test", "priority": "high"},
        }
        status, created = self._post("/api/v1/items", create_payload)
        self.assertEqual(status, 201)
        item_id = created.get("id")
        self.assertIsNotNone(item_id)
        self.assertEqual(created.get("name"), "Integration Test Entity")

        # Retrieve created item
        status, fetched, _ = self._get(f"/api/v1/items/{item_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched.get("name"), "Integration Test Entity")
        self.assertEqual(fetched.get("payload", {}).get("priority"), "high")

        # Delete created item
        status, del_resp = self._delete(f"/api/v1/items/{item_id}")
        self.assertEqual(status, 200)
        self.assertTrue(del_resp.get("deleted"))

        # Verify not found after deletion
        status, not_found_resp, _ = self._get(f"/api/v1/items/{item_id}")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
