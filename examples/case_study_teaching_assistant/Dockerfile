FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    TEACHING_ASSISTANT_CONFIG=/app/examples/case_study_teaching_assistant/config/ecs.yaml

WORKDIR /app
RUN groupadd --system --gid 10001 runtime \
    && useradd --system --uid 10001 --gid runtime --home-dir /nonexistent runtime

COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples
RUN pip install --no-cache-dir ".[traccia,postgres,redis,auth]"

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)"

CMD ["python", "-m", "examples.case_study_teaching_assistant.dashboard"]
