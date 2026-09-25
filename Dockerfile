# Photo Dedup: один образ для engine (CLI) и web (Streamlit).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# Сначала зависимости — кэш слоя не инвалидируется при правке кода
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine/ ./engine/
COPY web/ ./web/
COPY scripts/ ./scripts/
COPY docs/ ./docs/

# non-root user; /data/cache — named volume инициализируется правами appuser
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data/src /data/trash /data/cache/thumbs \
    && chown -R appuser:appuser /data /app

USER appuser

# engine читает настройки по умолчанию отсюда (ro-маунт в compose);
# web переопределяет SETTINGS_PATH на rw-маунт /data/settings.toml
ENV SETTINGS_PATH=/app/settings.toml \
    THUMBS_DIR=/data/cache/thumbs

# ENTRYPOINT позволяет `docker compose run --rm engine run|status|stop ...`
ENTRYPOINT ["python", "-m", "engine.cli"]
CMD ["--help"]
