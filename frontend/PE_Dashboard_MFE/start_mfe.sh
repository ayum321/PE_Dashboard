#!/bin/sh
set -e

echo "=============================================="
echo " PE Audit Dashboard - React MFE"
echo " Starting on port 8080 as UID $(id -u)"
echo "=============================================="

# Generate runtime env.js dynamically in writable /tmp
API_URL="${API_BASE_URL:-${apiBaseUrl:-}}"
APP_NAME="${LOCAL_APP_NAME:-${appName:-PE Audit Dashboard}}"
PATH_PREFIX="${FRAME_URL_PATH:-${frameUrlPath:-/}}"

# Safely write to /tmp without crashing on read-only environments
cat <<EOF > /tmp/env.js 2>/dev/null || true
window["env"] = {
  LOCAL_APP_NAME: "${APP_NAME}",
  FRAME_URL_PATH: "${PATH_PREFIX}",
  API_BASE_URL: "${API_URL}"
};
EOF

if [ -f /tmp/env.js ]; then
  echo "Runtime env.js generated at /tmp/env.js with API_BASE_URL: '${API_URL}'"
else
  echo "Warning: /tmp/env.js could not be written; falling back to bundled env.js"
fi

exec nginx -g "daemon off;"