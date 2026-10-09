# Stage 1: build the dashboard (served by FastAPI from /srv/ui/dist).
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
# Explicit list (and .dockerignore): never copy a host node_modules over npm ci.
COPY ui/index.html ui/vite.config.ts ui/tsconfig.json ui/tsconfig.app.json ui/tsconfig.node.json ./
COPY ui/src ./src
RUN npm run build

# Stage 2: the API image (zero-ML: hash embeddings + deterministic decisions).
# Dockerfile.inference layers the [inference] extra on top of this image.
FROM python:3.12-slim AS base
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
RUN addgroup --system --gid 1000 app && adduser --system --uid 1000 --ingroup app --home /home/app app
WORKDIR /srv
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=ui /ui/dist ./ui/dist
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# Writable dirs owned by the app user BEFORE dropping root, so named volumes
# mounted here inherit app ownership on first use.
RUN mkdir -p /data /srv/skills-cache /srv/models-cache \
 && chown -R app:app /data /srv/skills-cache /srv/models-cache \
 && chmod 0755 /usr/local/bin/docker-entrypoint.sh
USER app
ENV HOME=/home/app
VOLUME ["/data", "/srv/skills-cache", "/srv/models-cache"]
EXPOSE 8400
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MCPR_PORT','8400'), timeout=2).status == 200 else 1)"
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
