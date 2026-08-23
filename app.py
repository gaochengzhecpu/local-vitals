#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import sys
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ble_bp7255 import scan_omron, sync_once
from database import Store, seed_demo


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DEFAULT_DATA_DIR = ROOT / "data"
MAX_BODY = 5 * 1024 * 1024


class LocalVitalsHandler(BaseHTTPRequestHandler):
    store: Store
    data_dir: Path
    demo_mode = False
    demo_manifest: dict = {}

    def log_message(self, format_string: str, *args) -> None:
        # Deliberately avoid printing URLs or health data to shared terminal logs.
        if args and str(args[1]).startswith(("4", "5")):
            print(f"HTTP error: {args[1]}", file=sys.stderr)

    def _json(self, payload, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = 400):
        self._json({"ok": False, "error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY:
            raise ValueError("Request is larger than 5 MB")
        raw = self.rfile.read(length)
        return json.loads(raw or b"{}")

    def _local_write_allowed(self) -> bool:
        marker = self.headers.get("X-Local-Vitals") == "1"
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "127.0.0.1:8765")
        origin_ok = not origin or origin in {
            f"http://{host}",
            f"http://127.0.0.1:{host.rsplit(':', 1)[-1]}",
            f"http://localhost:{host.rsplit(':', 1)[-1]}",
        }
        return marker and origin_ok

    def _serve_static(self, path: str):
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC / relative).resolve()
        if STATIC not in target.parents or not target.is_file():
            self._error("Not found", 404)
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._json(
                {
                    "ok": True,
                    "record_count": len(self.store.all()),
                    "data_path": str(self.store.path),
                    "network": "127.0.0.1 only",
                    "demo_mode": self.demo_mode,
                    "demo_manifest": self.demo_manifest,
                }
            )
        elif parsed.path == "/api/records":
            self._json({"ok": True, "records": self.store.all()})
        elif parsed.path == "/api/meals":
            self._json({"ok": True, "meals": self.store.meals()})
        elif parsed.path == "/api/context":
            self._json(
                {
                    "ok": True,
                    "days": self.store.daily_context(),
                    "demo_mode": self.demo_mode,
                    "legend": {
                        "real-copy": "Copied from your local BP database into the isolated demo",
                        "synthetic": "Generated for demonstration; not measured from a person",
                    },
                }
            )
        elif parsed.path == "/api/transfers":
            limit = parse_qs(parsed.query).get("limit", ["50"])[0]
            self._json(
                {"ok": True, "batches": self.store.sync_batches(limit=int(limit))}
            )
        elif parsed.path == "/api/device/scan":
            if self.demo_mode:
                self._error("Bluetooth is disabled in the isolated demo", 409)
                return
            try:
                devices = asyncio.run(scan_omron(timeout=8.0))
                self._json({"ok": True, "devices": devices})
            except Exception as exc:
                self._error(str(exc), 500)
        elif parsed.path == "/api/export":
            format_name = parse_qs(parsed.query).get("format", ["json"])[0]
            if format_name == "csv":
                body = self.store.export_csv().encode()
                content_type = "text/csv; charset=utf-8"
                filename = "local-vitals.csv"
            else:
                body = self.store.export_json().encode()
                content_type = "application/json; charset=utf-8"
                filename = "local-vitals.json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path.startswith("/api/"):
            self._error("Not found", 404)
        else:
            self._serve_static(parsed.path)

    def do_POST(self):
        if not self._local_write_allowed():
            self._error("Local request marker is missing", 403)
            return
        try:
            payload = self._body()
            if self.path == "/api/device/sync":
                if self.demo_mode:
                    self._error("Bluetooth is disabled in the isolated demo", 409)
                    return
                deadline = time.monotonic() + 150.0
                address = payload.get("address")
                received = duplicates = attempts = 0
                last_error = None
                while time.monotonic() < deadline:
                    remaining = deadline - time.monotonic()
                    batch_id = None
                    try:
                        def commit_batch(records):
                            nonlocal batch_id
                            insert_results = [self.store.add(record) for record in records]
                            batch_id = self.store.log_sync_batch(records, insert_results)
                            return sum(insert_results)

                        result = asyncio.run(
                            sync_once(
                                address,
                                timeout=min(25.0, remaining),
                                discovery_timeout=min(20.0, remaining),
                                commit_records=commit_batch,
                            )
                        )
                        attempts += 1
                        address = result["device"]["address"]
                        received += len(result["records"])
                        inserted = result["inserted"]
                        duplicates += len(result["records"]) - inserted
                        if batch_id:
                            self.store.finish_sync_batch(
                                batch_id, acknowledged=result["acknowledged"]
                            )
                        if inserted:
                            break
                    except Exception as exc:
                        last_error = str(exc)
                        if batch_id:
                            self.store.finish_sync_batch(
                                batch_id, acknowledged=False, error=last_error
                            )
                    time.sleep(min(3.0, max(0.0, deadline - time.monotonic())))
                else:
                    detail = f" Last device message: {last_error}" if last_error else ""
                    raise RuntimeError(
                        "No new measurement arrived within 150 seconds. Existing readings "
                        f"were ignored as duplicates.{detail}"
                    )
                self._json(
                    {
                        "ok": True,
                        "device": result["device"],
                        "received": received,
                        "inserted": inserted,
                        "duplicates": duplicates,
                        "attempts": attempts,
                        "acknowledged": result["acknowledged"],
                        "batch_id": batch_id,
                    }
                )
            elif self.path == "/api/demo/synthetic-reading":
                if not self.demo_mode:
                    self._error("Synthetic readings are available in demo mode only", 409)
                    return
                record = {
                    "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                    "systolic": 119,
                    "diastolic": 80,
                    "mean_arterial_pressure": 93,
                    "pulse": 83,
                    "unit": "mmHg",
                    "user_slot": 1,
                    "measurement_status": None,
                    "source_model": "Synthetic button · demo only",
                    "device_address": None,
                }
                inserted = self.store.add(record)
                if inserted:
                    self.demo_manifest["synthetic_bp"] = self.demo_manifest.get("synthetic_bp", 0) + 1
                    self.demo_manifest["bp_records"] = self.demo_manifest.get("bp_records", 0) + 1
                self._json({"ok": True, "inserted": inserted, "record": record})
            elif self.path == "/api/meals":
                meal = self.store.add_meal(payload)
                self._json({"ok": True, "meal": meal}, 201)
            elif self.path == "/api/import":
                result = self.store.import_text(payload["content"], payload["format"])
                self._json({"ok": True, **result})
            else:
                self._error("Not found", 404)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._error(str(exc), 400)
        except Exception as exc:
            self._error(str(exc), 500)

    def do_DELETE(self):
        if not self._local_write_allowed():
            self._error("Local request marker is missing", 403)
            return
        if self.path == "/api/records":
            deleted = self.store.delete_all()
            self._json({"ok": True, "deleted": deleted, "device_records_untouched": True})
        else:
            self._error("Not found", 404)


