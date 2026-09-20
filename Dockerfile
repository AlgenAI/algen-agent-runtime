FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN groupadd --system algen && useradd --system --gid algen --create-home algen
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE CHANGELOG.md ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir '.[postgres]'
USER algen
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)" || exit 1
CMD ["algen-agent-runtime"]
