FROM node:20-slim AS builder
WORKDIR /build
COPY package*.json ./
# package-lock.json has internal Nexus URLs unreachable outside corp network;
# resolve from the registry instead (same approach as the root Dockerfile).
# Defaults to the China mirror (npmmirror) for fast CN builds; override with
# --build-arg NPM_REGISTRY for other environments.
ARG NPM_REGISTRY=https://registry.npmmirror.com
# Persist the npm download cache in BuildKit's cache store so repeated builds
# only re-download what changed.
ENV npm_config_cache=/root/.npm
RUN --mount=type=cache,target=/root/.npm \
    rm -f package-lock.json && npm install --registry=$NPM_REGISTRY
COPY . .
ARG VITE_API_BASE_PATH=/agent
ARG VITE_BASE_PATH=/agent-web/
RUN npm run build

FROM nginx:1.25-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
# wget is provided by BusyBox in the nginx Alpine base image and remains
# available after switching to the unprivileged nginx user.
RUN rm -f /etc/nginx/conf.d/nginx.conf
COPY --from=builder /build/dist /usr/share/nginx/html
RUN sed -i \
        -e 's|^user .*;|# user directive is unnecessary for non-root nginx|' \
        -e 's|^pid .*;|pid /tmp/nginx.pid;|' \
        /etc/nginx/nginx.conf \
    && mkdir -p /tmp/nginx /var/cache/nginx \
    && chown -R 101:101 /tmp/nginx /var/cache/nginx /usr/share/nginx/html
USER 101:101
ENTRYPOINT []
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
