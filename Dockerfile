# Base images are pinned by digest; the tag is in the comment above each FROM.
# Dependabot's docker ecosystem bumps them. Keep NODE_VERSION on the .nvmrc line
# (tests/test_docs_consistency.py checks it).
ARG NODE_VERSION=22.23.3

# Stage 1: build the dashboard (served by FastAPI from /srv/ui/dist).
# node:22.23.3-slim
FROM node:${NODE_VERSION}-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS ui
# APP_VERSION (e.g. the release tag) is shown in the dashboard; empty means the
# UI falls back to ui/package.json.
ARG APP_VERSION=""
ENV VITE_APP_VERSION=${APP_VERSION}
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
# Explicit list (and .dockerignore): never copy a host node_modules over npm ci.
COPY ui/index.html ui/vite.config.ts ui/tsconfig.json ui/tsconfig.app.json ui/tsconfig.node.json ./
COPY ui/src ./src
RUN npm run build

# Stage 2: the API image (zero-ML: hash embeddings + deterministic decisions).
# Dockerfile.inference layers the [inference] extra on top of this image.
# python:3.12-slim
FROM python:3.14-slim@sha256:f85c5697265c178cc6887276c55fe16cf3d14ca35c3df6a5eab3b360534a55d2 AS base
LABEL org.opencontainers.image.source="https://github.com/Amantux/mcp-router" \
      org.opencontainers.image.title="mcp-router" \
      org.opencontainers.image.description="MCP Router API + dashboard"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCPR_UI_DIST=/srv/ui/dist \
    MCPR_SKILLS_CACHE_DIR=/srv/skills-cache \
    MCPR_MODELS_CACHE_DIR=/srv/models-cache \
    HF_HOME=/srv/models-cache/hf
# git: required by git skill sources (gitsource.py shells out to `git clone`).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Node 22 runtime (node + npm + npx) from the same pinned node image, so
# imported `npx ...` stdio MCP servers can start inside the container (D16).
# `uvx` servers are NOT supported in this image: see docs/deploy.md.
COPY --from=ui /usr/local/bin/node /usr/local/bin/node
COPY --from=ui /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version && npx --version
RUN addgroup --system --gid 1000 app && adduser --system --uid 1000 --ingroup app --home /home/app app
WORKDIR /srv
# Dependency layer keyed on pyproject.toml only: editing src/ reuses it.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt hatchling \
 && rm /tmp/requirements.txt
COPY README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation .
COPY --from=ui /ui/dist ./ui/dist
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Writable dirs owned by the app user BEFORE dropping root, so named volumes
# mounted here inherit app ownership on first use.
RUN mkdir -p /data /srv/skills-cache /srv/models-cache \
 && chown -R app:app /data /srv/skills-cache /srv/models-cache \
 && chmod 0755 /usr/local/bin/docker-entrypoint.sh
USER app
# npx caches downloaded packages under $HOME/.npm, which the app user owns.
ENV HOME=/home/app \
    npm_config_cache=/home/app/.npm \
    npm_config_update_notifier=false
VOLUME ["/data", "/srv/skills-cache", "/srv/models-cache"]
EXPOSE 8400
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MCPR_PORT','8400'), timeout=2).status == 200 else 1)"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
