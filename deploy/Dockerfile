# Aurion runtime image. Target: a small VPS (1 vCPU / 1-2 GB).
#
# Slim rather than alpine: numpy and matplotlib wheels are glibc-built, and on musl pip
# falls back to compiling from source, which neither fits the image budget nor the box.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

# tzdata is required: session and quarter logic is timezone-aware and zoneinfo has no
# bundled database on slim images, so every America/New_York lookup would fail at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY aurion ./aurion
RUN pip install --no-cache-dir .

COPY config.yaml ./config.yaml
COPY deploy/healthcheck.py ./deploy/healthcheck.py

# Run unprivileged. The process needs no root capability: it reads market data and sends
# messages, and there is no execution surface to protect.
RUN useradd --create-home --uid 10001 aurion \
 && mkdir -p /app/data /app/logs /app/reports \
 && chown -R aurion:aurion /app
USER aurion

VOLUME ["/app/data", "/app/logs"]

HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
  CMD python /app/deploy/healthcheck.py || exit 1

CMD ["python", "-m", "aurion.runtime.app"]
