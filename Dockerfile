FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300

# Install only the OS packages we truly need at runtime.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ------------------------------------------------------------
# Build stage – compile Python dependencies (wheels) once.
# ------------------------------------------------------------
FROM base AS build

# Copy only the requirements files first to cache pip installs.
COPY requirements-api.txt requirements-dashboard.txt requirements-pipeline.txt requirements-dev.txt ./

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements-api.txt && \
    pip install --no-cache-dir -r requirements-dashboard.txt && \
    pip install --no-cache-dir -r requirements-pipeline.txt && \
    pip install --no-cache-dir -r requirements-dev.txt

# ------------------------------------------------------------
# Final image – copy source and compiled dependencies.
# ------------------------------------------------------------
FROM base

WORKDIR /app

# Bring in compiled wheels from the build stage.
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# Copy application code.
COPY app ./app
COPY dashboard ./dashboard
COPY pipeline ./pipeline
COPY data ./data

# Ensure the data directory exists (SQLite will store its DB here).
RUN mkdir -p /app/data

EXPOSE 8000 8501

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]