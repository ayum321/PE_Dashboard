#!/bin/sh
set -e

echo "=============================================="
echo " PE Audit Dashboard - React MFE"
echo " Starting on port 8080 as UID $(id -u)"
echo "=============================================="

# Generate runtime env.js dynamically
API_URL="${API_BASE_URL:-${apiBaseUrl:-}}"
APP_NAME="${LOCAL_APP_NAME:-${appName:-PE Audit Dashboard}}"
PATH_PREFIX="${FRAME_URL_PATH:-${frameUrlPath:-/}}"

cat <<EOF > /usr/share/nginx/html/env.js
window["env"] = {
  LOCAL_APP_NAME: "${APP_NAME}",
  FRAME_URL_PATH: "${PATH_PREFIX}",
  API_BASE_URL: "${API_URL}"
};
EOF

echo "Runtime env.js generated with API_BASE_URL: '${API_URL}'"

exec nginx -g "daemon off;"