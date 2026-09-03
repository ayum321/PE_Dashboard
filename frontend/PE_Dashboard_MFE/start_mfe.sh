#!/bin/sh
set -e

echo "=============================================="
echo " PE Audit Dashboard - React MFE"
echo " Starting on port 8080 as UID $(id -u)"
echo "=============================================="

# Determine backend API URL from environment variables
RAW_BACKEND="${API_BASE_URL:-${API_URL:-${apiBaseUrl:-https://pedashboard-api-asre-plan-ai-agents.us.live.internal.byp.ai}}}"
# Strip any trailing slashes
BACKEND_URL=$(echo "${RAW_BACKEND}" | sed -e 's|/*$||')
# Extract Hostname (strip protocol and any path)
BACKEND_HOST=$(echo "${BACKEND_URL}" | sed -e 's|^https*://||' -e 's|/.*$||')

echo "Configuring Nginx reverse proxy to API: ${BACKEND_URL} (Host: ${BACKEND_HOST})"

# Copy nginx.conf to writable /tmp
cp /etc/nginx/nginx.conf /tmp/nginx.conf

# Substitute __BACKEND_URL__ and __BACKEND_HOST__ in /tmp/nginx.conf
sed -i \
  -e "s|__BACKEND_URL__|${BACKEND_URL}|g" \
  -e "s|__BACKEND_HOST__|${BACKEND_HOST}|g" \
  /tmp/nginx.conf

APP_NAME="${LOCAL_APP_NAME:-${appName:-PE Audit Dashboard}}"
PATH_PREFIX="${FRAME_URL_PATH:-${frameUrlPath:-/}}"

# Generate runtime env.js dynamically in writable /tmp
# Set API_BASE_URL to empty string "" so the browser uses same-origin relative URLs (/api/...)
# which are cleanly routed through Nginx's reverse proxy without any CORS, cookie, or preflight issues!
cat <<EOF > /tmp/env.js 2>/dev/null || true
window["env"] = {
  LOCAL_APP_NAME: "${APP_NAME}",
  FRAME_URL_PATH: "${PATH_PREFIX}",
  API_BASE_URL: ""
};
EOF

if [ -f /tmp/env.js ]; then
  echo "Runtime env.js generated at /tmp/env.js (API_BASE_URL is relative for same-origin proxy)"
else
  echo "Warning: /tmp/env.js could not be written; falling back to bundled env.js"
fi

exec nginx -c /tmp/nginx.conf -g "daemon off;"