def main() -> int:
    parser = argparse.ArgumentParser(description="LocalVitals BP7255 app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("For privacy, LocalVitals only binds to localhost")

    if args.port is None:
        args.port = 8766 if args.demo else 8765
    if args.data_dir is None:
        configured = os.getenv("LOCAL_VITALS_DATA_DIR")
        args.data_dir = Path(configured) if configured else ROOT / ("demo-data" if args.demo else "data")

    args.data_dir.mkdir(parents=True, exist_ok=True)
    LocalVitalsHandler.store = Store(args.data_dir / "local-vitals.db")
    LocalVitalsHandler.data_dir = args.data_dir
    LocalVitalsHandler.demo_mode = args.demo
    if args.demo:
        LocalVitalsHandler.demo_manifest = seed_demo(LocalVitalsHandler.store)
    else:
        LocalVitalsHandler.demo_manifest = {}
    # A browser can keep one localhost connection open. Serving each request in
    # its own thread prevents that connection from blocking the whole app.
    server = ThreadingHTTPServer((args.host, args.port), LocalVitalsHandler)
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}"
    print(f"LocalVitals is running at {url}")
    print(f"Data stays at {LocalVitalsHandler.store.path}")
    if args.demo:
        print("Demo mode: Bluetooth disabled; synthetic values are labeled in the UI")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLocalVitals stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
