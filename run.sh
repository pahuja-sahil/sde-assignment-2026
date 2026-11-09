#!/usr/bin/env bash
# Controller entry point. Contract: ./run.sh <SIM_URL> runs until the scenario finishes.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
exec python -m controller.main --url "$SIM_URL" --db "${STATE_DB:-controller/state.db}"
