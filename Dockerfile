# ── Stage 1: Build web frontend ────────────────────────────────────────────
FROM node:22-slim AS web-build
WORKDIR /app
COPY packages/web/package*.json ./
# package-lock.json has internal Nexus URLs unreachable outside corp network.
# Use --no-package-lock to resolve from the npm registry instead.
# Defaults to the China mirror (npmmirror) for fast CN builds; override with
# --build-arg NPM_REGISTRY=https://registry.npmjs.org for other environments.
ARG NPM_REGISTRY=https://registry.npmmirror.com
# Persist the npm download cache in BuildKit's cache store so repeated
# builds only download what changed (keeps even cache-invalidated layers cheap).
ENV npm_config_cache=/root/.npm
RUN --mount=type=cache,target=/root/.npm \
    npm install -g npm@latest --registry=$NPM_REGISTRY \
    && rm -f package-lock.json \
    && npm install --registry=$NPM_REGISTRY
COPY packages/web/ ./
ARG VITE_BASE_PATH=/
ARG VITE_API_BASE_PATH=
ENV VITE_BASE_PATH=$VITE_BASE_PATH
ENV VITE_API_BASE_PATH=$VITE_API_BASE_PATH
RUN npx vite build

# ── Stage 2: Python base with ODBC + Playwright + nginx ───────────────────
# Pin to bookworm (Debian 12) - Microsoft ODBC packages support bookworm;
# the untagged python:3.12-slim has moved to trixie (Debian 13).
FROM python:3.12-slim-bookworm AS python-base

# Accept proxy build args - used by apt and curl inside this layer.
ARG http_proxy
ARG https_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY

# China build mirrors (override with --build-arg for other environments).
ARG APT_MIRROR_HOST=mirrors.tuna.tsinghua.edu.cn
# No working China mirror exists for the Microsoft repo (huaweicloud and others
# return HTML 404s for these paths) - keep the official repo.
ARG MS_REPO=https://packages.microsoft.com
ARG NODE_BASE_URL=https://npmmirror.com/mirrors/node
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_DOWNLOAD_HOST=$PLAYWRIGHT_DOWNLOAD_HOST

# Install ODBC Driver 17 for SQL Server, Java, Maven, nginx and common tools.
# Corporate SSL-inspection proxy presents self-signed certs - disable
# verification for curl and apt so the Microsoft package repo can be fetched.
RUN if [ -n "$http_proxy" ]; then \
        echo "Acquire::http::Proxy \"$http_proxy\";" > /etc/apt/apt.conf.d/99proxy; \
        echo "Acquire::https::Proxy \"$http_proxy\";" >> /etc/apt/apt.conf.d/99proxy; \
    elif [ -n "$HTTP_PROXY" ]; then \
        echo "Acquire::http::Proxy \"$HTTP_PROXY\";" > /etc/apt/apt.conf.d/99proxy; \
        echo "Acquire::https::Proxy \"$HTTP_PROXY\";" >> /etc/apt/apt.conf.d/99proxy; \
    fi
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    --mount=type=cache,target=/tmp/ms-keys \
    set -eu \
    && sed -i 's|deb.debian.org|'"$APT_MIRROR_HOST"'|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true \
    && printf 'Acquire::https::Verify-Peer "false";\nAcquire::https::Verify-Host "false";\nAcquire::Retries "3";\n' \
        > /etc/apt/apt.conf.d/99no-verify \
    && apt-get update && apt-get install -y --no-install-recommends \
        bash curl git gnupg jq unixodbc-dev maven nginx \
    && if [ ! -f /tmp/ms-keys/microsoft.asc ]; then \
        curl -fsSLk $MS_REPO/keys/microsoft.asc -o /tmp/ms-keys/microsoft.asc; \
    fi \
    # gpg 2.2 dearmor mode rejects `-o` alongside a file arg ("usage:" error,
    # exit 2) - read from stdin and redirect to the keyring instead.
    && gpg --batch --dearmor < /tmp/ms-keys/microsoft.asc > /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] $MS_REPO/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 msopenjdk-21 \
    && rm -f /etc/apt/apt.conf.d/99no-verify

# Install Playwright system dependencies
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        fonts-liberation fonts-noto-cjk libasound2 libatk-bridge2.0-0 libatk1.0-0 \
        libcairo2 libcups2 libdbus-1-3 libdrm2 libfontconfig1 \
        libfreetype6 libgbm1 libglib2.0-0 libgtk-3-0 libnspr4 \
        libnss3 libpango-1.0-0 libx11-6 libx11-xcb1 libxcb1 \
        libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 \
        libxrandr2 libxshmfence1

