import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ble_bp7255 import parse_measurement
from database import Store, seed_demo


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
        demo_store.connection.close()

    def test_adds_local_meal_note(self):
        meal = self.store.add_meal({"description": "Rice and vegetables"})
        self.assertEqual(meal["description"], "Rice and vegetables")
        self.assertEqual(meal["data_class"], "real")
        self.assertEqual(len(self.store.meals()), 1)


if __name__ == "__main__":
    unittest.main()
