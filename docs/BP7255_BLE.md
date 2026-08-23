# OMRON BP7255 BLE notes

This document describes the narrow behavior used by LocalVitals. It is not an official OMRON protocol specification.

## UUIDs

| Purpose | UUID |
|---|---|
| OMRON advertisement/service marker | `0000fe4a-0000-1000-8000-00805f9b34fb` |
| Bluetooth SIG Blood Pressure Measurement | `00002a35-0000-1000-8000-00805f9b34fb` |
| Observed OMRON command | `db5b55e0-aee7-11e1-965e-0002a5d5c51b` |
| Observed OMRON data | `b305b680-aee7-11e1-a730-0002a5d5c51b` |
| Observed OMRON status | `49123040-aee8-11e1-a74d-0002a5d5c51b` |

## Transfer sequence

```text
BP7255 begins advertising
        │
        ▼
scan_omron / _wait_for_device
        │
        ▼
BleakClient.connect
        │
        ├── subscribe 0x2A35 measurement notifications
        ├── subscribe observed data/status notifications (best effort)
        └── write 20 zero bytes to observed command characteristic
                         │
                         ▼
                one or more 0x2A35 notifications
                         │
                         ▼
              parse → audit → deduplicate → SQLite
```

`sync_once()` waits for the first measurement and then treats three seconds without another notification as the end of the delivered batch.

## Parsing `0x2A35`

The first byte is a flags field. The next three 16-bit little-endian IEEE-11073 `SFLOAT` values are systolic, diastolic, and mean arterial pressure. Depending on the flags, the payload may then contain:

- a seven-byte timestamp;
- a pulse `SFLOAT`;
- a user slot byte;
- a two-byte measurement-status field.

Every accepted notification is stored with its raw hex bytes in the sync audit before any duplicate is discarded from the measurements view.

## Safety boundary

The current code sends only the observed volatile transfer request. It intentionally does not:

- acknowledge or mark records transferred;
- write a clock value;
- change user settings;
- reset the monitor;
- clear device memory.

The correct proprietary acknowledgement has not been verified from a public manufacturer specification. Avoid adding writes by trial and error to a health device.

## Troubleshooting

- Keep other paired phones from connecting while the desktop scans.
- Start listening before or while taking a new measurement.
- Grant the terminal process Bluetooth access on macOS.
- BLE device addresses on macOS may be CoreBluetooth identifiers rather than hardware MAC addresses.
- Inspect the local raw-transfer audit when a model or firmware sends an unexpected payload.
