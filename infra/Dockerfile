# syntax=docker/dockerfile:1
FROM python:3.12.10-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e .

# Runs unprivileged: a container that can write its own code is a container an
# attacker can rewrite.
RUN useradd --create-home --uid 10001 rcm && chown -R rcm:rcm /app
USER rcm

EXPOSE 8000
CMD ["uvicorn", "rcm.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
