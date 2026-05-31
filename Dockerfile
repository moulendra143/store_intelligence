# Store Intelligence API — Dockerfile
# Multi-stage build: keeps image lean by separating build deps from runtime

FROM python:3.11-slim AS base

# System dependencies needed for OpenCV and Shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    libgl1-mesa-glx \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (maximise layer cache hits)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY pipeline/ ./pipeline/
COPY data/ ./data/

# Create writable data directory for the SQLite DB and events file
RUN mkdir -p /app/data

# Expose API port
EXPOSE 8000

# Healthcheck — used by docker compose to gate dependent services
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default: run FastAPI production server
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
