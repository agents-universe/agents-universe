#!/usr/bin/env bash
set -euo pipefail

# Scrub proxy vars that are SET but empty. The compose `env_file: .env`
# neutralizes Docker Desktop's host-proxy injection with empty values; Python,
# curl and git skip empty proxies, but semgrep's OCaml core parses HTTPS_PROXY
# strictly and fatals on an empty URI - unsetting the empty vars is the same
# "no proxy" semantics for everyone.
for _v in ALL_PROXY HTTP_PROXY HTTPS_PROXY all_proxy http_proxy https_proxy; do
    if [ -z "${!_v:-}" ]; then
        unset "$_v"
    fi
done
unset _v

# Apply DB migrations before serving traffic (matches the previous api CMD).
echo "[entrypoint] Running database migrations..."
alembic upgrade head

# Serve the web UI with nginx (non-root; runtime dirs under /tmp/nginx).
# Strategy: try to remove stale dirs from a previous run (may fail on /tmp
# sticky bit if owned by a different UID), then recreate. The Dockerfile
# pre-creates these dirs as 0777 so even if rm fails and mkdir is a no-op,
# the baked-in permissions allow any UID to write.
echo "[entrypoint] Preparing nginx runtime dirs..."
rm -rf /tmp/nginx 2>/dev/null || true
mkdir -p /tmp/nginx/body /tmp/nginx/proxy /tmp/nginx/fastcgi \
         /tmp/nginx/uwsgi /tmp/nginx/scgi
chmod -R 0777 /tmp/nginx 2>/dev/null || true

# This script is PID 1. Forward TERM/INT to the children so `docker stop`
# shuts them down cleanly instead of SIGKILLing them.
_term() {
    echo "[entrypoint] received TERM/INT, shutting down children"
    kill -TERM "$UV_PID" "$NGINX_PID" 2>/dev/null || true
}
trap _term TERM INT

# Start nginx with `daemon off` so the master stays a direct child of this
# script: that is what makes liveness detection and zombie reaping work below.
start_nginx() {
    rm -f /tmp/nginx/nginx.pid
    nginx -g 'daemon off;' &
    NGINX_PID=$!
    for _ in $(seq 1 30); do
        [ -f /tmp/nginx/nginx.pid ] && break
        sleep 1
    done
    if [ ! -f /tmp/nginx/nginx.pid ]; then
        echo "[entrypoint] ERROR: nginx failed to start" >&2
        exit 1
    fi
    echo "[entrypoint] nginx started (pid $NGINX_PID)"
}

echo "[entrypoint] Starting nginx..."
start_nginx

# Bound to loopback only: nginx on port 8000 is the public entry point and
# proxies to this port.
echo "[entrypoint] Starting API server (uvicorn)..."
uvicorn api.main:app --host 127.0.0.1 --port 8001 &
UV_PID=$!
echo "[entrypoint] uvicorn started (pid $UV_PID)"

# Supervisor loop: this script is PID 1, so it must stay alive for the whole
# container lifetime.
#  - `wait -n` wakes the moment a direct child dies and reaps it. Without the
#    reap, a dead nginx master would linger as a zombie whose PID still
#    answers kill -0, and the restart below would never trigger.
#  - nginx death is invisible to Docker (the container stays "Up" while the
#    whole site is down), so it is restarted here.
#  - uvicorn death exits the container; the compose `restart: unless-stopped`
#    policy recreates it fresh.
while :; do
    wait -n 2>/dev/null || true

    if ! kill -0 "$UV_PID" 2>/dev/null; then
        echo "[entrypoint] uvicorn died - exiting container (restart policy recreates it)" >&2
        exit 1
    fi

    if ! kill -0 "$NGINX_PID" 2>/dev/null; then
        echo "[entrypoint] nginx died - stopping orphaned workers, restarting"
        # A master that dies suddenly leaves its workers behind; they get
        # reparented here and keep listening on 8000/8003, which would make
        # the new master's bind fail. TERM first (graceful), then KILL.
        for _pid in $(pgrep -P $$ 2>/dev/null || true); do
            [ "$_pid" = "$UV_PID" ] || kill -TERM "$_pid" 2>/dev/null || true
        done
        sleep 2
        for _pid in $(pgrep -P $$ 2>/dev/null || true); do
            [ "$_pid" = "$UV_PID" ] || kill -KILL "$_pid" 2>/dev/null || true
        done
        start_nginx
    fi
done
