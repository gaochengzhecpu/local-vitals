from __future__ import annotations

import csv
import hashlib
import io
import json
import random
import sqlite3
import threading
import uuid
from datetime import datetime, time, timedelta
from functools import wraps
from pathlib import Path
from typing import Any


EXPORT_FIELDS = [
    "timestamp",
    "systolic",
    "diastolic",
    "mean_arterial_pressure",
    "pulse",
    "unit",
    "user_slot",
    "measurement_status",
    "source_model",
    "device_address",
    "imported_at",
]


def synchronized(method):
    """Serialize access to the shared SQLite connection, including nested calls."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


class Store:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # The localhost UI serves requests concurrently. Python's SQLite build
        # is serialized, so allow those request threads to share this connection.
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS measurements (
                id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                timestamp TEXT,
                systolic INTEGER NOT NULL,
                diastolic INTEGER NOT NULL,
                mean_arterial_pressure INTEGER,
                pulse INTEGER,
                unit TEXT NOT NULL DEFAULT 'mmHg',
                user_slot INTEGER,
                measurement_status INTEGER,
                source_model TEXT NOT NULL,
                device_address TEXT,
                imported_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_batches (
                id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                device_address TEXT,
                received_count INTEGER NOT NULL,
                inserted_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_deliveries (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                raw_hex TEXT NOT NULL,
                timestamp TEXT,
                systolic INTEGER NOT NULL,
                diastolic INTEGER NOT NULL,
                mean_arterial_pressure INTEGER,
                pulse INTEGER,
                user_slot INTEGER,
                measurement_status INTEGER,
                was_inserted INTEGER NOT NULL,
                FOREIGN KEY (batch_id) REFERENCES sync_batches(id) ON DELETE CASCADE
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS health_samples (
                id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL UNIQUE,
                metric TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                source TEXT NOT NULL,
                data_class TEXT NOT NULL CHECK(data_class IN ('real', 'synthetic')),
                note TEXT
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS meal_entries (
                id TEXT PRIMARY KEY,
                eaten_at TEXT NOT NULL,
                description TEXT NOT NULL,
                photo_name TEXT,
                energy_low REAL,
                energy_high REAL,
                protein_g REAL,
                carbohydrates_g REAL,
                fat_g REAL,
                source TEXT NOT NULL,
                data_class TEXT NOT NULL CHECK(data_class IN ('real', 'synthetic'))
            )
            """
        )
        self.connection.commit()

    @staticmethod
    def _fingerprint(record: dict[str, Any]) -> str:
        key = "|".join(
            str(record.get(field) or "")
            for field in (
                "timestamp",
                "systolic",
                "diastolic",
                "pulse",
                "source_model",
                "device_address",
            )
        )
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def _validated(record: dict[str, Any]) -> dict[str, Any]:
        clean = dict(record)
        for field in ("systolic", "diastolic"):
            clean[field] = int(clean[field])
        for field in ("mean_arterial_pressure", "pulse", "user_slot", "measurement_status"):
            value = clean.get(field)
            clean[field] = int(value) if value not in (None, "") else None

        if not 20 <= clean["systolic"] <= 350:
            raise ValueError("Systolic value is outside the supported import range")
        if not 20 <= clean["diastolic"] <= 250:
            raise ValueError("Diastolic value is outside the supported import range")
        if clean["pulse"] is not None and not 20 <= clean["pulse"] <= 300:
            raise ValueError("Pulse value is outside the supported import range")

        clean["unit"] = clean.get("unit") or "mmHg"
        clean["source_model"] = clean.get("source_model") or "Imported record"
        clean["device_address"] = clean.get("device_address") or None
        clean["timestamp"] = clean.get("timestamp") or None
        clean["imported_at"] = clean.get("imported_at") or datetime.now().isoformat()
        return clean

    @synchronized
    def add(self, record: dict[str, Any]) -> bool:
        clean = self._validated(record)
        fingerprint = self._fingerprint(clean)
        try:
            self.connection.execute(
                """
                INSERT INTO measurements (
                    id, fingerprint, timestamp, systolic, diastolic,
                    mean_arterial_pressure, pulse, unit, user_slot,
                    measurement_status, source_model, device_address, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    fingerprint,
                    clean["timestamp"],
                    clean["systolic"],
                    clean["diastolic"],
                    clean["mean_arterial_pressure"],
                    clean["pulse"],
                    clean["unit"],
                    clean["user_slot"],
                    clean["measurement_status"],
                    clean["source_model"],
                    clean["device_address"],
                    clean["imported_at"],
                ),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    @synchronized
    def all(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM measurements ORDER BY COALESCE(timestamp, imported_at) DESC"
        ).fetchall()
        return [
            {key: row[key] for key in row.keys() if key != "fingerprint"}
            for row in rows
        ]

    @synchronized
    def log_sync_batch(
        self, records: list[dict[str, Any]], insert_results: list[bool]
    ) -> str:
        if len(records) != len(insert_results):
            raise ValueError("Every delivered record needs an insertion result")
        batch_id = str(uuid.uuid4())
        inserted_count = sum(insert_results)
        device_address = records[0].get("device_address") if records else None
        self.connection.execute(
            """
            INSERT INTO sync_batches (
                id, received_at, device_address, received_count,
                inserted_count, duplicate_count, acknowledged, error
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                batch_id,
                datetime.now().isoformat(),
                device_address,
                len(records),
                inserted_count,
                len(records) - inserted_count,
            ),
        )
        for index, (record, was_inserted) in enumerate(
            zip(records, insert_results), start=1
        ):
            clean = self._validated(record)
            self.connection.execute(
                """
                INSERT INTO sync_deliveries (
                    id, batch_id, sequence_index, raw_hex, timestamp,
                    systolic, diastolic, mean_arterial_pressure, pulse,
                    user_slot, measurement_status, was_inserted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    batch_id,
                    index,
                    record.get("raw_hex") or "",
                    clean["timestamp"],
                    clean["systolic"],
                    clean["diastolic"],
                    clean["mean_arterial_pressure"],
                    clean["pulse"],
                    clean["user_slot"],
                    clean["measurement_status"],
                    int(was_inserted),
                ),
            )
        self.connection.commit()
        return batch_id

    @synchronized
    def finish_sync_batch(
        self, batch_id: str, acknowledged: bool, error: str | None = None
    ) -> None:
        self.connection.execute(
            "UPDATE sync_batches SET acknowledged = ?, error = ? WHERE id = ?",
            (int(acknowledged), error, batch_id),
        )
        self.connection.commit()

    @synchronized
    def sync_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM sync_batches ORDER BY received_at DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        batches = []
        for row in rows:
            batch = {key: row[key] for key in row.keys()}
            deliveries = self.connection.execute(
                """
                SELECT sequence_index, raw_hex, timestamp, systolic, diastolic,
                       mean_arterial_pressure, pulse, user_slot,
                       measurement_status, was_inserted
                FROM sync_deliveries
                WHERE batch_id = ?
                ORDER BY sequence_index
                """,
                (row["id"],),
            ).fetchall()
            batch["acknowledged"] = bool(batch["acknowledged"])
            batch["records"] = [
                {
                    **{key: delivery[key] for key in delivery.keys()},
                    "was_inserted": bool(delivery["was_inserted"]),
                }
                for delivery in deliveries
            ]
            batches.append(batch)
        return batches

    @synchronized
    def add_health_sample(self, sample: dict[str, Any]) -> bool:
        metric = str(sample["metric"])
        start_time = str(sample["start_time"])
        value = float(sample["value"])
        unit = str(sample["unit"])
        source = str(sample.get("source") or "Imported health data")
        data_class = str(sample.get("data_class") or "real")
        if data_class not in {"real", "synthetic"}:
            raise ValueError("Health sample data_class must be real or synthetic")
        fingerprint = hashlib.sha256(
            "|".join((metric, start_time, str(value), unit, source, data_class)).encode()
        ).hexdigest()
        try:
            self.connection.execute(
                """
                INSERT INTO health_samples (
                    id, fingerprint, metric, start_time, end_time, value,
                    unit, source, data_class, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    fingerprint,
                    metric,
                    start_time,
                    sample.get("end_time"),
                    value,
                    unit,
                    source,
                    data_class,
                    sample.get("note"),
                ),
            )
            self.connection.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    @synchronized
    def health_samples(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT id, metric, start_time, end_time, value, unit,
                   source, data_class, note
            FROM health_samples
            ORDER BY start_time DESC, metric
            """
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    @synchronized
    def add_meal(self, meal: dict[str, Any]) -> dict[str, Any]:
        description = str(meal.get("description") or "").strip()
        if not description:
            raise ValueError("Add a short description of the meal")
        if len(description) > 500:
            raise ValueError("Meal description must be 500 characters or fewer")
        entry = {
            "id": str(uuid.uuid4()),
            "eaten_at": str(meal.get("eaten_at") or datetime.now().isoformat()),
            "description": description,
            "photo_name": str(meal.get("photo_name") or "")[:180] or None,
            "energy_low": meal.get("energy_low"),
            "energy_high": meal.get("energy_high"),
            "protein_g": meal.get("protein_g"),
            "carbohydrates_g": meal.get("carbohydrates_g"),
            "fat_g": meal.get("fat_g"),
            "source": str(meal.get("source") or "Manual local meal note"),
            "data_class": str(meal.get("data_class") or "real"),
        }
        if entry["data_class"] not in {"real", "synthetic"}:
            raise ValueError("Meal data_class must be real or synthetic")
        self.connection.execute(
            """
            INSERT INTO meal_entries (
                id, eaten_at, description, photo_name, energy_low, energy_high,
                protein_g, carbohydrates_g, fat_g, source, data_class
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(entry[key] for key in (
                "id", "eaten_at", "description", "photo_name", "energy_low",
                "energy_high", "protein_g", "carbohydrates_g", "fat_g",
                "source", "data_class"
            )),
        )
        self.connection.commit()
        return entry

    @synchronized
    def meals(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM meal_entries ORDER BY eaten_at DESC"
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    @synchronized
    def daily_context(self) -> list[dict[str, Any]]:
        days: dict[str, dict[str, Any]] = {}

        def day_bucket(day: str) -> dict[str, Any]:
            return days.setdefault(
                day,
                {"date": day, "blood_pressure": [], "health": {}, "classes": set()},
            )

        for record in self.all():
            timestamp = record.get("timestamp")
            if not timestamp:
                continue
            day = timestamp[:10]
            bucket = day_bucket(day)
            bucket["blood_pressure"].append(
                {
                    "systolic": record["systolic"],
                    "diastolic": record["diastolic"],
                    "pulse": record.get("pulse"),
                    "timestamp": timestamp,
                    "source": record["source_model"],
                    "data_class": (
                        "synthetic"
                        if record["source_model"].startswith("Synthetic")
                        else "real-copy"
                        if record["source_model"].startswith("Copied")
                        else "real"
                    ),
                }
            )
            bucket["classes"].add(bucket["blood_pressure"][-1]["data_class"])

        for sample in self.health_samples():
            day = sample["start_time"][:10]
            bucket = day_bucket(day)
            bucket["health"][sample["metric"]] = {
                "value": sample["value"],
                "unit": sample["unit"],
                "source": sample["source"],
                "data_class": sample["data_class"],
                "note": sample["note"],
            }
            bucket["classes"].add(sample["data_class"])

        result = []
        for day in sorted(days, reverse=True):
            bucket = days[day]
            readings = bucket["blood_pressure"]
            if readings:
                bucket["bp_summary"] = {
                    "count": len(readings),
                    "systolic": round(sum(r["systolic"] for r in readings) / len(readings)),
                    "diastolic": round(sum(r["diastolic"] for r in readings) / len(readings)),
                    "pulse": round(
                        sum(r["pulse"] for r in readings if r["pulse"] is not None)
                        / max(1, sum(r["pulse"] is not None for r in readings))
                    ),
                }
            else:
                bucket["bp_summary"] = None
            bucket["classes"] = sorted(bucket["classes"])
            result.append(bucket)
        return result

    @synchronized
    def delete_all(self) -> int:
        count = self.connection.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        self.connection.execute("DELETE FROM sync_deliveries")
        self.connection.execute("DELETE FROM sync_batches")
        self.connection.execute("DELETE FROM measurements")
        self.connection.execute("DELETE FROM health_samples")
        self.connection.execute("DELETE FROM meal_entries")
        self.connection.commit()
        return count

    @synchronized
    def export_json(self) -> str:
        records = [
            {field: record.get(field) for field in EXPORT_FIELDS}
            for record in self.all()
        ]
        return json.dumps(
            {
                "version": 1,
                "measurements": records,
                "sync_batches": self.sync_batches(limit=500),
                "meals": self.meals(),
            },
            indent=2,
        )

    @synchronized
    def export_csv(self) -> str:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for record in self.all():
            writer.writerow({field: record.get(field) for field in EXPORT_FIELDS})
        return output.getvalue()

    @synchronized
    def import_text(self, content: str, format_name: str) -> dict[str, int]:
        if format_name == "json":
            payload = json.loads(content)
            records = payload.get("measurements", payload) if isinstance(payload, dict) else payload
        elif format_name == "csv":
            records = list(csv.DictReader(io.StringIO(content)))
        else:
            raise ValueError("Only JSON and CSV imports are supported")

        if not isinstance(records, list):
            raise ValueError("Import must contain a list of measurements")
        inserted = sum(1 for record in records if self.add(record))
        return {"received": len(records), "inserted": inserted, "duplicates": len(records) - inserted}


def _seed_demo_meals(store: Store, anchor_day) -> int:
    if store.meals():
        return 0
    meals = (
        (8, "Oatmeal with banana and walnuts", 380, 470, 13, 62, 16),
        (13, "Chicken rice bowl with vegetables", 620, 780, 42, 84, 22),
        (19, "Salmon, roasted vegetables, and potatoes", 560, 720, 39, 48, 27),
    )
    for hour, description, low, high, protein, carbs, fat in meals:
        store.add_meal(
            {
                "eaten_at": datetime.combine(anchor_day, time(hour, 0)).isoformat(),
                "description": description,
                "energy_low": low,
                "energy_high": high,
                "protein_g": protein,
                "carbohydrates_g": carbs,
                "fat_g": fat,
                "source": "Synthetic meal · demo only",
                "data_class": "synthetic",
            }
        )
    return len(meals)


def seed_demo(
    store: Store, _real_records: list[dict[str, Any]] | None = None
) -> dict[str, int]:
    """Create a deterministic, clearly labeled demo without touching the real store."""
    if store.health_samples():
        # Older demo versions copied real BP values. Remove only those demo
        # copies and same-day background samples so the button reading is latest.
        today = datetime.now().date().isoformat()
        with store._lock:
            removed = store.connection.execute(
                "DELETE FROM measurements WHERE source_model LIKE 'Copied real BP%'"
            ).rowcount
            removed_same_day = store.connection.execute(
                """
                DELETE FROM measurements
                WHERE source_model = 'Synthetic BP context · demo only'
                  AND substr(timestamp, 1, 10) >= ?
                """,
                (today,),
            ).rowcount
            store.connection.commit()
        _seed_demo_meals(store, datetime.now().date())
        records = store.all()
        return {
            "bp_records": len(records),
            "health_samples": len(store.health_samples()),
            "real_bp_copies": 0,
            "synthetic_bp": sum(
                record["source_model"].startswith("Synthetic") for record in records
            ),
            "removed_real_bp_copies": removed,
            "removed_same_day_background": removed_same_day,
            "synthetic_meals": sum(
                meal["data_class"] == "synthetic" for meal in store.meals()
            ),
        }

    rng = random.Random(7255)
    # End the generated timeline yesterday. A user-triggered sample created now
    # is therefore always the newest BP record returned by the API.
    anchor_day = (datetime.now() - timedelta(days=1)).date()
    _seed_demo_meals(store, datetime.now().date())

    synthetic_bp = 0
    health_count = 0
    for offset in range(13, -1, -1):
        day = anchor_day - timedelta(days=offset)
        sleep = round(rng.uniform(5.4, 8.2), 1)
        steps = int(rng.uniform(2800, 11800))
        exercise = int(max(0, rng.gauss(26, 18)))
        hrv = round(max(18, min(68, 31 + (sleep - 6.2) * 7 + rng.gauss(0, 4))), 1)
        resting_hr = round(max(52, min(88, 76 - (sleep - 5.5) * 3 + rng.gauss(0, 2))))
        respiratory = round(max(12.5, min(18.5, 15.1 + rng.gauss(0, 0.7))), 1)
        oxygen = round(max(94, min(100, 97.3 + rng.gauss(0, 0.8))))
        wrist_temp = round(rng.gauss(0.0, 0.22), 2)
        deep_sleep = round(max(0.5, sleep * rng.uniform(0.12, 0.21)), 1)
        rem_sleep = round(max(0.6, sleep * rng.uniform(0.18, 0.27)), 1)

        sample_values = {
            "sleep_duration": (sleep, "hr"),
            "deep_sleep": (deep_sleep, "hr"),
            "rem_sleep": (rem_sleep, "hr"),
            "hrv_sdnn": (hrv, "ms"),
            "resting_heart_rate": (resting_hr, "beats/min"),
            "step_count": (steps, "count"),
            "exercise_minutes": (exercise, "min"),
            "respiratory_rate": (respiratory, "breaths/min"),
            "oxygen_saturation": (oxygen, "%"),
            "wrist_temperature_delta": (wrist_temp, "°C from baseline"),
        }
        for metric, (value, unit) in sample_values.items():
            if store.add_health_sample(
                {
                    "metric": metric,
                    "start_time": datetime.combine(day, time(7, 0)).isoformat(),
                    "value": value,
                    "unit": unit,
                    "source": "Synthetic Apple Watch context",
                    "data_class": "synthetic",
                    "note": "Demo value; not measured from a person.",
                }
            ):
                health_count += 1

        sleep_load = max(0, 7.4 - sleep) * 4.2
        inactivity_load = max(0, 6500 - steps) / 1800
        for hour, shift in ((8, 0), (20, 2)):
            systolic = round(121 + sleep_load + inactivity_load + shift + rng.gauss(0, 2.4))
            diastolic = round(76 + sleep_load * 0.45 + shift * 0.4 + rng.gauss(0, 1.8))
            pulse = round(resting_hr + (4 if hour == 20 else 1) + rng.gauss(0, 2))
            if store.add(
                {
                    "timestamp": datetime.combine(day, time(hour, 10)).isoformat(),
                    "systolic": systolic,
                    "diastolic": diastolic,
                    "mean_arterial_pressure": round((systolic + 2 * diastolic) / 3),
                    "pulse": pulse,
                    "unit": "mmHg",
                    "user_slot": 1,
                    "measurement_status": None,
                    "source_model": "Synthetic BP context · demo only",
                    "device_address": None,
                }
            ):
                synthetic_bp += 1

    return {
        "bp_records": len(store.all()),
        "health_samples": health_count,
        "real_bp_copies": 0,
        "synthetic_bp": synthetic_bp,
        "synthetic_meals": len(store.meals()),
    }
