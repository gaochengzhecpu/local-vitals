import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ble_bp7255 import parse_measurement
from database import Store, seed_demo
from app import LocalVitalsHandler


class ParserTests(unittest.TestCase):
    def test_parses_sig_measurement(self):
        payload = bytes.fromhex("06780050006400ea0708170c22384800")
        record = parse_measurement(payload)
        self.assertEqual(record["systolic"], 120)
        self.assertEqual(record["diastolic"], 80)
        self.assertEqual(record["pulse"], 72)
        self.assertEqual(record["timestamp"], "2026-08-23T12:34:56")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "test.db")
        self.record = {
            "timestamp": "2026-08-23T12:34:56",
            "systolic": 120,
            "diastolic": 80,
            "mean_arterial_pressure": 94,
            "pulse": 72,
            "unit": "mmHg",
            "user_slot": 1,
            "measurement_status": 0,
            "source_model": "BP7255 / HEM-716CT2-Z",
            "device_address": "test-device",
        }

    def tearDown(self):
        self.store.connection.close()
        self.tempdir.cleanup()

    def test_deduplicates(self):
        self.assertTrue(self.store.add(self.record))
        self.assertFalse(self.store.add(self.record))
        self.assertEqual(len(self.store.all()), 1)

    def test_raw_sync_audit(self):
        delivered = {**self.record, "raw_hex": "06" + "00" * 18}
        inserted = self.store.add(delivered)
        batch_id = self.store.log_sync_batch([delivered], [inserted])
        self.store.finish_sync_batch(batch_id, acknowledged=True)
        batches = self.store.sync_batches()
        self.assertEqual(batches[0]["received_count"], 1)
        self.assertTrue(batches[0]["acknowledged"])
        self.assertTrue(batches[0]["records"][0]["was_inserted"])
        self.assertEqual(batches[0]["records"][0]["raw_hex"], "06" + "00" * 18)

    def test_json_roundtrip_and_delete(self):
        self.store.add(self.record)
        exported = self.store.export_json()
        self.assertEqual(json.loads(exported)["measurements"][0]["systolic"], 120)
        self.assertEqual(self.store.delete_all(), 1)
        result = self.store.import_text(exported, "json")
        self.assertEqual(result["inserted"], 1)

    def test_health_sample_deduplicates(self):
        sample = {
            "metric": "sleep_duration",
            "start_time": "2026-08-23T07:00:00",
            "value": 7.2,
            "unit": "hr",
            "source": "Synthetic Apple Watch context",
            "data_class": "synthetic",
        }
        self.assertTrue(self.store.add_health_sample(sample))
        self.assertFalse(self.store.add_health_sample(sample))
        self.assertEqual(len(self.store.health_samples()), 1)

    def test_demo_is_separate_and_labeled(self):
        self.store.add(self.record)
        demo_store = Store(Path(self.tempdir.name) / "demo.db")
        manifest = seed_demo(demo_store, self.store.all())

        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(len(self.store.health_samples()), 0)
        self.assertEqual(manifest["real_bp_copies"], 0)
        self.assertEqual(manifest["synthetic_bp"], 28)
        self.assertEqual(manifest["health_samples"], 140)
        self.assertEqual(manifest["synthetic_meals"], 3)
        self.assertTrue(all(sample["data_class"] == "synthetic" for sample in demo_store.health_samples()))
        self.assertEqual(len(demo_store.daily_context()), 14)
        sources = {record["source_model"] for record in demo_store.all()}
        self.assertNotIn("Copied real BP · demo only", sources)
        self.assertIn("Synthetic BP context · demo only", sources)
        newest_background = max(
            datetime.fromisoformat(record["timestamp"])
            for record in demo_store.all()
        )
        self.assertLess(newest_background.date(), datetime.now().date())
        button_record = {
            **self.record,
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "systolic": 119,
            "diastolic": 80,
            "pulse": 83,
            "source_model": "Synthetic button · demo only",
            "device_address": None,
        }
        self.assertTrue(demo_store.add(button_record))
        self.assertEqual(
            demo_store.all()[0]["source_model"], "Synthetic button · demo only"
        )
        demo_store.connection.close()

    def test_adds_local_meal_note(self):
        meal = self.store.add_meal({"description": "Rice and vegetables"})
        self.assertEqual(meal["description"], "Rice and vegetables")
        self.assertEqual(meal["data_class"], "real")
        self.assertEqual(len(self.store.meals()), 1)

    def test_serializes_concurrent_writes(self):
        def add(index):
            return self.store.add_meal({"description": f"Meal {index}"})

        with ThreadPoolExecutor(max_workers=8) as pool:
            meals = list(pool.map(add, range(40)))
        self.assertEqual(len(meals), 40)
        self.assertEqual(len(self.store.meals()), 40)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tempdir.name) / "api.db")
        manifest = seed_demo(self.store)

        class TestHandler(LocalVitalsHandler):
            def log_message(self, _format, *args):
                pass

        TestHandler.store = self.store
        TestHandler.data_dir = Path(self.tempdir.name)
        TestHandler.demo_mode = True
        TestHandler.demo_manifest = manifest
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.connection.close()
        self.tempdir.cleanup()

    def request(self, path, method="GET", payload=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "X-Local-Vitals": "1",
                **({"Content-Type": "application/json"} if body else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def test_demo_api_end_to_end(self):
        status_code, status = self.request("/api/status")
        self.assertEqual(status_code, 200)
        self.assertTrue(status["demo_mode"])

        status_code, result = self.request(
            "/api/demo/synthetic-reading", method="POST", payload={}
        )
        self.assertEqual(status_code, 200)
        self.assertEqual(
            (
                result["record"]["systolic"],
                result["record"]["diastolic"],
                result["record"]["pulse"],
            ),
            (119, 80, 83),
        )
        _, records = self.request("/api/records")
        self.assertEqual(records["records"][0]["source_model"], "Synthetic button · demo only")

        status_code, meal = self.request(
            "/api/meals", method="POST", payload={"description": "Test meal"}
        )
        self.assertEqual(status_code, 201)
        self.assertEqual(meal["meal"]["description"], "Test meal")


if __name__ == "__main__":
    unittest.main()
