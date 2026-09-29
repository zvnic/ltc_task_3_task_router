# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.8.22 AS uv

FROM python:3.13.5-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./

FROM base AS runtime-build
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh
RUN uv sync --frozen --no-dev

FROM runtime-build AS runtime
RUN groupadd --system app && useradd --system --gid app --home /app app \
    && chown -R app:app /app /opt/venv
USER app
EXPOSE 8000
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

FROM base AS tools
RUN uv sync --frozen --group dev --no-install-project
COPY app ./app
COPY tests ./tests
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --group dev
CMD ["python", "-m", "app.cli", "--help"]
