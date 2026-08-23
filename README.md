# LocalVitals

LocalVitals is a local-first health timeline prototype. Its working core reads blood-pressure measurements directly from an OMRON BP7255 over Bluetooth Low Energy, without OMRON Connect or a cloud account. The UI also contains privacy-conscious prototypes for a health-agent chat, meal photo logging, and Apple Health context.

> Personal recordkeeping prototype only. It does not diagnose conditions, provide medical advice, or provide emergency monitoring.

## What is included

- Direct BP7255 BLE discovery, connection, notification parsing, and local persistence.
- SQLite storage with measurement deduplication and a raw BLE transfer audit.
- Localhost-only Python HTTP server and dependency-free browser UI.
- JSON/CSV export and import endpoints.
- An isolated synthetic demo with no real measurements.
- Placeholder interfaces for a future local LLM, meal-image analysis, and Apple Health import.

No health database, Apple Health export, device address, meal photo, API key, or user-specific value is included in this repository.

## Project structure

```text
local-vitals/
├── app.py                 # localhost HTTP server and JSON API
├── ble_bp7255.py          # BLE discovery, command, notifications, parser
├── database.py            # SQLite schema, deduplication, audit, demo seed
├── static/
│   ├── index.html         # dashboard structure
│   ├── app.js             # UI and API client
│   └── styles.css         # responsive local UI
├── tests/test_core.py     # parser, database, audit, and demo tests
├── docs/BP7255_BLE.md     # protocol notes and safety boundaries
├── start.command          # real-device launcher on port 8765
├── start-demo.command     # synthetic demo launcher on port 8766
└── requirements.txt       # bleak
```

## Run the synthetic demo

The demo requires only Python 3 and never connects to Bluetooth:

```bash
python3 app.py --demo
```

Open `http://127.0.0.1:8766`. The demo database is created under `demo-data/`, which is ignored by Git. Press **Syn BP data** to insert a labeled `119/80 mmHg · pulse 83` sample.

## Run with an OMRON BP7255

On macOS, double-click `start.command`, or run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Then:

1. Turn off Bluetooth on a phone that may automatically connect to the monitor.
2. Allow Terminal to use Bluetooth if macOS asks.
3. Open `http://127.0.0.1:8765`.
4. Press **Sync BP7255**.
5. Take a measurement normally while LocalVitals listens for the monitor.

Measurements are stored in `data/local-vitals.db`. This directory is ignored by Git.

## How the BP7255 connection works

The implementation in `ble_bp7255.py` follows this sequence:

1. Repeatedly scan for an OMRON advertisement or the OMRON service UUID.
2. Connect with `BleakClient` once the monitor starts advertising.
3. Subscribe to the standard Blood Pressure Measurement characteristic (`0x2A35`).
4. Subscribe best-effort to the two observed proprietary OMRON data/status characteristics.
5. Write a 20-byte zero-valued volatile transfer request to the observed OMRON command characteristic.
6. Parse each `0x2A35` notification using the Bluetooth SIG flag layout and IEEE-11073 `SFLOAT` values.
7. Wait until notifications have been quiet for three seconds, then persist the whole delivered batch.
8. Deduplicate measurements by timestamp, BP, pulse, source model, and device address.

LocalVitals deliberately does **not** send an unverified transfer acknowledgement, set the monitor clock, reset the monitor, or delete device memory. More detail is in [docs/BP7255_BLE.md](docs/BP7255_BLE.md).

## Privacy model

- The server binds only to `127.0.0.1` or `localhost`.
- Static assets are bundled; there are no remote scripts or telemetry.
- Browser write requests require a local marker and same-origin check.
- Real and demo databases live in separate ignored directories.
- Meal photos currently remain in browser memory and are not uploaded or saved.
- The LLM UI is an interface placeholder and makes no model request.

## Tests

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
```

## Known limitations

- The BLE behavior was tested with BP7255 / HEM-716CT2-Z and may differ on other models or firmware.
- A phone can prevent discovery by connecting to the monitor first.
- The proprietary OMRON command is based on observed behavior, not an official public protocol specification.
- Apple Health ingestion and model-backed analysis are UI prototypes, not implemented integrations.

## License

MIT. Bluetooth communication uses [`bleak`](https://github.com/hbldh/bleak), also under the MIT License.
