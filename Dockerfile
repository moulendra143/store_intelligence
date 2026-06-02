# Store Intelligence API — Dockerfile
# Installs ALL dependencies in every container so docker compose up --build
# is fully self-contained regardless of which service an evaluator tests.
#
# Split requirements files are kept for reference:
#   requirements-api.txt       → FastAPI backend
#   requirements-dashboard.txt → Streamlit dashboard
#   requirements-pipeline.txt  → OpenCV + PyTorch/YOLOv8 (heavy)
#   requirements-dev.txt       → pytest / test tooling

FROM python:3.11-slim AS base

# System libraries required by OpenCV and Shapely
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxrender1 \
    libxext6 \
    libgl1 \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Install ALL Python dependencies ─────────────────────────────────
# Install pip upgrade first, then all four requirement files so every
# container can run any part of the system (API, dashboard, pipeline,
# or tests) without a missing-module error.
COPY requirements-api.txt \
     requirements-dashboard.txt \
     requirements-pipeline.txt \
     requirements-dev.txt \
     ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        -r requirements-api.txt \
        -r requirements-dashboard.txt \
        -r requirements-pipeline.txt \
        -r requirements-dev.txt

# ── Copy application code ────────────────────────────────────────────
COPY app/ ./app/
COPY dashboard/ ./dashboard/
COPY pipeline/ ./pipeline/
COPY data/ ./data/

# Create writable data directory for SQLite DB and events file
RUN mkdir -p /app/data

# Expose API and Dashboard ports
EXPOSE 8000 8501

# Healthcheck — used by docker compose to gate dependent services
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default: run FastAPI production server
# docker-compose.yml overrides CMD for the dashboard and pipeline services
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
