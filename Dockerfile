FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=1000 \
    PYTHONPATH=/app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        curl \
        libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ------------------------------------------------------------
# Build stage
# ------------------------------------------------------------
FROM base AS build

COPY requirements-api.txt requirements-dashboard.txt requirements-pipeline.txt requirements-dev.txt ./

COPY . /app

RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements-api.txt && \
    pip install --no-cache-dir -r requirements-dashboard.txt && \
    pip install --no-cache-dir -r requirements-pipeline.txt && \
    pip install --no-cache-dir -r requirements-dev.txt && \
    pip install --no-cache-dir uvicorn

# ------------------------------------------------------------
# Final image
# ------------------------------------------------------------
FROM base

WORKDIR /app
ENV PYTHONPATH=/app

COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
COPY --from=build /app /app

EXPOSE 8000 8501

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]