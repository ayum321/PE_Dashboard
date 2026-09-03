#!/bin/bash
# ============================================================
# PE Audit Dashboard Agent API — Startup Script
# ============================================================

set -e

export PORT=${PORT:-8765}

echo "=============================================="
echo " PE Audit Dashboard Agent API"
echo " Starting on port ${PORT}"
echo "=============================================="

# Ensure writable state directory
export PE_STATE_DIR=${PE_STATE_DIR:-/tmp/pe_dashboard_state}
mkdir -p "${PE_STATE_DIR}" 2>/dev/null || true
echo "PE_STATE_DIR: ${PE_STATE_DIR}"

# -------------------------------------------------------
# Launch uvicorn
# -------------------------------------------------------
echo "Starting uvicorn..."
exec uvicorn main:app \
    --app-dir /app/app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 1 \
    --log-level info