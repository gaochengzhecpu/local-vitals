"""Minimal OMRON BP7255 BLE reader.

Only a volatile transfer request is written. This module does not set the
monitor clock, acknowledge/clear transferred readings, reset the monitor, or
write arbitrary device memory.
"""

from __future__ import annotations

import asyncio
import struct
from datetime import datetime
from typing import Any, Callable

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # Demo mode and parser tests do not require Bluetooth support.
    BleakClient = None
    BleakScanner = None


OMRON_SERVICE = "0000fe4a-0000-1000-8000-00805f9b34fb"
BP_MEASUREMENT = "00002a35-0000-1000-8000-00805f9b34fb"
OMRON_COMMAND = "db5b55e0-aee7-11e1-965e-0002a5d5c51b"
OMRON_DATA = "b305b680-aee7-11e1-a730-0002a5d5c51b"
OMRON_STATUS = "49123040-aee8-11e1-a74d-0002a5d5c51b"
TRANSFER_SILENCE = 3.0


def _require_bleak() -> None:
    if BleakClient is None or BleakScanner is None:
        raise RuntimeError(
            "Bluetooth support is not installed. Launch start.command once to install it."
        )


def _is_omron(device, advertisement) -> bool:
    name = (device.name or "").upper()
    services = [item.lower() for item in advertisement.service_uuids]
    return name.startswith(("BLESMART", "OMRON", "BP", "HEM-")) or OMRON_SERVICE in services


def _decode_sfloat(raw: int) -> float:
    exponent = (raw >> 12) & 0xF
    if exponent >= 8:
        exponent -= 16
    mantissa = raw & 0x0FFF
    if mantissa >= 0x0800:
        mantissa -= 0x1000
    return mantissa * (10**exponent)


def parse_measurement(data: bytes) -> dict[str, Any] | None:
    """Parse Bluetooth SIG Blood Pressure Measurement characteristic 0x2A35."""
    if len(data) < 7:
        return None

    flags = data[0]
    offset = 7
    result: dict[str, Any] = {
        "systolic": round(_decode_sfloat(struct.unpack_from("<H", data, 1)[0])),
        "diastolic": round(_decode_sfloat(struct.unpack_from("<H", data, 3)[0])),
        "mean_arterial_pressure": round(
            _decode_sfloat(struct.unpack_from("<H", data, 5)[0])
        ),
        "unit": "kPa" if flags & 0x01 else "mmHg",
        "timestamp": None,
        "pulse": None,
        "user_slot": None,
        "measurement_status": None,
    }

    if flags & 0x02 and len(data) >= offset + 7:
        year = struct.unpack_from("<H", data, offset)[0]
        month, day, hour, minute, second = data[offset + 2 : offset + 7]
        try:
            result["timestamp"] = datetime(
                year, month, day, hour, minute, second
            ).isoformat()
        except ValueError:
            pass
        offset += 7

    if flags & 0x04 and len(data) >= offset + 2:
        result["pulse"] = round(
            _decode_sfloat(struct.unpack_from("<H", data, offset)[0])
        )
        offset += 2

    if flags & 0x08 and len(data) >= offset + 1:
        result["user_slot"] = data[offset]
        offset += 1

    if flags & 0x10 and len(data) >= offset + 2:
        result["measurement_status"] = struct.unpack_from("<H", data, offset)[0]

    return result


async def scan_omron(timeout: float = 8.0) -> list[dict[str, Any]]:
    _require_bleak()
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matches: list[dict[str, Any]] = []
    for device, advertisement in found.values():
        name = device.name or ""
        if _is_omron(device, advertisement):
            matches.append(
                {
                    "name": name or "OMRON monitor",
                    "address": device.address,
                    "rssi": advertisement.rssi,
                    "service_uuids": advertisement.service_uuids,
                }
            )
    return sorted(matches, key=lambda item: item["rssi"] or -999, reverse=True)


async def _wait_for_device(address: str | None, max_wait: float):
    """Continuously scan until the paired monitor begins advertising."""
    _require_bleak()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        window = min(6.0, max(1.0, deadline - loop.time()))
        found = await BleakScanner.discover(timeout=window, return_adv=True)
        candidates = []
        for device, advertisement in found.values():
            if address and device.address == address:
                return device
            if _is_omron(device, advertisement):
                candidates.append((device, advertisement.rssi or -999))
        if candidates:
            candidates.sort(key=lambda item: item[1], reverse=True)
            return candidates[0][0]
        await asyncio.sleep(0.25)
    raise RuntimeError(
        "No BP7255 appeared before the listening window ended. Keep phone Bluetooth "
        "off, click Start automatic sync, and then take a new measurement."
    )


async def sync_once(
    address: str | None = None,
    timeout: float = 25.0,
    discovery_timeout: float = 150.0,
    commit_records: Callable[[list[dict[str, Any]]], int] | None = None,
) -> dict[str, Any]:
    _require_bleak()
    device = await _wait_for_device(address, discovery_timeout)

    records: list[dict[str, Any]] = []
    first_record = asyncio.Event()
    last_record_at = [0.0]

    def on_measurement(_sender, value: bytearray) -> None:
        raw = bytes(value)
        record = parse_measurement(raw)
        if record:
            record["raw_hex"] = raw.hex()
            record["source_model"] = "BP7255 / HEM-716CT2-Z"
            record["device_address"] = device.address
            records.append(record)
            last_record_at[0] = asyncio.get_running_loop().time()
            first_record.set()

    client = BleakClient(device, timeout=20.0)
    try:
        await client.connect()
        if not client.is_connected:
            raise RuntimeError("The BP7255 disconnected during connection")

        await client.start_notify(BP_MEASUREMENT, on_measurement)
        for characteristic in (OMRON_DATA, OMRON_STATUS):
            try:
                await client.start_notify(characteristic, lambda _sender, _value: None)
            except Exception:
                pass

        await asyncio.sleep(0.8)
        # Volatile 'send pending measurements' request. Intentionally no ACK.
        await client.write_gatt_char(OMRON_COMMAND, bytes(20), response=True)

        try:
            await asyncio.wait_for(first_record.wait(), timeout=timeout)
            while True:
                await asyncio.sleep(0.25)
                if asyncio.get_running_loop().time() - last_record_at[0] >= TRANSFER_SILENCE:
                    break
        except TimeoutError as exc:
            raise RuntimeError(
                "Connected, but no measurement arrived. Take a new reading, then "
                "press Connection/Memory until the Bluetooth icon flashes and retry."
            ) from exc

        inserted = None
        if commit_records is not None:
            # Persist every delivered record and its audit trail. BP7255's
            # transfer-state command is unverified, so no acknowledgement or
            # device-memory write is sent.
            inserted = commit_records(records)
    finally:
        if client.is_connected:
            await client.disconnect()

    return {
        "device": {"name": device.name or "BP7255", "address": device.address},
        "records": records,
        "inserted": inserted,
        "acknowledged": False,
    }
