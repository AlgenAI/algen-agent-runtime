# Base images are pinned by digest; Dependabot (docker ecosystem) proposes updates.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10
WORKDIR /src
# Build the wheel with the hash-locked build tools only (no build isolation, which would download
# unpinned ones).
COPY requirements/build.txt ./requirements/build.txt
RUN pip install --require-hashes --no-deps -r requirements/build.txt
COPY pyproject.toml README.md LICENSE NOTICE CHANGELOG.md ./
COPY .agents/skills ./.agents/skills
COPY src ./src
COPY algen_agent_runtime_examples ./algen_agent_runtime_examples
RUN python -m build --wheel --no-isolation --outdir /dist .

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime
# Take the base image's security fixes published since it was built (apt CVE-2026-109518).
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60 PIP_RETRIES=10
RUN groupadd --system algen && useradd --system --gid algen --create-home algen
WORKDIR /app
# Dependencies come from the hash-locked requirements/service.txt (scripts/lock-requirements.sh),
# so a rebuild installs exactly the reviewed versions and a tampered download fails the build.
COPY requirements/service.txt ./requirements/service.txt
RUN pip install --require-hashes --no-deps -r requirements/service.txt
COPY --from=build /dist/ /tmp/dist/
RUN pip install --no-deps /tmp/dist/*.whl && rm -rf /tmp/dist && pip check
USER algen
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)" || exit 1
CMD ["algen-agent-runtime"]
