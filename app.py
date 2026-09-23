#!/usr/bin/env python3
"""Enterprise Service Engine for MultiProduct OS.

Implements MasterSpec Section 41.2 three-endpoint rule (/health, /version, /metrics),
domain CRUD APIs, SQLite database persistence, migrations runner, and seed loader.
Zero external pip dependencies required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "app.db"
MIGRATIONS_DIR = BASE_DIR / "migrations"
SEED_DIR = BASE_DIR / "seed"


def get_db_path() -> Path:
    env_db = os.environ.get("DB_PATH")
    if env_db:
        return Path(env_db)
    return DEFAULT_DB_PATH


def get_product_id() -> str:
    env_id = os.environ.get("PRODUCT_ID")
    if env_id:
        return env_id
    product_yaml = BASE_DIR / "product.yaml"
    if product_yaml.is_file():
        try:
            for line in product_yaml.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("product_id:"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
    return BASE_DIR.name


def get_git_sha() -> str:
    env_sha = os.environ.get("GIT_SHA") or os.environ.get("COMMIT_SHA")
    if env_sha:
        return env_sha
    git_head = BASE_DIR / ".git" / "HEAD"
    if git_head.is_file():
        try:
            ref = git_head.read_text(encoding="utf-8").strip()
            if ref.startswith("ref: "):
                ref_path = BASE_DIR / ".git" / ref[5:]
                if ref_path.is_file():
                    return ref_path.read_text(encoding="utf-8").strip()[:40]
            else:
                return ref[:40]
        except Exception:
            pass
    return "0000000000000000000000000000000000000000"


def get_artifact_digest() -> str:
    env_digest = os.environ.get("ARTIFACT_DIGEST")
    if env_digest:
        return env_digest
    sha = get_git_sha()
    return f"sha256:{hashlib.sha256(sha.encode('utf-8')).hexdigest()}"


class MetricsCollector:
    """In-memory thread-safe metrics collector formatted for Prometheus exposition."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.request_counts: Dict[Tuple[str, str, int], int] = {}
        self.request_durations: List[float] = []

    def record_request(self, method: str, path: str, status_code: int, duration_sec: float) -> None:
        with self._lock:
            key = (method, path, status_code)
            self.request_counts[key] = self.request_counts.get(key, 0) + 1
            self.request_durations.append(duration_sec)
            if len(self.request_durations) > 1000:
                self.request_durations = self.request_durations[-1000:]

    def render_prometheus(self, product_id: str, db_entity_count: int) -> str:
        with self._lock:
            counts = dict(self.request_counts)
            durations = list(self.request_durations) or [0.001]
            start_t = self.start_time

        lines: List[str] = [
            "# HELP up Service availability status",
            "# TYPE up gauge",
            "up 1",
            "",
            "# HELP app_version_info Application version and contract metadata",
            "# TYPE app_version_info gauge",
            f'app_version_info{{product_id="{product_id}",version="1.0.0",contract_version="2"}} 1',
            "",
            "# HELP http_requests_total Total number of HTTP requests processed",
            "# TYPE http_requests_total counter",
        ]
        if not counts:
            lines.append(f'http_requests_total{{product_id="{product_id}",method="GET",handler="/metrics",status="200"}} 1')
        else:
            for (m, p, s), count in sorted(counts.items()):
                lines.append(f'http_requests_total{{product_id="{product_id}",method="{m}",handler="{p}",status="{s}"}} {count}')

        lines.extend([
            "",
            "# HELP http_request_duration_seconds HTTP request latency histogram summary",
            "# TYPE http_request_duration_seconds histogram",
        ])
        sum_dur = sum(durations)
        count_dur = len(durations)
        buckets = [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
        for b in buckets:
            b_count = sum(1 for d in durations if d <= b)
            lines.append(f'http_request_duration_seconds_bucket{{product_id="{product_id}",le="{b}"}} {b_count}')
        lines.append(f'http_request_duration_seconds_bucket{{product_id="{product_id}",le="+Inf"}} {count_dur}')
        lines.append(f'http_request_duration_seconds_sum{{product_id="{product_id}"}} {sum_dur:.6f}')
        lines.append(f'http_request_duration_seconds_count{{product_id="{product_id}"}} {count_dur}')

        lines.extend([
            "",
            "# HELP domain_entities_total Total count of persisted domain records",
            "# TYPE domain_entities_total gauge",
            f'domain_entities_total{{product_id="{product_id}"}} {db_entity_count}',
            "",
            "# HELP process_uptime_seconds Elapsed seconds since service startup",
            "# TYPE process_uptime_seconds gauge",
            f'process_uptime_seconds{{product_id="{product_id}"}} {int(time.time() - start_t)}',
            "",
        ])
        return "\n".join(lines) + "\n"


GLOBAL_METRICS = MetricsCollector()


class DatabaseManager:
    """Manages SQLite schema creation, migrations, and CRUD operations."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> Any:
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url.startswith(("postgres://", "postgresql://")):
            try:
                import psycopg2
                return psycopg2.connect(db_url)
            except ImportError:
                pass
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA journal_mode = WAL")
            sync_mode = os.environ.get("SQLITE_SYNCHRONOUS", "NORMAL").upper()
            if sync_mode in ("0", "OFF"):
                conn.execute("PRAGMA synchronous = OFF")
            elif sync_mode in ("2", "FULL"):
                conn.execute("PRAGMA synchronous = FULL")
            elif sync_mode in ("3", "EXTRA"):
                conn.execute("PRAGMA synchronous = EXTRA")
            else:
                conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            return conn
        except Exception:
            conn.close()
            raise

    def check_health(self) -> Tuple[bool, str]:
        try:
            conn = self.get_connection()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
            return True, "healthy"
        except Exception as err:
            return False, f"unhealthy: {err}"

    def apply_migrations(self, migrations_dir: Optional[Path] = None) -> int:
        mdir = migrations_dir or MIGRATIONS_DIR
        applied = 0
        conn = self.get_connection()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS _migrations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL
                )"""
            )
            if not mdir.is_dir():
                return applied

            files = sorted(mdir.glob("*.sql"))
            for sql_file in files:
                existing = conn.execute(
                    "SELECT 1 FROM _migrations WHERE name = ?", (sql_file.name,)
                ).fetchone()
                if not existing:
                    sql_content = sql_file.read_text(encoding="utf-8")
                    conn.executescript(sql_content)
                    conn.execute(
                        "INSERT INTO _migrations (name, applied_at) VALUES (?, ?)",
                        (sql_file.name, datetime.now(timezone.utc).isoformat()),
                    )
                    applied += 1
            conn.commit()
        finally:
            conn.close()
        return applied

    def seed_data(self, seed_file: Optional[Path] = None) -> int:
        sfile = seed_file or (SEED_DIR / "seed.json")
        if not sfile.is_file():
            return 0
        try:
            data = json.loads(sfile.read_text(encoding="utf-8"))
        except Exception:
            return 0

        inserted = 0
        items = data if isinstance(data, list) else data.get("items", [])
        conn = self.get_connection()
        try:
            for item in items:
                name = item.get("name") or item.get("title") or "Seed Item"
                category = item.get("category") or item.get("type") or "default"
                status = item.get("status") or "active"
                payload = json.dumps(item)
                now = datetime.now(timezone.utc).isoformat()
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO items (name, category, status, payload, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (name, category, status, payload, now, now),
                )
                if cursor.rowcount > 0:
                    inserted += 1
            conn.commit()
        finally:
            conn.close()
        return inserted

    def count_entities(self) -> int:
        try:
            conn = self.get_connection()
            try:
                row = conn.execute("SELECT count(*) as cnt FROM items").fetchone()
                return int(row["cnt"]) if row else 0
            finally:
                conn.close()
        except Exception:
            return 0

    def list_items(self, limit: int = 100, desc: bool = False) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            order_by = "ORDER BY id DESC" if desc else "ORDER BY id ASC"
            rows = conn.execute(
                f"SELECT id, name, category, status, payload, created_at, updated_at FROM items {order_by} LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for r in rows:
                try:
                    payload = json.loads(r["payload"]) if r["payload"] else {}
                except Exception:
                    payload = {}
                result.append({
                    "id": r["id"],
                    "name": r["name"],
                    "category": r["category"],
                    "status": r["status"],
                    "payload": payload,
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                })
            return result
        finally:
            conn.close()

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        try:
            r = conn.execute(
                "SELECT id, name, category, status, payload, created_at, updated_at FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if not r:
                return None
            try:
                payload = json.loads(r["payload"]) if r["payload"] else {}
            except Exception:
                payload = {}
            return {
                "id": r["id"],
                "name": r["name"],
                "category": r["category"],
                "status": r["status"],
                "payload": payload,
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
        finally:
            conn.close()

    def create_item(self, data: Dict[str, Any]) -> Dict[str, Any]:
        name = data.get("name", "Untitled")
        category = data.get("category", "general")
        status = data.get("status", "active")
        payload = json.dumps(data.get("payload", {}))
        now = datetime.now(timezone.utc).isoformat()
        conn = self.get_connection()
        try:
            cursor = conn.execute(
                """INSERT INTO items (name, category, status, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, category, status, payload, now, now),
            )
            item_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()
        return {
            "id": item_id,
            "name": name,
            "category": category,
            "status": status,
            "payload": data.get("payload", {}),
            "created_at": now,
            "updated_at": now,
        }

    def delete_item(self, item_id: int) -> bool:
        conn = self.get_connection()
        try:
            cursor = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def checkpoint(self, mode: str = "PASSIVE") -> Dict[str, Any]:
        """Checkpoint the WAL journal into the main database.

        mode: PASSIVE, FULL, RESTART, or TRUNCATE
        Returns: {mode: str, busy: int, log_frames: int, checkpointed_frames: int}
        """
        valid_modes = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
        mode_upper = mode.upper()
        if mode_upper not in valid_modes:
            raise ValueError(f"Invalid checkpoint mode: {mode}. Must be one of {valid_modes}")
        conn = self.get_connection()
        try:
            res = conn.execute(f"PRAGMA wal_checkpoint({mode_upper})").fetchone()
            return {
                "mode": mode_upper,
                "busy": res[0] if res else 0,
                "log_frames": res[1] if res else 0,
                "checkpointed_frames": res[2] if res else 0,
            }
        finally:
            conn.close()

    def backup(self, target_path: Path | str, pages: int = -1) -> Path:
        """Create a consistent online backup using SQLite Online Backup API.

        Safely captures WAL and database pages without blocking concurrent readers.
        """
        target = Path(target_path).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.checkpoint("PASSIVE")
        except Exception:
            pass

        src_conn = self.get_connection()
        dest_conn = sqlite3.connect(str(target), timeout=30.0)
        try:
            with dest_conn:
                src_conn.backup(dest_conn, pages=pages)
            dest_conn.execute("PRAGMA foreign_keys = ON")
            dest_conn.execute("PRAGMA journal_mode = WAL")
            ic = dest_conn.execute("PRAGMA integrity_check").fetchone()
            if not ic or ic[0] != "ok":
                raise RuntimeError(f"Backup verification failed integrity check: {ic}")
        finally:
            dest_conn.close()
            src_conn.close()
        return target

    def restore(self, backup_path: Path | str) -> bool:
        """Restore database from backup file with disaster recovery resilience.

        Handles online restoration as well as physical replacement in case of corruption.
        """
        backup_file = Path(backup_path).resolve()
        if not backup_file.is_file():
            raise FileNotFoundError(f"Backup file not found: {backup_file}")

        test_conn = sqlite3.connect(str(backup_file), timeout=10.0)
        try:
            ic = test_conn.execute("PRAGMA integrity_check").fetchone()
            if not ic or ic[0] != "ok":
                raise RuntimeError(f"Source backup failed integrity check: {ic}")
        finally:
            test_conn.close()

        restored_online = False
        dest_conn = None
        src_conn = None
        try:
            dest_conn = self.get_connection()
            src_conn = sqlite3.connect(str(backup_file), timeout=30.0)
            with dest_conn:
                src_conn.backup(dest_conn)
            dest_conn.execute("PRAGMA journal_mode = WAL")
            dest_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            restored_online = True
        except (sqlite3.DatabaseError, Exception):
            restored_online = False
        finally:
            if dest_conn:
                try:
                    dest_conn.close()
                except Exception:
                    pass
            if src_conn:
                try:
                    src_conn.close()
                except Exception:
                    pass

        if not restored_online:
            import gc
            import shutil
            import time
            gc.collect()
            time.sleep(0.05)
            wal_file = self.db_path.with_name(self.db_path.name + "-wal")
            shm_file = self.db_path.with_name(self.db_path.name + "-shm")
            if self.db_path.exists():
                corrupt_quarantine = self.db_path.with_name(f"{self.db_path.name}.corrupt.{int(time.time())}")
                replaced = False
                for _ in range(5):
                    try:
                        self.db_path.rename(corrupt_quarantine)
                        replaced = True
                        break
                    except Exception:
                        gc.collect()
                        time.sleep(0.05)
                if not replaced:
                    for _ in range(5):
                        try:
                            self.db_path.unlink(missing_ok=True)
                            break
                        except Exception:
                            gc.collect()
                            time.sleep(0.05)

            if wal_file.exists():
                wal_file.unlink(missing_ok=True)
            if shm_file.exists():
                shm_file.unlink(missing_ok=True)

            shutil.copy2(backup_file, self.db_path)
            conn = self.get_connection()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()

        return True

    def verify_integrity(self) -> Dict[str, Any]:
        """Run comprehensive SQLite health, journal mode, pragmas, and integrity checks."""
        conn = self.get_connection()
        try:
            jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
            sync = conn.execute("PRAGMA synchronous").fetchone()[0]
            bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            ic_rows = conn.execute("PRAGMA integrity_check").fetchall()
            integrity_ok = len(ic_rows) == 1 and ic_rows[0][0] == "ok"
            qc_rows = conn.execute("PRAGMA quick_check").fetchall()
            quick_ok = len(qc_rows) == 1 and qc_rows[0][0] == "ok"
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            fk_ok = len(fk_rows) == 0

            has_items = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
            ).fetchone() is not None
            count = 0
            if has_items:
                count_row = conn.execute("SELECT count(*) FROM items").fetchone()
                count = count_row[0] if count_row else 0

            return {
                "journal_mode": jm,
                "synchronous": sync,
                "busy_timeout": bt,
                "foreign_keys": fk,
                "integrity_ok": integrity_ok,
                "quick_ok": quick_ok,
                "foreign_key_ok": fk_ok,
                "row_count": count,
                "healthy": integrity_ok and quick_ok and fk_ok and (jm.lower() == "wal"),
            }
        finally:
            conn.close()


class ServiceRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler implementing MasterSpec Section 41.2 endpoints and domain APIs."""

    db: DatabaseManager = DatabaseManager()

    def send_json(self, status: int, data: Any) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        cid = self.headers.get("X-Correlation-ID") or self.headers.get("x-correlation-id")
        if cid:
            self.send_header("X-Correlation-ID", cid)
        tp = self.headers.get("traceparent") or self.headers.get("Traceparent")
        if tp:
            self.send_header("traceparent", tp)
        self.end_headers()
        self.wfile.write(payload)

    def send_plain_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        cid = self.headers.get("X-Correlation-ID") or self.headers.get("x-correlation-id")
        if cid:
            self.send_header("X-Correlation-ID", cid)
        tp = self.headers.get("traceparent") or self.headers.get("Traceparent")
        if tp:
            self.send_header("traceparent", tp)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        start_time = time.time()
        parsed = urlparse(self.path)
        path = parsed.path
        status_code = HTTPStatus.OK

        try:
            # Section 41.2 Endpoint: /health
            if path == "/health":
                db_healthy, db_detail = self.db.check_health()
                overall = "healthy" if db_healthy else "unhealthy"
                status_code = HTTPStatus.OK if db_healthy else HTTPStatus.SERVICE_UNAVAILABLE
                response = {
                    "status": overall,
                    "application": "healthy",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "dependencies": {
                        "database": db_detail,
                    },
                }
                self.send_json(status_code, response)

            # Section 41.2 Endpoint: /version
            elif path == "/version":
                status_code = HTTPStatus.OK
                response = {
                    "product_id": get_product_id(),
                    "version": "1.0.0",
                    "contract_version": 2,
                    "commit_sha": get_git_sha(),
                    "digest": get_artifact_digest(),
                    "deployed_at": "2026-09-22T00:00:00Z",
                }
                self.send_json(status_code, response)

            # Section 41.2 Endpoint: /metrics
            elif path == "/metrics":
                status_code = HTTPStatus.OK
                product_id = get_product_id()
                entity_count = self.db.count_entities()
                text = GLOBAL_METRICS.render_prometheus(product_id, entity_count)
                self.send_plain_text(
                    status_code, text, content_type="text/plain; version=0.0.4; charset=utf-8"
                )

            # Domain Collection Endpoint: /api/v1/items
            elif path == "/api/v1/items":
                status_code = HTTPStatus.OK
                q_params = parse_qs(parsed.query)
                limit_val = int(q_params.get("limit", [100])[0]) if "limit" in q_params else 100
                desc_val = q_params.get("desc", ["false"])[0].lower() in ("true", "1")
                items = self.db.list_items(limit=limit_val, desc=desc_val)
                self.send_json(status_code, {"items": items, "count": len(items)})

            # Domain Single Entity Endpoint: /api/v1/items/<id>
            elif re.match(r"^/api/v1/items/(\d+)$", path):
                match = re.match(r"^/api/v1/items/(\d+)$", path)
                assert match is not None
                item_id = int(match.group(1))
                item = self.db.get_item(item_id)
                if item:
                    status_code = HTTPStatus.OK
                    self.send_json(status_code, item)
                else:
                    status_code = HTTPStatus.NOT_FOUND
                    self.send_json(status_code, {"error": "Item not found", "id": item_id})

            # Root info endpoint
            elif path == "/":
                status_code = HTTPStatus.OK
                self.send_json(status_code, {
                    "service": get_product_id(),
                    "endpoints": ["/health", "/version", "/metrics", "/api/v1/items"],
                })

            else:
                status_code = HTTPStatus.NOT_FOUND
                self.send_json(status_code, {"error": "Not Found", "path": path})

        except Exception as exc:
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR
            self.send_json(status_code, {"error": str(exc)})
        finally:
            GLOBAL_METRICS.record_request(
                "GET", path, int(status_code), time.time() - start_time
            )

    def do_POST(self) -> None:
        start_time = time.time()
        parsed = urlparse(self.path)
        path = parsed.path
        status_code = HTTPStatus.CREATED

        try:
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                status_code = HTTPStatus.BAD_REQUEST
                self.send_json(status_code, {"error": "Invalid JSON body"})
                return

            cid = self.headers.get("X-Correlation-ID") or self.headers.get("x-correlation-id")
            tp = self.headers.get("traceparent") or self.headers.get("Traceparent")

            if path in ("/api/v1/items", "/api/v1/events", "/api/v1/webhooks"):
                created = self.db.create_item(data)
                if data.get("cascade"):
                    prod_id = get_product_id()
                    try:
                        import sys
                        from pathlib import Path
                        mesh_path = Path(__file__).resolve().parent.parent
                        if str(mesh_path) not in sys.path:
                            sys.path.insert(0, str(mesh_path))

                        if prod_id in ("Product-1", "iam"):
                            from event_mesh import dispatch_user_created_workflow
                            user_email = data.get("email")
                            if not user_email and isinstance(data.get("payload"), dict):
                                user_email = data.get("payload", {}).get("email")
                            if not user_email:
                                user_email = f"{data.get('name', 'user')}@enterprise.internal"

                            cascade_res = dispatch_user_created_workflow(
                                user_id=created.get("id"),
                                username=data.get("name", f"user-{created.get('id')}"),
                                email=user_email,
                                role=data.get("role", "customer"),
                                correlation_id=cid,
                                traceparent=tp,
                            )
                            created["cascade"] = cascade_res
                        elif prod_id in ("Product-3", "orders"):
                            from event_mesh import dispatch_order_placed_workflow
                            cascade_res = dispatch_order_placed_workflow(
                                order_id=created.get("id"),
                                user_id=data.get("customer_id", 1),
                                total_amount=float(data.get("amount", 99.99)),
                                correlation_id=cid,
                                traceparent=tp,
                            )
                            created["cascade"] = cascade_res
                    except Exception as mesh_err:
                        created["cascade"] = {"error": str(mesh_err)}

                status_code = HTTPStatus.CREATED
                self.send_json(status_code, created)
            else:
                status_code = HTTPStatus.NOT_FOUND
                self.send_json(status_code, {"error": "Not Found", "path": path})

        except Exception as exc:
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR
            self.send_json(status_code, {"error": str(exc)})
        finally:
            GLOBAL_METRICS.record_request(
                "POST", path, int(status_code), time.time() - start_time
            )

    def do_DELETE(self) -> None:
        start_time = time.time()
        parsed = urlparse(self.path)
        path = parsed.path
        status_code = HTTPStatus.OK

        try:
            match = re.match(r"^/api/v1/items/(\d+)$", path)
            if match:
                item_id = int(match.group(1))
                deleted = self.db.delete_item(item_id)
                if deleted:
                    status_code = HTTPStatus.OK
                    self.send_json(status_code, {"deleted": True, "id": item_id})
                else:
                    status_code = HTTPStatus.NOT_FOUND
                    self.send_json(status_code, {"error": "Item not found", "id": item_id})
            else:
                status_code = HTTPStatus.NOT_FOUND
                self.send_json(status_code, {"error": "Not Found", "path": path})
        except Exception as exc:
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR
            self.send_json(status_code, {"error": str(exc)})
        finally:
            GLOBAL_METRICS.record_request(
                "DELETE", path, int(status_code), time.time() - start_time
            )

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard HTTP access logs in test runs
        if os.environ.get("LOG_LEVEL") == "debug":
            super().log_message(format, *args)


def run_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    db = DatabaseManager()
    db.apply_migrations()
    server = ThreadingHTTPServer((host, port), ServiceRequestHandler)
    print(f"[{get_product_id()}] Running Enterprise Service on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="MultiProduct Enterprise Service Engine")
    parser.add_argument("--serve", action="store_true", help="Start the HTTP server")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="Host to bind")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")), help="Port to bind")
    parser.add_argument("--migrate", action="store_true", help="Apply SQL database migrations")
    parser.add_argument("--seed", action="store_true", help="Seed database with initial records")
    parser.add_argument("--health", action="store_true", help="Perform offline database health check")
    parser.add_argument("--backup", metavar="TARGET_PATH", help="Create an online backup of the SQLite database")
    parser.add_argument("--restore", metavar="SOURCE_PATH", help="Restore the SQLite database from a backup file")
    parser.add_argument("--checkpoint", choices=["PASSIVE", "FULL", "RESTART", "TRUNCATE"], nargs="?", const="PASSIVE", help="Execute a WAL checkpoint")
    parser.add_argument("--verify-db", action="store_true", help="Run comprehensive database integrity and PRAGMA checks")

    args = parser.parse_args()
    db = DatabaseManager()

    if args.migrate:
        count = db.apply_migrations()
        print(f"Applied {count} database migration(s).")

    if args.seed:
        count = db.seed_data()
        print(f"Seeded {count} domain record(s).")

    if args.health:
        healthy, detail = db.check_health()
        print(f"Database health: {detail}")
        sys.exit(0 if healthy else 1)

    if args.backup:
        out_path = db.backup(args.backup)
        print(f"Database successfully backed up to {out_path}")
        sys.exit(0)

    if args.restore:
        db.restore(args.restore)
        print(f"Database successfully restored from {args.restore}")
        sys.exit(0)

    if args.checkpoint:
        res = db.checkpoint(args.checkpoint)
        print(f"WAL Checkpoint ({res['mode']}): busy={res['busy']}, log_frames={res['log_frames']}, checkpointed={res['checkpointed_frames']}")
        sys.exit(0)

    if args.verify_db:
        res = db.verify_integrity()
        print(json.dumps(res, indent=2))
        sys.exit(0 if res.get("healthy") else 1)

    has_standalone_cmd = (
        args.migrate
        or args.seed
        or args.health
        or args.backup
        or args.restore
        or args.checkpoint
        or args.verify_db
    )

    if args.serve or not has_standalone_cmd:
        run_server(args.host, args.port)


if __name__ == "__main__":
    main()