# Playwright >= 1.60 requires Node >= 20, but Debian bookworm apt only ships
# Node 18. Install Node 22 LTS (from the China mirror) so agents can run Playwright.
# Cache the tarball in BuildKit's cache store so repeated builds skip the download.
RUN --mount=type=cache,target=/tmp/node-dl \
    NODE_TAR=node-v22.14.0-linux-x64.tar.gz \
    && if [ ! -f "/tmp/node-dl/$NODE_TAR" ]; then \
        curl -fsSLk "$NODE_BASE_URL/v22.14.0/$NODE_TAR" -o "/tmp/node-dl/$NODE_TAR"; \
    fi \
    && tar -xz -C /usr/local --strip-components=1 -f "/tmp/node-dl/$NODE_TAR" \
    && node --version && npm --version

# ── Stage 3: Install Python deps ──────────────────────────────────────────
FROM python-base AS python-deps
WORKDIR /app

COPY packages/agent-core/pyproject.toml /tmp/agent-core/
COPY packages/api/pyproject.toml /tmp/api/

# Hatchling needs at least __init__.py to build a valid (possibly empty) wheel.
# Real source is copied in stage 4; this layer is only for dep caching.
RUN mkdir -p /tmp/agent-core/src/agent_core /tmp/api/src/api \
    && touch /tmp/agent-core/src/agent_core/__init__.py \
             /tmp/api/src/api/__init__.py

# China PyPI mirror (override with --build-arg PIP_INDEX_URL for other environments).
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
# Cache pip's HTTP cache in BuildKit's cache store: a dependency change then
# only re-downloads the wheels that actually changed, not the whole tree.
# The [test] extras (pytest, pytest-asyncio, pytest-mock, vcrpy, aiosqlite)
# are baked in because the sandbox has no pip/network: agents run
# `python -m pytest` against project checkouts directly.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url $PIP_INDEX_URL \
    --trusted-host $PIP_TRUSTED_HOST \
    "/tmp/agent-core[test]" \
    "/tmp/api[test]"

# Arm the sandbox file-access guard even when a command overrides PYTHONPATH:
# site.py auto-imports sitecustomize.py from site-packages, so the audit hook
# loads no matter what PYTHONPATH the agent's command sets (a leading
# `PYTHONPATH=...` assignment would otherwise replace the tool-injected entry
# and silently disarm the guard). Never clobber a pre-existing sitecustomize
# (e.g. a corporate SSL shim) - in that case the guard keeps the PYTHONPATH
# channel only, which is the same behavior as local dev.
COPY packages/agent-core/src/agent_core/sandbox_guard/sitecustomize.py /tmp/sandbox-guard-sitecustomize.py
RUN if [ -f /usr/local/lib/python3.12/site-packages/sitecustomize.py ]; then \
        echo "WARN: existing sitecustomize.py in site-packages - sandbox guard uses PYTHONPATH channel only"; \
    else \
        cp /tmp/sandbox-guard-sitecustomize.py /usr/local/lib/python3.12/site-packages/sitecustomize.py; \
    fi

# Own layer, own cache key: pip dependency changes must not re-download the
# ~170MB Playwright browser (and vice versa).
# Cache the browser in BuildKit's cache store: playwright install checks if
# the browser is already present and skips the download; we then copy it into
# the image layer so COPY --from in the final stage can pick it up.
RUN --mount=type=cache,target=/tmp/pw-cache \
    PLAYWRIGHT_BROWSERS_PATH=/tmp/pw-cache python -m playwright install chromium \
    && mkdir -p /ms-playwright \
    && cp -a /tmp/pw-cache/. /ms-playwright/

# Pentest toolchain for the pentest-expert agent (own layer, own cache key:
# tool changes must not re-download the ~170MB browser, and vice versa).
# Installed into the main site-packages so agents invoke them as
# `python3 -m <module>` - the shell tool allowlist whitelists python3, not tool
# console scripts. Dependency conflicts with the framework deps are caught by
# `pip check` in the final stage's verification layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url $PIP_INDEX_URL \
    --trusted-host $PIP_TRUSTED_HOST \
    sqlmap semgrep bandit pip-audit detect-secrets sslyze dirsearch wafw00f

# ── Stage 4: Final combined image (API + Web) ─────────────────────────────
FROM python-base AS final
RUN groupadd -r appuser && useradd -r -m -d /home/appuser -g appuser appuser
WORKDIR /app

# Copy Python environment and Playwright browser from deps stage
COPY --from=python-deps /usr/local/lib/python3.12 /usr/local/lib/python3.12
COPY --from=python-deps /usr/local/bin /usr/local/bin
COPY --from=python-deps /ms-playwright /ms-playwright

# Copy Mermaid runtime from the web-build stage (no local npm install required)
COPY --from=web-build /app/node_modules/mermaid/dist/mermaid.min.js /app/vendor/mermaid/mermaid.min.js

# Copy built web dist into nginx html directory
COPY --from=web-build /app/dist /usr/share/nginx/html

