# Stage 1: build the dashboard (served by FastAPI from /srv/ui/dist).
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci
# Explicit list (no .dockerignore): never copy a host node_modules over npm ci.
COPY ui/index.html ui/vite.config.ts ui/tsconfig.json ui/tsconfig.app.json ui/tsconfig.node.json ./
COPY ui/src ./src
RUN npm run build

# Stage 2: the API image.
FROM python:3.12-slim
RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /srv
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=ui /ui/dist ./ui/dist
ENV MCPR_UI_DIST=/srv/ui/dist
USER app
EXPOSE 8400
CMD ["uvicorn", "--factory", "mcprouter.api.app:create_app", "--host", "0.0.0.0", "--port", "8400"]
