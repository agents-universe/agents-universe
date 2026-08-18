#!/usr/bin/env bash
set -euo pipefail

# Apply DB migrations before serving traffic (matches the previous api CMD).
echo "[entrypoint] Running database migrations..."
alembic upgrade head

# Serve the web UI with nginx (non-root; runtime dirs under /tmp/nginx).
# Strategy: try to remove stale dirs from a previous run (may fail on /tmp
# sticky bit if owned by a different UID), then recreate. The Dockerfile
# pre-creates these dirs as 0777 so even if rm fails and mkdir is a no-op,
# the baked-in permissions allow any UID to write.
echo "[entrypoint] Starting nginx..."
rm -rf /tmp/nginx 2>/dev/null || true
mkdir -p /tmp/nginx/body /tmp/nginx/proxy /tmp/nginx/fastcgi \
         /tmp/nginx/uwsgi /tmp/nginx/scgi
chmod -R 0777 /tmp/nginx 2>/dev/null || true
nginx
for _ in $(seq 1 30); do
    [ -f /tmp/nginx/nginx.pid ] && break
    sleep 1
done
if [ ! -f /tmp/nginx/nginx.pid ]; then
    echo "[entrypoint] ERROR: nginx failed to start" >&2
    exit 1
fi

# exec so uvicorn receives container signals (PID 1). Bound to loopback only:
# nginx on port 8000 is the public entry point and proxies to this port.
echo "[entrypoint] Starting API server (uvicorn)..."
exec uvicorn api.main:app --host 127.0.0.1 --port 8001
