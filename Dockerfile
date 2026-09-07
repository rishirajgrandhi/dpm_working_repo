# Multi-stage: build the SPA, then serve it from the API container (04 §1).
# One artifact, one URL, one thing to deploy (12 §1).

# ── stage 1: the React SPA ────────────────────────────────────────────────────
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── stage 2: the service ──────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# git is a runtime dependency, not a build one: the repo reader clones the pipeline repo
# on every run to resolve ownership and bind sign-offs to a commit (02 §1).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DPHM_ENV=production

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY --from=web /web/dist ./web/dist

# Non-root, and no writable filesystem outside /tmp (12 §8).
RUN useradd --create-home --uid 10001 dphm \
 && mkdir -p /tmp/dphm && chown -R dphm:dphm /tmp/dphm /app
USER dphm

EXPOSE 8000

# Liveness only. Warehouse reachability is /diagnostics, which is a signed-in view —
# a health probe must not depend on the warehouse, or a Snowflake blip restarts the pod.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/healthz', timeout=4).status == 200 else 1)"

# One replica. The in-process worker pool is the dispatcher; the job table is durable, so
# scale-out is a dispatcher swap rather than a redesign (12 §5).
CMD ["uvicorn", "dphm.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
