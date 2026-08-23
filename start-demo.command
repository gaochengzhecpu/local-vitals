#!/bin/bash
set -e
cd "$(dirname "$0")"
exec python3 app.py --demo
