# syntax=docker/dockerfile:1

# ---- builder ---------------------------------------------------------------
# Dependencies are resolved in a throwaway stage so that build tooling never
# reaches the runtime image.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only the dependency manifest first: application edits then reuse the
# cached dependency layer instead of reinstalling on every commit.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LEADERBOARD_ENVIRONMENT=production \
    LEADERBOARD_LOG_FORMAT=json \
    PORT=8080

# Run as an unprivileged user: a container compromise should not be root.
RUN useradd --create-home --uid 10001 appuser
COPY --from=builder /opt/venv /opt/venv
WORKDIR /srv
COPY app ./app
RUN chown -R appuser:appuser /srv
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status == 200 else 1)"

# `sh -c` so $PORT (injected by App Platform) is expanded at runtime.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-2} --no-access-log"]
