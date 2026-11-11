#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
uv run --no-project --with pytest --with cryptography python -m pytest -q -p no:cacheprovider tests