# Copy application source (agent-core + api merged into /app/src)
COPY packages/agent-core/src ./src
COPY packages/api/src ./src
COPY packages/api/alembic ./alembic
COPY packages/api/alembic.ini .

# Bundle agents, workflows, knowledge, scaffold
COPY agents/ ./agents/
COPY workflows/ ./workflows/
COPY knowledge/ ./knowledge/
COPY scaffold/ ./scaffold/

ENV JAVA_HOME=/usr/lib/jvm/msopenjdk-21-amd64 \
    HOME=/home/appuser \
    MAVEN_USER_HOME=/home/appuser/.m2 \
    GRADLE_USER_HOME=/home/appuser/.gradle \
    PYTHONPATH=/app/src \
    PATH="/usr/lib/jvm/msopenjdk-21-amd64/bin:${PATH}" \
    npm_config_cache=/tmp/npm-cache

# Fail during the image build if the tools used by agents and healthchecks are
# missing from the final runtime.
RUN set -eux; \
    command -v bash git jq node npm curl java javac mvn nginx; \
    bash --version; \
    git --version; \
    jq --version; \
    node --version; \
    npm --version; \
    curl --version >/dev/null; \
    java --version; \
    javac --version; \
    mvn --version; \
    test -x /ms-playwright/chromium-*/chrome-linux/chrome \
        || test -x /ms-playwright/chromium-*/chrome-linux64/chrome; \
    python -m pytest --version; \
    pip check; \
    python -m sqlmap.sqlmap --version; \
    python -c "from semgrep.console_scripts.entrypoint import main; main()" --version; \
    python -m bandit --version; \
    python -m pip_audit --version; \
    python -m detect_secrets --version; \
    python -m sslyze --help; \
    python -m dirsearch --version; \
    python -m wafw00f.main -V

# Configure nginx for non-root execution.
# All runtime-writable paths live under /tmp/nginx. /tmp is world-writable
# (1777) for ANY uid, so this works whether the platform runs the container
# as the in-image appuser or as a random OpenShift/K8s uid. The entrypoint
# also recreates these dirs at startup in case the runtime mounts /tmp as an
# empty tmpfs (wiping anything baked in here).
RUN sed -i \
        -e 's|^user .*;|# user directive is unnecessary for non-root nginx|' \
        -e 's|^pid .*|pid /tmp/nginx/nginx.pid;|' \
        -e 's|^error_log .*|error_log /tmp/nginx/error.log;|' \
        -e 's|access_log .*|access_log /tmp/nginx/access.log;|' \
        -e '/client_body_temp_path/d' \
        -e '/proxy_temp_path/d' \
        -e '/fastcgi_temp_path/d' \
        -e '/uwsgi_temp_path/d' \
        -e '/scgi_temp_path/d' \
        /etc/nginx/nginx.conf \
    && sed -i '/^http {/a \    client_body_temp_path /tmp/nginx/body;\n    proxy_temp_path /tmp/nginx/proxy;\n    fastcgi_temp_path /tmp/nginx/fastcgi;\n    uwsgi_temp_path /tmp/nginx/uwsgi;\n    scgi_temp_path /tmp/nginx/scgi;' \
        /etc/nginx/nginx.conf \
    && rm -f /etc/nginx/sites-enabled/default \
    && mkdir -p /tmp/nginx/body /tmp/nginx/proxy /tmp/nginx/fastcgi \
                 /tmp/nginx/uwsgi /tmp/nginx/scgi \
    && chmod -R 0777 /tmp/nginx \
    && chown -R appuser:appuser /usr/share/nginx/html

# Copy nginx config and entrypoint script
COPY nginx-combined.conf /etc/nginx/conf.d/default.conf
COPY docker-entrypoint.sh /docker-entrypoint.sh
# Strip Windows CRLF line endings so the script runs correctly on Linux
RUN sed -i 's/\r$//' /docker-entrypoint.sh \
    && chmod +x /docker-entrypoint.sh

RUN mkdir -p /home/appuser/.m2 /home/appuser/.gradle /app/projects /tmp/npm-cache \
    && chown -R appuser:appuser /app /ms-playwright /home/appuser /tmp/npm-cache

EXPOSE 8000
USER appuser
CMD ["/docker-entrypoint.sh"]

# ── Stage 5: Web dev server (local dev only) ──────────────────────────────
FROM node:22-slim AS web-dev
WORKDIR /app
COPY packages/web/package*.json ./
# package-lock.json has internal Nexus URLs unreachable outside corp network.
ARG NPM_REGISTRY=https://registry.npmmirror.com
ENV npm_config_cache=/root/.npm
RUN --mount=type=cache,target=/root/.npm \
    rm -f package-lock.json && npm install --registry=$NPM_REGISTRY
COPY packages/web/ ./
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
