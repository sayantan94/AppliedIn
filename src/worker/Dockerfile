# AppliedIn worker image: apply worker + career-site crawler (one image, two
# modes). Built from the repo root:
#   docker build -f src/worker/Dockerfile .
#
# Base image bakes in Chromium builds matching playwright==1.47.0 — the
# `runtime` extra pins the same version so browsers stay in lockstep.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install the project with the runtime extra (playwright/gmail) into the image's
# system python (which already carries the Playwright browsers).
COPY pyproject.toml pyproject.toml
COPY src src
COPY config config

RUN uv pip install --system ".[runtime]"

ENV APPLIEDIN_TASK_MODE=apply \
    APPLIEDIN_CONFIG_DIR=/app/config

# Task mode is switched by the dispatcher/scheduler via env:
#   APPLIEDIN_TASK_MODE=apply  reads APPLIEDIN_JOB_PK
#   APPLIEDIN_TASK_MODE=crawl  reads APPLIEDIN_CRAWL_COMPANY (JSON)
ENTRYPOINT ["python", "-m", "worker"]
