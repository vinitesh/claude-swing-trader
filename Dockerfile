# =====================================================================
# swing_platform — Docker image
#
# Multi-stage:
#   1. builder: installs uv-managed Python deps into a venv at /opt/venv
#   2. runtime: python:3.11-slim + the venv + the app code
#
# Build:   docker build -t swing-platform:latest .
# Run:     docker run --rm \
#            --env-file .env \
#            -v "$(pwd)/trading.db:/app/trading.db" \
#            -v "$(pwd)/logs:/app/logs" \
#            -v "$(pwd)/data_cache:/app/data_cache" \
#            swing-platform:latest \
#            run-live --dry-run
#
# .env, trading.db, logs/, data_cache/ are MOUNTED — never baked in.
# =====================================================================

# ---------------- Builder ----------------
FROM python:3.11-slim AS builder

# uv = fastest Python dep manager. Pinned for reproducibility.
COPY --from=ghcr.io/astral-sh/uv:0.5.6 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Cache deps separately from code so code changes don't bust the dep layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy app code and complete install (this writes the project's own entry
# point script `swingbot` into /opt/venv/bin/).
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ---------------- Runtime ----------------
FROM python:3.11-slim AS runtime

# Use a non-root user. Trading code shouldn't run as root.
RUN groupadd --gid 1000 swing \
    && useradd --uid 1000 --gid swing --shell /bin/bash --create-home swing

# tini for proper signal handling (clean SIGTERM on `docker stop`)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

# America/New_York is the trading clock; keep the container in ET so cron
# expressions and timestamps in logs match exchange hours.
ENV TZ=America/New_York
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Copy app code (everything not in .dockerignore)
COPY --chown=swing:swing . /app

# Make the venv the default Python
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Pre-create mount points so a fresh container has writable dirs even
# if the host hasn't created them yet.
RUN mkdir -p /app/logs /app/data_cache /app/backtest_results \
    && chown -R swing:swing /app

USER swing

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["--help"]
