FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PYTHONPATH=/app

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
# Copy application source code
COPY . /app

RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements-api.txt && \
    pip install --no-cache-dir -r requirements-dashboard.txt && \
    pip install --no-cache-dir -r requirements-pipeline.txt && \
    pip install --no-cache-dir -r requirements-dev.txt && pip install --no-cache-dir uvicorn

# ------------------------------------------------------------
# Final image – copy source and compiled dependencies.
# ------------------------------------------------------------
FROM base

WORKDIR /app
ENV PYTHONPATH=/app

# Bring in compiled wheels from the build stage.
COPY --from=build /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
# Copy application source code from build stage
COPY --from=build /app /app

# Re‑install uvicorn in the final image so the executable script is available.
RUN pip install --no-cache-dir uvicorn

# Expose ports for FastAPI (8000) and Streamlit dashboard (8501)
EXPOSE 8000 8501

# Use the module form of uvicorn to avoid relying on a PATH lookup.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
