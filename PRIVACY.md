# Privacy notes for contributors

Do not commit real health data or identifiers.

The repository ignore rules exclude SQLite databases, Apple Health exports, GPX routes, environment files, and local virtual environments. Before publishing a change, also check staged files for:

- measurement values and timestamps;
- Bluetooth addresses or CoreBluetooth identifiers;
- Apple Health XML, CDA, route, or ZIP exports;
- meal photos;
- local filesystem paths and account names;
- API keys or model credentials.

Use only clearly labeled synthetic records in tests, screenshots, issues, and pull requests.
