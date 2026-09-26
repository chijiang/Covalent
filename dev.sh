#!/usr/bin/env bash
# Run Covalent Enterprise from any working directory.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
set -a
if [ -f .env ]; then source .env; fi
set +a
BACKEND_PORT=${AGENT_FRAMEWORK_BACKEND_PORT:-5170}
BACKEND_HOST=${AGENT_FRAMEWORK_BACKEND_HOST:-0.0.0.0}
FRONTEND_PORT=${AGENT_FRAMEWORK_FRONTEND_PORT:-3100}
run_backend() { exec uv run --package covalent-enterprise covalent-enterprise serve --host "$BACKEND_HOST" --port "$BACKEND_PORT"; }
run_frontend() { pnpm --filter @covalent/enterprise-web dev --port "$FRONTEND_PORT"; }
case "${1:-both}" in
  backend) run_backend ;;
  frontend) run_frontend ;;
  both)
    run_backend &
    BACKEND_PID=$!
    trap 'kill "$BACKEND_PID" 2>/dev/null || true' EXIT
    sleep 2
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      wait "$BACKEND_PID"
      exit 1
    fi
    run_frontend
    ;;
  *) echo "Usage: ./dev.sh [backend|frontend|both]" >&2; exit 1 ;;
esac
