FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_DEFAULT_TIMEOUT=300

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-api.txt .
COPY requirements-dashboard.txt .
COPY requirements-pipeline.txt .
COPY requirements-dev.txt .

RUN pip install --upgrade pip setuptools wheel

# Install CPU-only Torch FIRST
RUN pip install --no-cache-dir \
    torch==2.2.2 \
    torchvision==0.17.2 \
    --index-url https://download.pytorch.org/whl/cpu

# Install remaining dependencies
RUN pip install --no-cache-dir \
    -r requirements-api.txt \
    -r requirements-dashboard.txt \
    -r requirements-pipeline.txt \
    -r requirements-dev.txt

COPY app ./app
COPY dashboard ./dashboard
COPY pipeline ./pipeline
COPY data ./data

RUN mkdir -p /app/data

EXPOSE 8000
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]