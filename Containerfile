# Playwright's image ships Chromium + every system dep it needs; the tag must
# match the playwright== pin in pyproject.toml. Larger than the uv-slim base the
# rest of the collection uses, but it's what makes tiers 2-3 of the fetch
# ladder work, and it's the same image for every site adapter.
FROM mcr.microsoft.com/playwright/python:v1.47.0-noble

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Pull in security updates the base image lags behind on
# (rebuild with --pull --no-cache to refresh this layer)
RUN apt-get update && apt-get -y upgrade && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# README.md + LICENSE are build-time inputs: pyproject declares
# readme/license-files, so hatchling refuses to build the wheel without them.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

# Install the project only once src/ is present (syncing it earlier bakes an
# empty install that `uv run` would rebuild at every start).
COPY src/ src/
RUN uv sync --frozen --no-dev

# Cache, cookie jars and the persistent Chromium profile. Mount a volume here.
ENV DATA_DIR=/data
VOLUME ["/data"]

# Chromium needs more than Docker's 64MB default /dev/shm: run with --shm-size=512m.
EXPOSE 8347

ENV UV_PROJECT_ENVIRONMENT=/app/.venv
HEALTHCHECK --interval=60s --timeout=10s CMD ["/app/.venv/bin/python", "-m", "au_shopping_mcp.healthcheck"]
# The server itself is PID 1, not a resident `uv run` parent.
CMD ["/app/.venv/bin/au-shopping-mcp"]
