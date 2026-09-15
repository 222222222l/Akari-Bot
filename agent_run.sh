#!/bin/bash
# One prompt/session/budget; the Python bridge keeps streaming and isolated logs.
set -eu
AKARI_BRIDGE_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec python3 -u "$AKARI_BRIDGE_DIR/akari_agent.py" "$@"
