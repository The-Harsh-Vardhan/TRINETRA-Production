# Inference Worker Dockerfile — requires NVIDIA GPU at runtime

# TensorRT base: provides CUDA 12.x + cuDNN + TRT 8.x
FROM nvcr.io/nvidia/tensorrt:23.10-py3 AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libgl1-mesa-glx libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Layer 1: Python deps (changes rarely)
COPY requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt

# Layer 2: Application code
COPY . .

# Non-root user
RUN useradd -m -u 1001 inferencer && chown -R inferencer /app
USER inferencer

# TRT cache directory (writable at runtime)
RUN mkdir -p /tmp/trt_cache

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import redis; redis.Redis.from_url('${REDIS_URL}').ping()" || exit 1

# Entry: plain python — Celery is optional; the main loop is self-contained
CMD ["python", "main.py"]
