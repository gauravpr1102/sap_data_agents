# Web front end + agent for Cloud Run (docs/DEPLOYMENT.md).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SAPKIT_AUTH=iap \
    SAPKIT_LOG_JSON=1 \
    DEEPEVAL_TELEMETRY_OPT_OUT=YES \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY domain ./domain
COPY sql ./sql
RUN pip install --upgrade pip && pip install ".[web]" \
 && useradd --create-home --uid 10001 sapkit \
 && mkdir -p /app/data /app/out && chown -R sapkit /app/data /app/out

USER sapkit
EXPOSE 8080
# The data file is mounted at /app/data (Cloud Storage volume in the pilot; see DEPLOYMENT.md).
CMD ["sh", "-c", "uvicorn --factory sapkit.web:create_app --host 0.0.0.0 --port ${PORT}"]
