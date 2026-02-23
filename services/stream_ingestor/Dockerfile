# Stream Ingestor Dockerfile
# No GPU required — pure CPU RTSP decode + Redis push

FROM python:3.11-slim AS base

# System deps for OpenCV RTSP via FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libglib2.0-0 libgl1-mesa-glx libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer 1: Dependencies (cached unless requirements changes)
COPY requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt

# Layer 2: Application code
COPY . .

# Non-root user
RUN useradd -m -u 1001 ingestor && chown -R ingestor /app
USER ingestor

EXPOSE 8000 8001

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
